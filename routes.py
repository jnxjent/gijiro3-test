# 無限ループ回避版routes.py
from __future__ import annotations

from flask import request, render_template, jsonify, redirect, send_file, abort, Response
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
import ipaddress

from azure.storage.blob import BlobClient

from storage import generate_upload_sas, enqueue_processing
from kowake import (
    load_keywords_from_file,
    get_all_keywords,
    add_keyword,
    delete_keyword_by_id,
    get_keyword_by_id,
    update_keyword_by_id,
)

from config import MAX_CONTENT_LENGTH_BYTES


# ─────────────────────────────────────────────
# 許可IP設定
# ─────────────────────────────────────────────
ALLOWED_DOWNLOAD_IPS = [
    ip.strip()
    for ip in os.getenv("ALLOWED_DOWNLOAD_IPS", "").split(",")
    if ip.strip()
]


def ip_allowed(client_ip: str, allowed_list: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    for allowed in allowed_list:
        try:
            if ip in ipaddress.ip_network(allowed, strict=False):
                return True
        except ValueError:
            continue
    return False


# ─────────────────────────────────────────────
# ルート設定
# ─────────────────────────────────────────────
def setup_routes(app):
    logger = logging.getLogger("routes")
    logging.basicConfig(level=logging.INFO)
    logger.info("✔ setup_routes() 開始")

    # Flask標準設定
    app.config.setdefault("MAX_CONTENT_LENGTH", int(MAX_CONTENT_LENGTH_BYTES))

    # ─────────────────────────────────────────
    # EasyAuth メール取得
    # ─────────────────────────────────────────
    def _get_user_email() -> str | None:
        principal = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
        if principal:
            return principal.strip().lower()

        hdr = request.headers.get("X-User-Email") or request.headers.get("X-Email")
        if hdr:
            return hdr.strip().lower()

        email = request.args.get("email") or request.form.get("email")
        return (email or "").strip().lower() or None

    # ─────────────────────────────────────────
    # IP制限
    # ─────────────────────────────────────────
    @app.before_request
    def restrict_download_by_ip():
        if request.path.startswith("/api/process/") and request.path.endswith("/download"):
            if not ALLOWED_DOWNLOAD_IPS:
                return

            forwarded_for = request.headers.get("X-Forwarded-For")
            appservice_ip = request.headers.get("X-AppService-Client-IP")

            if appservice_ip:
                client_ip = appservice_ip.strip()
            elif forwarded_for:
                client_ip = forwarded_for.split(",")[0].strip()
            else:
                client_ip = request.remote_addr

            if client_ip and "." in client_ip and client_ip.count(":") == 1:
                client_ip = client_ip.rsplit(":", 1)[0]

            if not ip_allowed(client_ip, ALLOWED_DOWNLOAD_IPS):
                abort(403, "社外からのダウンロードは許可されていません")

    # キーワードDBロード
    load_keywords_from_file()

    # ─────────────────────────────────────────
    # 基本ページ
    # ─────────────────────────────────────────
    @app.route("/")
    def index():
        return render_template("index.html", max_bytes=MAX_CONTENT_LENGTH_BYTES)

    @app.route("/health")
    def health():
        return jsonify({"status": "OK"}), 200

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "OK"}), 200

    @app.route("/results/<job_id>")
    def result_page(job_id):
        return render_template("result.html", job_id=job_id)

    # ─────────────────────────────────────────
    # SAS発行
    # ─────────────────────────────────────────
    @app.route("/api/blob/sas")
    def api_blob_sas():
        blob_name = request.args.get("name")
        if not blob_name:
            return jsonify({"error": "name parameter is required"}), 400
        return jsonify(generate_upload_sas(blob_name))

    # ─────────────────────────────────────────
    # ジョブ登録（★メール対応）
    # ─────────────────────────────────────────
    @app.route("/api/process", methods=["POST"])
    def api_process():
        data = request.get_json(silent=True) or {}
        blob_url = data.get("blobUrl")
        template_blob_url = data.get("templateBlobUrl")

        if not blob_url or not template_blob_url:
            return jsonify({"error": "blobUrl and templateBlobUrl are required"}), 400

        email = _get_user_email() or (data.get("email") or "").strip().lower() or None

        logger.info("✔ ジョブ登録 email=%s", email)

        job_id = uuid.uuid4().hex
        enqueue_processing(blob_url, template_blob_url, job_id, email=email)

        return jsonify({"jobId": job_id}), 202

    # ─────────────────────────────────────────
    # ステータス確認
    # ─────────────────────────────────────────
    @app.route("/api/process/<job_id>/status")
    def api_status(job_id):
        result_blob = f"processed/{job_id}.docx"
        blob_client = BlobClient.from_connection_string(
            os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
            os.getenv("AZURE_STORAGE_CONTAINER_NAME"),
            result_blob,
        )
        if blob_client.exists():
            return jsonify({"status": "Completed", "resultUrl": blob_client.url}), 200
        return jsonify({"status": "Processing"}), 202

    # ─────────────────────────────────────────
    # ダウンロード
    # ─────────────────────────────────────────
    @app.route("/api/process/<job_id>/download")
    def api_download(job_id):
        result_blob = f"processed/{job_id}.docx"
        blob_client = BlobClient.from_connection_string(
            os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
            os.getenv("AZURE_STORAGE_CONTAINER_NAME"),
            result_blob,
        )
        if not blob_client.exists():
            return jsonify({"error": "ファイルが見つかりません"}), 404

        download_stream = blob_client.download_blob()
        return Response(
            download_stream.chunks(),
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename=gijiroku_{job_id}.docx"},
        )

    # ─────────────────────────────────────────
    # キーワード管理
    # ─────────────────────────────────────────
    @app.route("/keywords")
    def keywords_page():
        return render_template("keywords.html", keywords=get_all_keywords())

    @app.route("/register_keyword", methods=["POST"])
    def register_keyword():
        add_keyword(
            request.form.get("reading"),
            request.form.get("wrong_examples"),
            request.form.get("keyword"),
        )
        return redirect("/keywords")

    @app.route("/delete_keyword", methods=["POST"])
    def delete_keyword():
        delete_keyword_by_id(request.form.get("id"))
        return redirect("/keywords")

    @app.route("/edit_keyword")
    def edit_keyword():
        return render_template("edit_keyword.html", keyword=get_keyword_by_id(request.args.get("id")))

    @app.route("/update_keyword", methods=["POST"])
    def update_keyword():
        update_keyword_by_id(
            request.form.get("id"),
            request.form.get("reading"),
            request.form.get("wrong_examples"),
            request.form.get("keyword"),
        )
        return redirect("/keywords")

    # ─────────────────────────────────────────
    # エラーハンドラ
    # ─────────────────────────────────────────
    @app.errorhandler(404)
    def _h_404(e):
        return render_template("error.html", code=404, message=str(e)), 404

    @app.errorhandler(413)
    def _h_413(e):
        return render_template("error.html", code=413, message="アップロード上限を超えています。"), 413

    @app.errorhandler(403)
    def _h_403(e):
        return render_template("error.html", code=403, message="社外からのダウンロードは許可されていません"), 403

    @app.errorhandler(500)
    def _h_500(e):
        return render_template("error.html", code=500, message=str(e)), 500
