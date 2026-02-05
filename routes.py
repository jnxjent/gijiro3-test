# routes.py
from flask import request, render_template, jsonify, redirect, send_file, abort
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
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

# ★ 追加：.env の容量制限を index に渡すため
from config import MAX_CONTENT_LENGTH_BYTES
import ipaddress

# 環境変数から許可IPリストを取得（カンマ区切り、CIDR対応）
ALLOWED_DOWNLOAD_IPS = [
    ip.strip()
    for ip in os.getenv("ALLOWED_DOWNLOAD_IPS", "").split(",")
    if ip.strip()
]

def ip_allowed(client_ip: str, allowed_list: list[str]) -> bool:
    """単一IP・複数IP・CIDR すべて対応"""
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

def setup_routes(app):
    logger = logging.getLogger("routes")
    logging.basicConfig(level=logging.INFO)
    logger.info("✔ setup_routes() 開始")

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

            # ポート番号除去（IPv4:PORT のみ）
            if client_ip and "." in client_ip and client_ip.count(":") == 1:
                client_ip = client_ip.rsplit(":", 1)[0]

            logger.warning(
                f"[IP DEBUG] client_ip={client_ip}, XFF={forwarded_for}, AppServiceIP={appservice_ip}"
            )

            if not ip_allowed(client_ip, ALLOWED_DOWNLOAD_IPS):
                abort(403, "社外からのダウンロードは許可されていません")

    # キーワードDBの初期ロード
    load_keywords_from_file()

    # ─── トップページ ───────────────────────────
    @app.route("/", methods=["GET"])
    def index():
        logger.info("✔ / にアクセスされました")
        # ★ ここに max_bytes を追加するだけ（サーバー側の挙動は不変）
        return render_template("index.html", max_bytes=MAX_CONTENT_LENGTH_BYTES)

    # ─── ヘルスチェック（/health と /healthz を両方用意） ───
    @app.route("/health", methods=["GET"])
    def health():
        logger.info("✔ /health にアクセス")
        return jsonify({"status": "OK"}), 200

    @app.route("/healthz", methods=["GET"])
    def healthz():
        logger.info("✔ /healthz にアクセス")
        return jsonify({"status": "OK"}), 200

    # ─── 結果ページ（静的テンプレート表示） ─────────────
    @app.route("/results/<job_id>", methods=["GET"])
    def result_page(job_id):
        return render_template("result.html", job_id=job_id)

    # ─── Azure AD コールバック（ダミー） ───────────────
    @app.route("/api/auth/callback/azure-ad", methods=["GET", "POST"])
    def azure_ad_callback():
        try:
            code = request.args.get("code")
            state = request.args.get("state")
            error = request.args.get("error")

            if error:
                logger.error(f"認証エラー: {error}")
                return jsonify({"error": error}), 400
            if not code:
                logger.error("認証コードがありません")
                return jsonify({"error": "認証コードがありません"}), 400

            logger.info(f"Azure AD 認証成功！code={code}, state={state}")
            return jsonify({
                "message": "Azure AD 認証成功！",
                "code": code,
                "state": state
            })
        except Exception as e:
            logger.error(f"エラー発生: {e}")
            return jsonify({"error": f"エラー発生: {e}"}), 500

    # ─── Blob SAS 発行 ────────────────────────
    @app.route("/api/blob/sas", methods=["GET"])
    def api_blob_sas():
        blob_name = request.args.get("name")
        if not blob_name:
            logger.error("SAS URL 生成エラー: name パラメーターがありません")
            return jsonify({"error": "name parameter is required"}), 400
        sas_info = generate_upload_sas(blob_name)
        return jsonify(sas_info)

    # ─── 非同期ジョブ登録 ─────────────────────
    @app.route("/api/process", methods=["POST"])
    def api_process():
        data = request.get_json(silent=True) or {}
        blob_url = data.get("blobUrl")
        template_blob_url = data.get("templateBlobUrl")

        if not blob_url or not template_blob_url:
            logger.error("ジョブ登録エラー: blobUrl または templateBlobUrl が不足")
            return jsonify({"error": "blobUrl and templateBlobUrl are required"}), 400

        job_id = uuid.uuid4().hex
        enqueue_processing(blob_url, template_blob_url, job_id)
        logger.info(f"✔ ジョブ登録完了: job_id={job_id}")
        return jsonify({"jobId": job_id}), 202

    # ─── ステータス確認 ───────────────────────
    @app.route("/api/process/<job_id>/status", methods=["GET"])
    def api_status(job_id):
        result_blob = f"processed/{job_id}.docx"
        try:
            blob_client = BlobClient.from_connection_string(
                os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
                os.getenv("AZURE_STORAGE_CONTAINER_NAME"),
                result_blob
            )
            if blob_client.exists():
                return jsonify({"status": "Completed", "resultUrl": blob_client.url}), 200
            else:
                return jsonify({"status": "Processing"}), 202
        except Exception as e:
            logger.error(f"ステータス確認中にエラー: {e}")
            return jsonify({"error": str(e)}), 500

    # ─── 同期で待つ（必要なら利用） ─────────────────
    @app.route("/api/process/<job_id>/wait", methods=["GET"])
    def api_wait_for_result(job_id):
        max_wait_sec = 600
        interval_sec = 5
        result_blob = f"processed/{job_id}.docx"

        blob_client = BlobClient.from_connection_string(
            os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
            os.getenv("AZURE_STORAGE_CONTAINER_NAME"),
            result_blob
        )

        elapsed = 0
        while elapsed < max_wait_sec:
            if blob_client.exists():
                local_path = Path("downloads") / f"{job_id}.docx"
                local_path.parent.mkdir(parents=True, exist_ok=True)
                with open(local_path, "wb") as f:
                    download_stream = blob_client.download_blob()
                    f.write(download_stream.readall())
                return send_file(local_path, as_attachment=True)

            time.sleep(interval_sec)
            elapsed += interval_sec

        return jsonify({"error": "処理が完了しませんでした"}), 504

    
  # ─── キーワード管理 ────────────────────────
    @app.route("/keywords", methods=["GET"])
    def keywords_page():
        keywords = get_all_keywords()
        print(f"🟡 /keywords loaded = {len(keywords)}")  # ログ
        return render_template("keywords.html", keywords=keywords)

    @app.route("/register_keyword", methods=["POST"])
    def register_keyword():
        reading = request.form.get("reading")
        wrong_examples = request.form.get("wrong_examples")
        keyword = request.form.get("keyword")

        before = len(get_all_keywords())
        print(f"🟢 register before = {before}")

        add_keyword(reading, wrong_examples, keyword)

        after = len(get_all_keywords())
        print(f"🟢 register after  = {after}")
        return redirect("/keywords")

    @app.route("/delete_keyword", methods=["POST"])
    def delete_keyword():
        keyword_id = request.form.get("id")

        before = len(get_all_keywords())
        print(f"🔴 delete  before = {before}")

        delete_keyword_by_id(keyword_id)

        after = len(get_all_keywords())
        print(f"🔴 delete  after  = {after}")
        return redirect("/keywords")

    @app.route("/edit_keyword", methods=["GET"])
    def edit_keyword():
        keyword_id = request.args.get("id")
        keyword = get_keyword_by_id(keyword_id)
        return render_template("edit_keyword.html", keyword=keyword)

    @app.route("/update_keyword", methods=["POST"])
    def update_keyword():
        keyword_id = request.form.get("id")
        reading = request.form.get("reading")
        wrong_examples = request.form.get("wrong_examples")
        keyword_text = request.form.get("keyword")

        update_keyword_by_id(keyword_id, reading, wrong_examples, keyword_text)
        return redirect("/keywords")

    # ─── エラーページ描画（フロントからの /error?code=... に対応） ───
    @app.route("/error", methods=["GET"])
    def error_page():
        code = request.args.get("code", default=500, type=int)
        message = request.args.get("message", default="")
        path = request.args.get("path", default=request.path)
        job_id = request.args.get("job_id")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return (
            render_template(
                "error.html",
                title="エラー",
                code=code,
                message=message,
                path=path,
                job_id=job_id,
                now=now,
            ),
            code,
        )

    # ─── 共通エラーハンドラ（サーバー起因の未捕捉も UI 化） ───
    @app.errorhandler(404)
    def _h_404(e):
        logger.error(f"404 Not Found: {request.path}")
        return render_template(
            "error.html",
            title="404 Not Found",
            code=404,
            message=str(e),
            path=request.path,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ), 404

    @app.errorhandler(413)
    def _h_413(e):
        logger.error(f"413 Payload Too Large: {request.path}")
        return render_template(
            "error.html",
            title="413 Payload Too Large",
            code=413,
            message="アップロード上限を超えています。",
            path=request.path,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ), 413
    
    @app.errorhandler(403)
    def _h_403(e):
        logger.error(f"403 Forbidden: {request.path}")
        return render_template(
            "error.html",
            title="403 Forbidden",
            code=403,
            message="社外からのダウンロードは許可されていません",
            path=request.path,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ), 403

    @app.errorhandler(500)
    def _h_500(e):
        logger.exception("500 Internal Server Error")
        return render_template(
            "error.html",
            title="500 Internal Server Error",
            code=500,
            message=str(e),
            path=request.path,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ), 500
    
    # ─── プロキシダウンロード（Blob URL を隠す）─────────────
    @app.route("/api/process/<job_id>/download", methods=["GET"])
    def api_download(job_id):
        """WEB中継方式: Blobから取得してそのままストリーム返却"""
        result_blob = f"processed/{job_id}.docx"
        try:
            blob_client = BlobClient.from_connection_string(
                os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
                os.getenv("AZURE_STORAGE_CONTAINER_NAME"),
                result_blob
            )
            if not blob_client.exists():
                return jsonify({"error": "ファイルが見つかりません"}), 404

            # ストリームで返却（メモリ効率良い）
            download_stream = blob_client.download_blob()
            
            from flask import Response
            return Response(
                download_stream.chunks(),
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={
                    "Content-Disposition": f"attachment; filename=gijiroku_{job_id}.docx"
                }
            )
        except Exception as e:
            logger.error(f"ダウンロード中にエラー: {e}")
            return jsonify({"error": str(e)}), 500

