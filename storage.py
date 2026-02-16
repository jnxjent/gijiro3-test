import os
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from azure.core.pipeline.transport import RequestsTransport
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)
from azure.storage.queue import QueueServiceClient
from azure.core.exceptions import ResourceExistsError, HttpResponseError

# ── .env 読み込み ───────────────────────────────
load_dotenv()

AZ_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
AZ_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
AZ_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER_NAME")
QUEUE_NAME = os.getenv("AZURE_QUEUE_NAME", "audio-processing")

# ── ロガー ─────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("storage")

# ── クライアント初期化（タイムアウト拡大） ───────
transport = RequestsTransport(connection_timeout=600, read_timeout=600)
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
    return f"https://{AZ_ACCOUNT}.blob.core.windows.net/{AZ_CONTAINER}/{blob_name}"

def upload_to_blob(
    blob_name: str,
    file_stream,
    *,
    add_audio_prefix: bool = True,
    content_type: str = "application/octet-stream",
) -> str:
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

    if not client.exists():
        raise RuntimeError(f"Blob {blob_name} not found right after upload!")

    logger.info(f"Uploaded blob: {blob_name}")
    return generate_blob_url(blob_name)

def download_blob(blob_name_or_url: str, download_path: str) -> str:
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
    blob_name = _normalize_blob_name(blob_name, force_audio_prefix=True)
    sas_token = generate_blob_sas(
        account_name=AZ_ACCOUNT,
        container_name=AZ_CONTAINER,
        blob_name=blob_name,
        account_key=blob_service_client.credential.account_key,
        permission=BlobSasPermissions(read=True, write=True, create=True),
        expiry=datetime.utcnow() + timedelta(hours=expiry_hours),
    )
    url = generate_blob_url(blob_name)
    return {"uploadUrl": f"{url}?{sas_token}", "blobUrl": url}

def enqueue_processing(blob_url: str, template_blob_url: str, job_id: str, email: str | None = None) -> None:


    """
    明示的に audio-processing キューへメッセージを送信。
    - メッセージはそのまま JSON テキストで送信
    - キューがなければ自動作成
    - send_message には余計なキーワードを渡さない
    """
    try:
        # キュー自動作成
        try:
            queue_client.create_queue()
        except ResourceExistsError:
            pass

        # 生の JSON テキストをそのまま送信
        payload = json.dumps({
            "job_id": job_id,
            "blob_url": blob_url,
            "template_blob_url": template_blob_url,
            "email": email,  # ★ この行を追加
        })
        queue_client.send_message(payload)
        logger.info(f"Enqueued job {job_id} to '{QUEUE_NAME}'")
    except HttpResponseError as e:
        logger.error(f"Failed to enqueue job {job_id}: {e}", exc_info=True)
        raise
