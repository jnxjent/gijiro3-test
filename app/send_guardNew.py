# app/send_guard.py
import os
from typing import Optional
from azure.storage.blob import BlobClient, BlobServiceClient
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

# ── 設定（存在すれば使う。無ければ既定を使う） ───────────────────────────────
CONN = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
ACCOUNT = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
KEY = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
CONTAINER = os.environ.get("SENT_FLAG_CONTAINER", "sentflags")

# verbose: 1=ログ出す / 0=出さない（本番で静かにしたい時向け）
VERBOSE = (os.environ.get("SEND_GUARD_VERBOSE", "1") not in ("0", "false", "False"))

def _log(msg: str) -> None:
    if VERBOSE:
        print(f"[send_guard] {msg}", flush=True)

def _blob_client(job_id: str, chunk_index: int) -> BlobClient:
    blob_name = f"{job_id}/{chunk_index}.sent"
    # 接続方式の決定
    if CONN:
        _log("use connection_string")
        svc = BlobServiceClient.from_connection_string(CONN)
    else:
        # 既存環境が name/key だけの場合に対応
        if not (ACCOUNT and KEY):
            raise RuntimeError(
                "Storage credentials not found. "
                "Set AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_NAME / AZURE_STORAGE_ACCOUNT_KEY."
            )
        url = f"https://{ACCOUNT}.blob.core.windows.net"
        _log(f"use account/key: account={ACCOUNT}")
        svc = BlobServiceClient(account_url=url, credential=KEY)

    # コンテナ作成（冪等）
    try:
        svc.create_container(CONTAINER)
        _log(f"container created: {CONTAINER}")
    except ResourceExistsError:
        _log(f"container exists: {CONTAINER}")

    _log(f"blob target -> container={CONTAINER}, name={blob_name}")
    return svc.get_blob_client(CONTAINER, blob_name)


def mark_once(job_id: str, chunk_index: int) -> bool:
    """
    送信前に呼ぶ。最初の1回だけ True を返し、以降は False。
    - True: フラグ新規作成（＝この送信は“初回”として実行してOK）
    - False: 既にフラグあり（＝重複送信はスキップ推奨）
    """
    bc = _blob_client(job_id, chunk_index)
    dedupe_key = f"{job_id}:{chunk_index}"
    try:
        # 0バイトを「存在フラグ」としてアップロード。既存ならエラー（=重複）
        bc.upload_blob(b"", overwrite=False)
        _log(f"SEND (first time): key={dedupe_key} -> flag created")
        return True
    except ResourceExistsError:
        _log(f"SKIP duplicate: key={dedupe_key} -> flag already exists")
        return False


def unmark(job_id: str, chunk_index: int) -> None:
    """
    送信失敗時に呼ぶ。フラグを取り消し、再送を許可。
    """
    bc = _blob_client(job_id, chunk_index)
    dedupe_key = f"{job_id}:{chunk_index}"
    try:
        bc.delete_blob()
        _log(f"UNMARK: key={dedupe_key} -> flag removed")
    except ResourceNotFoundError:
        _log(f"UNMARK: key={dedupe_key} -> flag not found (noop)")
