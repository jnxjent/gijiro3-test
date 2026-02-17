#__init__.py
import logging

# ─── ロガー初期化 ─────────────────────────────────────────
logger = logging.getLogger("ProcessAudioFunction")
logger.setLevel(logging.INFO)
logger.info("▶▶ Module import start")

import os
import json
import base64
import tempfile
import subprocess
import uuid
from pathlib import Path
import shutil
import stat
from urllib.parse import urlparse

from pydub import AudioSegment
import azure.functions as func

from storage import download_blob, upload_to_blob
from extraction import extract_meeting_info_and_speakers
from docwriter import process_document

TMP_DIR = tempfile.gettempdir()
TMP_FFMPEG = os.path.join(TMP_DIR, "ffmpeg")
TMP_FFPROBE = os.path.join(TMP_DIR, "ffprobe")


def _is_file(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path)
    except Exception:
        return False


def _exec_succeeds(bin_path: str) -> bool:
    """実際に `-version` を叩いて実行可否を確認（noexec 検出にも有効）"""
    try:
        subprocess.check_output([bin_path, "-version"], stderr=subprocess.STDOUT, timeout=5)
        return True
    except Exception:
        return False


def _same_size(a: str, b: str) -> bool:
    try:
        return os.path.getsize(a) == os.path.getsize(b)
    except Exception:
        return False


