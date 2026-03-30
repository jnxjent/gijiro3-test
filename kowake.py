# kowake.py
import os
import sys
import platform
import shutil
import subprocess
import tempfile
import uuid
import re
import json
import asyncio
import base64
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote

from dotenv import load_dotenv
from deepgram import Deepgram
import openai

from storage import upload_to_blob, download_blob

# ── ロガー設定 ─────────────────────────────────────────
logger = logging.getLogger("ProcessAudioFunction")

# ── app/send_guard.py を確実に読み込むためのパス設定 ─────────────────
BASE_DIR = os.path.dirname(__file__)
APP_DIR = os.path.join(BASE_DIR, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from send_guard import mark_once, unmark  # ★ 冪等ガード

# ── 1) ffmpeg / ffprobe パス検出 ───────────────────────────────
ffmpeg_path = os.getenv("FFMPEG_PATH")
ffprobe_path = os.getenv("FFPROBE_PATH")

if not (ffmpeg_path and ffprobe_path):
    BASE_DIR2 = os.path.dirname(__file__)
    BIN_ROOT = os.getenv("FFMPEG_HOME", os.path.join(BASE_DIR2, "ffmpeg", "bin"))
    if platform.system() == "Windows":
        tb = os.path.join(BIN_ROOT, "win")
        ffmpeg_path = os.path.join(tb, "ffmpeg.exe")
        ffprobe_path = os.path.join(tb, "ffprobe.exe")
    else:
        tb = os.path.join(BIN_ROOT, "linux")
        ffmpeg_path = os.path.join(tb, "ffmpeg")
        ffprobe_path = os.path.join(tb, "ffprobe")

if not os.path.isfile(ffmpeg_path):
    ffmpeg_path = shutil.which("ffmpeg") or ffmpeg_path
if not os.path.isfile(ffprobe_path):
    ffprobe_path = shutil.which("ffprobe") or ffprobe_path

os.environ["PATH"] = os.path.dirname(ffmpeg_path) + os.pathsep + os.environ.get("PATH", "")
os.environ["FFMPEG_BINARY"] = ffmpeg_path
os.environ["FFPROBE_BINARY"] = ffprobe_path

print(f"[INFO] Using ffmpeg:  {ffmpeg_path}")
print(f"[INFO] Using ffprobe: {ffprobe_path}")

# ── 2) 環境変数読み込み ─────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE")
DEPLOYMENT_ID = os.getenv("DEPLOYMENT_ID")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
TEMPERATURE = float(os.getenv("TEMPERATURE", 0.7))

openai.api_key = OPENAI_API_KEY
openai.api_base = OPENAI_API_BASE
openai.api_type = "azure"
openai.api_version = "2025-01-01-preview"

deepgram_client = Deepgram(DEEPGRAM_API_KEY)
TMP_DIR = tempfile.gettempdir()

# ★ 安全弁：短い語→長い語への「拡張置換」を防ぐ（誤登録吸収）
#   例: target="フレンドサニ" corr="フレンドサニタリー" を誤って「短→長」にしてしまった時に暴発しやすい
DISABLE_EXPANSION_REPLACEMENT = os.getenv("DISABLE_EXPANSION_REPLACEMENT", "1").strip().lower() in (
    "1", "true", "yes", "on"
)

# ──────────────────────────────────────────────────────────────
# 音声 → Deepgram → OpenAI 整形
# ──────────────────────────────────────────────────────────────
async def _transcribe_chunk(job_id: str, idx: int, chunk_path: str) -> str:
    """1チャンクの書き起こし（冪等ガードつき）"""
    wav_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}_chunk_{idx}.wav")
    subprocess.run(
        [ffmpeg_path, "-y", "-i", chunk_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
        check=True,
    )

    with open(wav_path, "rb") as f:
        buf = f.read()

    # 一時ファイルは極力消す（失敗しても続行）
    try:
        os.remove(wav_path)
    except Exception:
        pass
    try:
        os.remove(chunk_path)
    except Exception:
        pass

    # ★ 冪等：最初の1回だけ通す（重送・重課金を遮断）
    if not mark_once(job_id, idx):
        print(f"[INFO] Skip duplicated send: job={job_id} chunk={idx}", file=sys.stderr, flush=True)
        return ""

    try:
        resp = await deepgram_client.transcription.prerecorded(
            {"buffer": buf, "mimetype": "audio/wav"},
            {"model": "nova-2-general", "detect_language": True, "diarize": True, "utterances": True},
        )
    except Exception as e:
        # 失敗したらフラグ解除（再送を許可）
        unmark(job_id, idx)
        print(f"[ERROR] Deepgram failed: job={job_id} chunk={idx} err={e}", file=sys.stderr, flush=True)
        raise

    uts = resp.get("results", {}).get("utterances", []) or []
    return "\n".join(f"[Speaker {u.get('speaker')}] {u.get('transcript')}" for u in uts)


async def transcribe_and_correct(source: str, email: str | None = None) -> str:
    """音声を書き起こし、整形し、キーワード置換を行う（email対応版）"""
    # 1) URL判定 & ダウンロード
    if source.lower().startswith("http"):
        parsed = urlparse(source)
        safe_path = "/".join(quote(p) for p in parsed.path.split("/"))
        safe_url = f"{parsed.scheme}://{parsed.netloc}{safe_path}"
        if parsed.query:
            safe_url += f"?{parsed.query}"
        ext = os.path.splitext(parsed.path)[1]
        local_audio = os.path.join(TMP_DIR, f"{uuid.uuid4()}{ext}")
        download_blob(safe_url, local_audio)
        base_key = safe_url
    else:
        local_audio = source
        base_key = os.path.abspath(source)

    # ★ 同一音源で安定する job_id（冪等フラグのキー）
    job_id = uuid.uuid5(uuid.NAMESPACE_URL, base_key).hex[:16]

    # 2) Fast-Start 適用
    ext = os.path.splitext(local_audio)[1]
    fixed = os.path.join(TMP_DIR, f"{uuid.uuid4()}_fixed{ext}")
    subprocess.run(
        [ffmpeg_path, "-y", "-i", local_audio, "-c", "copy", "-movflags", "+faststart", fixed],
        check=True,
    )

    # 3) 長さ取得 (秒)
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        fixed,
    ]
    duration = float(subprocess.check_output(cmd).strip())
    chunk_len = 10 * 60  # 10分
    overlap = 1 * 60     # 1分
    step = chunk_len - overlap

    # 4) ffmpeg でチャンク分割
    chunk_paths: list[tuple[int, str]] = []
    start = 0.0
    idx = 0
    while start < duration:
        out_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}_seg_{idx}.mp4")
        subprocess.run(
            [ffmpeg_path, "-y", "-ss", str(start), "-t", str(min(chunk_len, duration - start)), "-i", fixed, "-c", "copy", out_path],
            check=True,
        )
        chunk_paths.append((idx, out_path))
        idx += 1
        start += step

    # 5) 並列送信 → 整形AI
    corrected: list[str] = []
    for i in range(0, len(chunk_paths), 6):
        tasks = [_transcribe_chunk(job_id, cidx, path) for cidx, path in chunk_paths[i: i + 6]]
        results = await asyncio.gather(*tasks)

        for text in results:
            if not text:
                continue  # duplicated / 空は無視

            prompt = (
                "以下の音声書き起こしを自然な日本語にしてください。\n\n"
                f"{text}\n\n"
                "【出力形式】\n[Speaker X] 発話内容\n[Speaker X] 発話内容\n"
            )
            resp = openai.ChatCompletion.create(
                engine=DEPLOYMENT_ID,
                messages=[
                    {"role": "system", "content": "あなたは日本語整形アシスタントです。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_completion_tokens=4000,
            )
            corrected.append(resp.choices[0].message.content)

    full = "\n".join(corrected)

    # クリーンアップ
    if source.lower().startswith("http"):
        try:
            os.remove(local_audio)
        except Exception:
            pass
    try:
        os.remove(fixed)
    except Exception:
        pass

    # ★ 置換ステップ（ユーザー別）
    replaced_text, hit = _apply_keyword_replacements(full, email=email)

    # ★ Azure Functions ログに出力
    logger.info(f"[KEYWORD] replace hit = {hit}, email={email}")

    return replaced_text


# ─── キーワード管理 / Blob 連携（ユーザー別） ─────────────────────
LEGACY_BLOB_JSON_PATH = "settings/keywords.json"
KEYWORDS_BLOB_PREFIX = "settings/keywords/users"

_KEYWORDS_CACHE: dict[str, list[dict]] = {}
_KEYWORDS_LOADED_AT: dict[str, str] = {}


def _safe_email(email: str) -> str:
    """URL-safe Base64（末尾の=は落とす）"""
    b = base64.urlsafe_b64encode(email.strip().lower().encode("utf-8")).decode("ascii")
    return b.rstrip("=")


def _keywords_blob_path(email: str | None) -> str:
    """
    個人辞書パス:
      settings/keywords/users/<base64(email)>/keywords.json
    ない場合は legacy:
      settings/keywords.json
    """
    if not email:
        return LEGACY_BLOB_JSON_PATH
    return f"{KEYWORDS_BLOB_PREFIX}/{_safe_email(email)}/keywords.json"


def _cache_key(email: str | None) -> str:
    return email.strip().lower() if email else "__legacy__"


def _split_targets(reading: str, wrong_examples: str) -> list[str]:
    tgts: list[str] = []
    r = (reading or "").strip()
    if r:
        tgts.append(r)
    we = (wrong_examples or "").strip()
    if we:
        tgts += [e.strip() for e in re.split(r"[,\uFF0C\u3001]", we) if e.strip()]
    # 重複除去（順序維持）
    tgts = [t for t in dict.fromkeys(tgts) if t]
    return tgts


def load_keywords_from_file(email: str | None = None) -> list[dict]:
    """
    email が指定された場合は個人辞書を優先してロード。
    取得できなければ legacy(settings/keywords.json) を試す。
    それも無ければローカル keywords.json があればそれを使う。
    """
    ck = _cache_key(email)
    if ck in _KEYWORDS_CACHE:
        return _KEYWORDS_CACHE[ck]

    # まず個人辞書（emailありの場合）→ 失敗したら legacy へフォールバック
    primary_blob_path = _keywords_blob_path(email)
    fallback_blob_path = LEGACY_BLOB_JSON_PATH

    def _try_download(blob_path: str, tmp_path: str) -> bool:
        try:
            download_blob(blob_path, tmp_path)
            logger.info(f"[KEYWORD] Blob download OK → {tmp_path} ({blob_path})")
            return True
        except Exception as e:
            logger.info(f"[KEYWORD] Blob download skipped: {e} ({blob_path})")
            return False

    try:
        tmp = os.path.join(
            TMP_DIR,
            f"keywords_{_safe_email(email) if email else 'legacy'}.json"
        )
        Path(tmp).parent.mkdir(exist_ok=True, parents=True)

        downloaded = _try_download(primary_blob_path, tmp)

        if (not downloaded) and email:
            downloaded = _try_download(fallback_blob_path, tmp)

        candidate = tmp
        if not downloaded:
            local_json = os.path.abspath("keywords.json")
            if os.path.exists(local_json):
                candidate = local_json

        if not os.path.exists(candidate) or os.path.getsize(candidate) == 0:
            logger.warning(f"[KEYWORD] empty/not found → start empty ({candidate}) email={email}")
            _KEYWORDS_CACHE[ck] = []
            _KEYWORDS_LOADED_AT[ck] = datetime.now().isoformat()
            return _KEYWORDS_CACHE[ck]

        with open(candidate, encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            logger.warning(f"[KEYWORD] invalid type (not list) → start empty type={type(data)} email={email}")
            data = []

        _KEYWORDS_CACHE[ck] = data
        _KEYWORDS_LOADED_AT[ck] = datetime.now().isoformat()
        logger.info(f"[KEYWORD] loaded {len(data)} keywords for email={email} ({candidate})")
        if data:
            logger.info(f"[KEYWORD] SAMPLE: {data[:2]}")
        return data

    except Exception as e:
        logger.warning(f"[KEYWORD] load failed: {e} email={email}")
        _KEYWORDS_CACHE[ck] = []
        _KEYWORDS_LOADED_AT[ck] = datetime.now().isoformat()
        return _KEYWORDS_CACHE[ck]


def _save_keywords_to_blob(email: str | None = None) -> None:
    ck = _cache_key(email)
    blob_path = _keywords_blob_path(email)
    kwdb = _KEYWORDS_CACHE.get(ck, [])

    try:
        tmp = os.path.join(
            TMP_DIR,
            f"keywords_{_safe_email(email) if email else 'legacy'}.json"
        )
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(kwdb, f, ensure_ascii=False, indent=2)

        with open(tmp, "rb") as f:
            # add_audio_prefix=False：settings/ 配下なので audio/ を付けない
            upload_to_blob(blob_path, f, add_audio_prefix=False)

        logger.info(f"[KEYWORD] saved keywords email={email} -> {blob_path}")
    except Exception as e:
        logger.error(f"[KEYWORD] save failed: {e} email={email} path={blob_path}")


def _apply_keyword_replacements(text: str, email: str | None = None) -> tuple[str, int]:
    """
    テキストに対してキーワード置換を実行し、置換後テキストとヒット数を返します。
    email が指定された場合は、そのユーザー辞書をロードして適用します。

    ★安全弁:
      - target == corr は無視
      - corr が target を含み、かつ corr の方が長い場合（=拡張置換）はデフォルト無効
      - target は長い順に適用（部分一致で壊れにくい）
    """
    kwdb = load_keywords_from_file(email)
    logger.info(f"[KEYWORD] loaded {len(kwdb)} keywords for email={email}")

    total_hit = 0
    for kw in kwdb:
        corr = (kw.get("keyword", "") or "").strip()
        if not corr:
            continue

        reading = kw.get("reading", "")
        wrong_examples = kw.get("wrong_examples", "")

        targets = _split_targets(reading, wrong_examples)

        # 自己置換は除外
        targets = [t for t in targets if t and t != corr]

        # ★ 拡張置換を防ぐ（誤登録対策）
        if DISABLE_EXPANSION_REPLACEMENT:
            safe_targets = []
            for t in targets:
                if len(corr) > len(t) and (t in corr):
                    logger.warning(
                        f"[KEYWORD] skip expansion replacement: target='{t}' -> corr='{corr}' email={email}"
                    )
                    continue
                safe_targets.append(t)
            targets = safe_targets

        # 長いターゲットから置換（部分一致の暴発を減らす）
        targets.sort(key=len, reverse=True)

        for t in targets:
            pat = re.compile(re.escape(t), flags=re.IGNORECASE)
            text, n_hits = pat.subn(corr, text)
            if n_hits > 0:
                logger.info(f"[KEYWORD] '{t}' -> '{corr}' : {n_hits} hits")
            total_hit += n_hits

    logger.info(f"[KEYWORD] total replace hit = {total_hit} email={email}")
    return text, total_hit


# -------------------- CRUD（ユーザー別） ---------------------------------

def get_all_keywords(email: str | None = None) -> list[dict]:
    return load_keywords_from_file(email)


def get_keyword_by_id(id: str, email: str | None = None) -> dict | None:
    kwdb = load_keywords_from_file(email)
    return next((k for k in kwdb if k.get("id") == id), None)


def add_keyword(reading: str, wrong_examples: str, keyword: str, email: str | None = None) -> None:
    ck = _cache_key(email)
    kwdb = load_keywords_from_file(email)

    before = len(kwdb)
    kwdb.append({
        "id": str(uuid.uuid4()),
        "reading": reading,
        "wrong_examples": wrong_examples,
        "keyword": keyword,
    })
    after = len(kwdb)

    _KEYWORDS_CACHE[ck] = kwdb
    logger.info(f"[ADD] keywords {before} → {after} email={email}")
    _save_keywords_to_blob(email)


def delete_keyword_by_id(id: str, email: str | None = None) -> None:
    ck = _cache_key(email)
    kwdb = load_keywords_from_file(email)

    before = len(kwdb)
    kwdb = [k for k in kwdb if k.get("id") != id]
    after = len(kwdb)

    _KEYWORDS_CACHE[ck] = kwdb
    logger.info(f"[DEL] keywords {before} → {after} email={email}")
    _save_keywords_to_blob(email)


def update_keyword_by_id(id: str, reading: str, wrong_examples: str, keyword: str, email: str | None = None) -> None:
    ck = _cache_key(email)
    kwdb = load_keywords_from_file(email)

    for k in kwdb:
        if k.get("id") == id:
            k["reading"] = reading
            k["wrong_examples"] = wrong_examples
            k["keyword"] = keyword
            logger.info(f"[UPDATE] keyword id={id} updated email={email}")
            break

    _KEYWORDS_CACHE[ck] = kwdb
    _save_keywords_to_blob(email)
