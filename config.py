# config.py
import os
from dotenv import load_dotenv

# .env を読み込む
load_dotenv()

def _int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip().split()[0])
    except Exception:
        return default

def _float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip().split()[0])
    except Exception:
        return default

def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

# ==== Upload size limit (全体) ====
MAX_CONTENT_LENGTH_BYTES = _int("MAX_CONTENT_LENGTH_BYTES", 104_857_600)  # 100MB 既定

# ==== Deepgram safety knobs（kowake.py と揃える）====
DG_TIMEOUT_TOTAL_SEC = _int("DG_TIMEOUT_TOTAL_SEC", 240)
DG_MAX_RETRIES       = _int("DG_MAX_RETRIES", 1)
DG_BACKOFF_BASE_SEC  = _float("DG_BACKOFF_BASE_SEC", 2.0)
DG_PARALLEL          = _int("DG_PARALLEL", 3)
CHUNK_LEN_SEC        = _int("CHUNK_LEN_SEC", 480)
CHUNK_OVERLAP_SEC    = _int("CHUNK_OVERLAP_SEC", 30)

# ==== 旧フロー: 本体POSTアップロード（使わないなら False）====
USE_BODY_UPLOAD       = _bool("USE_BODY_UPLOAD", False)

# 本体POSTアップロードの上限（未設定なら全体上限に揃える）
BODY_UPLOAD_MAX_BYTES = _int("BODY_UPLOAD_MAX_BYTES", MAX_CONTENT_LENGTH_BYTES)

# （将来用）フォームのフィールド名（必要なら .env で上書き）
BODY_FIELD_NAME       = os.getenv("BODY_FIELD_NAME", "file")