def _ensure_tmp(src: str, dst: str) -> str:
    """
    src から /tmp の dst へ必要時のみコピー。+x を付与して原子的に置換。
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst) or not _same_size(src, dst):
        tmp_dst = dst + ".tmp"
        shutil.copyfile(src, tmp_dst)
        os.chmod(tmp_dst, os.stat(tmp_dst).st_mode | stat.S_IEXEC)
        os.replace(tmp_dst, dst)
    return dst


def _pick_first_existing(paths: list[str]) -> str:
    for p in paths:
        if _is_file(p):
            return p
    return ""


def _candidate_paths_from_env() -> tuple[list[str], list[str]]:
    """環境変数→既知配置の順で候補を並べる"""
    env_ff = os.environ.get("FFMPEG_BINARY") or os.environ.get("FFMPEG_PATH") or ""
    env_fp = os.environ.get("FFPROBE_BINARY") or os.environ.get("FFPROBE_PATH") or ""

    ffmpeg_candidates = [
        env_ff,
        "/home/site/wwwroot/ProcessAudioFunction/tools/ffmpeg",
        "/home/site/wwwroot/tools/ffmpeg",
        "/home/site/ffmpeg-bin/bin/ffmpeg",
        "/home/site/wwwroot/ffmpeg",
    ]
    ffprobe_candidates = [
        env_fp,
        "/home/site/wwwroot/ProcessAudioFunction/tools/ffprobe",
        "/home/site/wwwroot/tools/ffprobe",
        "/home/site/ffmpeg-bin/bin/ffprobe",
        "/home/site/wwwroot/ffprobe",
    ]
    # 空文字と重複を除去
    ffmpeg_candidates = [p for p in dict.fromkeys(ffmpeg_candidates) if p]
    ffprobe_candidates = [p for p in dict.fromkeys(ffprobe_candidates) if p]
    return ffmpeg_candidates, ffprobe_candidates


def resolve_ffmpeg_and_ffprobe() -> tuple[str, str]:
    """
    実行時に ffmpeg/ffprobe を解決。
    - 候補から source を見つける
    - 直接実行できるならそれを使う
    - できなければ /tmp に実体コピーしてそこを使う
    - 最後に `-version` で実行確認
    - PATH / pydub / 環境変数 (BINARY/ PATH) をすべて /tmp 実体に統一
    """
    ff_candidates, fp_candidates = _candidate_paths_from_env()
    logger.info(f"[ffmpeg-check] candidates ffmpeg={ff_candidates}")
    logger.info(f"[ffmpeg-check] candidates ffprobe={fp_candidates}")

    ff_src = _pick_first_existing(ff_candidates)
    fp_src = _pick_first_existing(fp_candidates)

    if not ff_src:
        logger.error("FFMPEG binary not found in candidates.")
        raise RuntimeError("FFMPEG binary not found.")
    if not fp_src:
        logger.error("FFPROBE binary not found in candidates.")
        raise RuntimeError("FFPROBE binary not found.")

    if _exec_succeeds(ff_src) and _exec_succeeds(fp_src):
        ff_bin, fp_bin = ff_src, fp_src
        logger.info("[ffmpeg-check] direct execute OK")
    else:
        logger.warning("[ffmpeg-check] direct execute failed -> fallback to /tmp")
        ff_bin = _ensure_tmp(ff_src, TMP_FFMPEG)
        fp_bin = _ensure_tmp(fp_src, TMP_FFPROBE)
        if not (_exec_succeeds(ff_bin) and _exec_succeeds(fp_bin)):
            logger.error("[ffmpeg-check] /tmp fallback also failed to execute")
            raise RuntimeError("ffmpeg/ffprobe not executable even after /tmp fallback")

    os.environ["FFMPEG_BINARY"] = ff_bin
    os.environ["FFPROBE_BINARY"] = fp_bin
    os.environ["FFMPEG_PATH"] = ff_bin
    os.environ["FFPROBE_PATH"] = fp_bin

    AudioSegment.converter = ff_bin
    AudioSegment.ffprobe = fp_bin

    bin_dir = str(Path(ff_bin).parent)
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

    try:
        out = (
            subprocess.check_output([ff_bin, "-version"], stderr=subprocess.STDOUT, timeout=5)
            .decode("utf-8", "ignore")
            .splitlines()[0]
        )
        logger.info(f"[ffmpeg-check] ffmpeg -version: {out}")
    except Exception:
        logger.warning("[ffmpeg-check] could not print ffmpeg version")

    logger.info(f"▶▶ Using FFMPEG:  {ff_bin}")
    logger.info(f"▶▶ Using FFPROBE: {fp_bin}")
    return ff_bin, fp_bin


def _maybe_base64_to_json(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Empty queue message")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    s = raw
    if len(s) < 8:
        raise ValueError("Queue message is neither JSON nor base64(JSON)")

    pad = (-len(s)) % 4
    if pad:
        s = s + ("=" * pad)

    decoded = base64.b64decode(s).decode("utf-8", errors="replace")
    return json.loads(decoded)


def _guess_ext_from_url(url: str, default: str = ".mp4") -> str:
    try:
        path = urlparse(url).path
        _, ext = os.path.splitext(path)
        if ext and len(ext) <= 8:
            return ext
    except Exception:
        pass
    return default


logger.info("▶▶ Module import success (ffmpeg resolution will run at invocation)")


# ─── Function 本体 ────────────────────────────────────────
async def main(msg: func.QueueMessage) -> None:
    logger.info("▶▶ Function invoked")

    try:
        FFMPEG_BIN, FFPROBE_BIN = resolve_ffmpeg_and_ffprobe()
    except Exception:
        logger.exception("▶▶ FFMPEG/FFPROBE resolution failed")
        raise

    from kowake import transcribe_and_correct

    raw = msg.get_body().decode("utf-8", errors="replace")
    logger.info("▶▶ RAW payload: %s", raw)

    local_audio = None
    fixed_audio = None
    template_path = None
    local_docx = None

    local_audio = None
    fixed_audio = None
    template_path = None
    local_docx = None

    try:
        body = _maybe_base64_to_json(raw)

        job_id = body["job_id"]
        blob_url = body["blob_url"]
        template_blob_url = body["template_blob_url"]
        logger.info(f"Received job {job_id}, blob: {blob_url}, template: {template_blob_url}")

        ext = _guess_ext_from_url(blob_url, default=".mp4")
        local_audio = os.path.join(TMP_DIR, f"{uuid.uuid4()}{ext}")
        logger.info(f"▶▶ STEP1-1: Downloading audio from {blob_url}")
        download_blob(blob_url, local_audio)
        logger.info(f"▶▶ STEP1-2: Audio downloaded to {local_audio}")

        fixed_audio = os.path.join(TMP_DIR, f"{uuid.uuid4()}_fixed.mp4")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", local_audio, "-c", "copy", "-movflags", "+faststart", fixed_audio],
            check=True,
            timeout=60,
        )
        logger.info(f"▶▶ STEP2: Faststart applied: {fixed_audio}")

        logger.info("▶▶ STEP3-1: Starting transcription")
        transcript = await transcribe_and_correct(fixed_audio, email=email)
        logger.info("▶▶ STEP3-2: Transcription completed")

        template_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}_template.docx")
        logger.info(f"▶▶ STEP4-1: Downloading template from {template_blob_url}")
        download_blob(template_blob_url, template_path)
        logger.info(f"▶▶ STEP4-2: Template downloaded to {template_path}")

        logger.info("▶▶ STEP5-1: Starting document processing")
        meeting_info = await extract_meeting_info_and_speakers(transcript, template_path)

        local_docx = os.path.join(TMP_DIR, f"{job_id}.docx")
        blob_docx  = f"processed/{job_id}.docx"
        process_document(template_path, local_docx, meeting_info)
        logger.info("▶▶ STEP5-2: Document processed")

        with open(local_docx, "rb") as fp:
            upload_to_blob(blob_docx, fp, add_audio_prefix=False)

        logger.info(f"Job {job_id} completed, saved to {blob_docx}")

    except Exception:
        logger.exception("Error processing job")
        raise

    finally:
        # 後片付け
        for var in ("local_audio", "fixed_audio", "template_path", "local_docx"):
            path = locals().get(var)
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass