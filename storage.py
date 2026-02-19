# storage.py
import os
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse
from typing import Optional

from dotenv import load_dotenv
from azure.core.pipeline.transport import RequestsTransport
from azure.core.exceptions import ResourceExistsError, HttpResponseError
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)
from azure.storage.queue import QueueServiceClient

# ── .env 読み込み ───────────────────────────────
load_dotenv()

AZ_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
AZ_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
AZ_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER_NAME")
QUEUE_NAME = os.getenv("AZURE_QUEUE_NAME", "audio-processing")

# ── ロガー ─────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("storage")

# ───────────────────────────────────────────────
# クライアント初期化（import時に落とさない）
#  - ただし実行時に未設定なら明示的に例外
# ───────────────────────────────────────────────
transport = RequestsTransport(connection_timeout=600, read_timeout=600)

blob_service_client: Optional[BlobServiceClient] = None
container_client = None
queue_service: Optional[QueueServiceClient] = None
queue_client = None


def _ensure_clients() -> None:
    """
    必要な環境変数が揃っているかチェックして、クライアントを初期化する。
    import時点で落とさず、初回利用時にだけ初期化する。
    """
    global blob_service_client, container_client, queue_service, queue_client

    if blob_service_client and container_client and queue_service and queue_client:
        return

    if not AZ_CONN_STR:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is not set.")
    if not AZ_ACCOUNT:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME is not set.")
    if not AZ_CONTAINER:
        raise RuntimeError("AZURE_STORAGE_CONTAINER_NAME is not set.")
    if not QUEUE_NAME:
        raise RuntimeError("AZURE_QUEUE_NAME is not set.")

    blob_service_client = BlobServiceClient.from_connection_string(AZ_CONN_STR, transport=transport)
    container_client = blob_service_client.get_container_client(AZ_CONTAINER)
    logger.info(f"Connected to Blob Storage: {AZ_ACCOUNT}/{AZ_CONTAINER}")

    queue_service = QueueServiceClient.from_connection_string(AZ_CONN_STR)
    queue_client = queue_service.get_queue_client(QUEUE_NAME)
    logger.info(f"Initialized Queue client for: {QUEUE_NAME}")


# ── ヘルパー関数 ────────────────────────────────
def _normalize_blob_name(blob_name: str, *, force_audio_prefix: bool = False) -> str:
    if force_audio_prefix and not blob_name.startswith("audio/"):
        return f"audio/{blob_name}"
    return blob_name


def _extract_blob_name_from_url(blob_url: str) -> str:
    parts = urlparse(blob_url).path.lstrip("/").split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid blob URL: {blob_url}")
    return unquote(parts[1])


# ── Public API ─────────────────────────────────
def generate_blob_url(blob_name: str) -> str:
    if not AZ_ACCOUNT or not AZ_CONTAINER:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME / AZURE_STORAGE_CONTAINER_NAME is not set.")
    return f"https://{AZ_ACCOUNT}.blob.core.windows.net/{AZ_CONTAINER}/{blob_name}"


def upload_to_blob(
    blob_name: str,
    file_stream,
    *,
    add_audio_prefix: bool = True,
    content_type: str = "application/octet-stream",
) -> str:
    _ensure_clients()

    blob_name = _normalize_blob_name(blob_name, force_audio_prefix=add_audio_prefix)
    client = container_client.get_blob_client(blob_name)

    try:
        client.upload_blob(
            file_stream,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
    except Exception:
        logger.exception(f"UPLOAD FAILED: {blob_name}")
        raise

    # 即時検証（たまに eventual 的に見えない時があるが、基本はここでOK）
    if not client.exists():
        raise RuntimeError(f"Blob {blob_name} not found right after upload!")

    logger.info(f"Uploaded blob: {blob_name}")
    return generate_blob_url(blob_name)


def download_blob(blob_name_or_url: str, download_path: str) -> str:
    _ensure_clients()

    if blob_name_or_url.startswith("http"):
        blob_name = _extract_blob_name_from_url(blob_name_or_url)
    else:
        blob_name = blob_name_or_url

    logger.info(f"Downloading blob {blob_name} to {download_path}")
    client = container_client.get_blob_client(blob_name)

    stream = client.download_blob()
    with open(download_path, "wb") as fp:
        for chunk in stream.chunks():
            fp.write(chunk)

    logger.info(f"Downloaded blob {blob_name} → {download_path}")
    return download_path


def generate_upload_sas(blob_name: str, expiry_hours: int = 1) -> dict:
    """
    アップロード用 SAS を返す。
    注意: connection string が account key を含まない（Managed Identity等）場合、
         generate_blob_sas の account_key が取れず失敗するので明示的にエラーにする。
    """
    _ensure_clients()

    blob_name = _normalize_blob_name(blob_name, force_audio_prefix=True)

    # connection string 由来 credential から account_key を取得（キー無し構成なら None になり得る）
    account_key = getattr(getattr(blob_service_client, "credential", None), "account_key", None)
    if not account_key:
        raise RuntimeError(
            "Cannot generate SAS because account_key is not available. "
            "Use a connection string that includes AccountKey, or switch SAS generation strategy."
        )

    sas_token = generate_blob_sas(
        account_name=AZ_ACCOUNT,
        container_name=AZ_CONTAINER,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True, write=True, create=True),
        expiry=datetime.utcnow() + timedelta(hours=expiry_hours),
    )
    url = generate_blob_url(blob_name)
    return {"uploadUrl": f"{url}?{sas_token}", "blobUrl": url}


def enqueue_processing(blob_url: str, template_blob_url: str, job_id: str, email: str | None = None) -> None:
    """
    明示的に audio-processing キューへメッセージを送信。
    - メッセージは JSON テキストで送信
    - キューがなければ自動作成
    - email があれば payload に含める（ユーザー別辞書適用用）
    """
    _ensure_clients()

    try:
        # キュー自動作成
        try:
            queue_client.create_queue()
        except ResourceExistsError:
            pass

        payload_obj = {
            "job_id": job_id,
            "blob_url": blob_url,
            "template_blob_url": template_blob_url,
        }
        if email:
            payload_obj["email"] = email.strip().lower()

        payload = json.dumps(payload_obj, ensure_ascii=False)
        queue_client.send_message(payload)
        logger.info(f"Enqueued job {job_id} to '{QUEUE_NAME}' email={email}")

    except HttpResponseError as e:
        logger.error(f"Failed to enqueue job {job_id}: {e}", exc_info=True)
        raise
