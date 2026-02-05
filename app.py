#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py – Flask エントリポイント（最小修正版）
・ffmpeg / ffprobe の実行ファイルを OS 別に設定
・pydub が参照する環境変数を上書き
・CORS, ルーティング, ログ初期化, エラーハンドラ, waitress 起動
"""

import os
import platform
import logging
from dotenv import load_dotenv
from pydub import AudioSegment
from flask import Flask, render_template, request
from flask_cors import CORS
from jinja2 import TemplateNotFound as JinjaTemplateNotFound
from werkzeug.exceptions import NotFound

# ==== .env 読み込み ====
load_dotenv()

# ==== config（.env 反映の集約） ====
from config import (
    USE_BODY_UPLOAD,
    BODY_UPLOAD_MAX_BYTES,
)

# ==== ffmpeg / ffprobe 実行ファイルパス設定 ====
if platform.system() == "Windows":
    FFMPEG_BIN = os.getenv("FFMPEG_PATH_WIN", os.getenv("FFMPEG_PATH", "ffmpeg"))
    FFPROBE_BIN = os.getenv("FFPROBE_PATH_WIN", os.getenv("FFPROBE_PATH", "ffprobe"))
else:
    FFMPEG_BIN = os.getenv("FFMPEG_PATH_UNIX", "/home/site/ffmpeg-bin/bin/ffmpeg")
    FFPROBE_BIN = os.getenv("FFPROBE_PATH_UNIX", "/home/site/ffmpeg-bin/bin/ffprobe")

# pydub に実ファイルを認識させる（環境変数も上書き）
AudioSegment.converter = FFMPEG_BIN
AudioSegment.ffprobe = FFPROBE_BIN
os.environ["FFMPEG_BINARY"] = FFMPEG_BIN
os.environ["FFPROBE_BINARY"] = FFPROBE_BIN

# ==== Flask アプリ生成（template_folder を明示）====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
app = Flask(__name__, template_folder=TEMPLATE_DIR)
CORS(app)

# ==== アップロード上限（直アップ時のみ有効化） ====
if USE_BODY_UPLOAD:
    app.config["MAX_CONTENT_LENGTH"] = BODY_UPLOAD_MAX_BYTES
else:
    app.config.pop("MAX_CONTENT_LENGTH", None)

# ==== 構造化ログの初期化（ログのみ・処理には影響なし） ====
# ※ utils/logging_setup.py が無い場合はこの import を外してください
try:
    from utils.logging_setup import init_logging
    init_logging(app)
except Exception:
    # ログ初期化は任意。失敗しても続行する。
    logging.basicConfig(level=logging.INFO)
print("=== 環境変数チェック ===")
print(f"AZURE_STORAGE_ACCOUNT_NAME: {os.getenv('AZURE_STORAGE_ACCOUNT_NAME')}")
print(f"BACKEND_BASE: {os.getenv('BACKEND_BASE')}")
print(f"AZURE_STORAGE_CONNECTION_STRING: {os.getenv('AZURE_STORAGE_CONNECTION_STRING', '')[:50]}...")
# ---- 起動時に URL マップをログへ出力（エンドポイント名ズレ検知用） ----
def _log_url_map(flask_app: Flask) -> None:
    try:
        lines = [
            f"{r.endpoint:28s} {','.join(sorted(r.methods)):<18s} {r.rule}"
            for r in flask_app.url_map.iter_rules()
        ]
        flask_app.logger.info("=== URL MAP ===\n" + "\n".join(sorted(lines)))
    except Exception:
        pass

# ==== ルーティング登録 ====
try:
    from routes import setup_routes
    app.logger.info("✔ routes.py を読み込みます")
    setup_routes(app)  # 既存のルーティング初期化（/healthz も含む想定）
    app.logger.info("✔ setup_routes 実行完了")
except Exception as e:
    # ルーティング読み込み失敗もログに残す（起動は継続）
    app.logger.warning(f"[WARNING] routes.py の読み込みに失敗しました: {e}")

# URL マップを最後にログ出力
_log_url_map(app)

# ==== エラーハンドラ（404 は 404 のまま、その他は 500） ====
@app.errorhandler(404)
def handle_404(e):
    try:
        return render_template(
            "error.html",
            title="404 Not Found",
            code=404,
            message=str(e),
        ), 404
    except (JinjaTemplateNotFound, Exception):
        return f"404 Not Found: {request.path}", 404

@app.errorhandler(Exception)
def handle_500(e):
    # NotFound を 500 に昇格させない（保険）
    if isinstance(e, NotFound):
        return handle_404(e)
    logging.exception("Unhandled exception:")
    try:
        return render_template(
            "error.html",
            title="Internal Server Error",
            code=500,
            message=str(e),
        ), 500
    except (JinjaTemplateNotFound, Exception):
        return "500 Internal Server Error", 500

# ==== 起動ログ（情報） ====
app.logger.info(f"[INFO] FFmpeg:  {FFMPEG_BIN}")
app.logger.info(f"[INFO] FFprobe: {FFPROBE_BIN}")
app.logger.info(f"[INFO] USE_BODY_UPLOAD={USE_BODY_UPLOAD} BODY_UPLOAD_MAX_BYTES={BODY_UPLOAD_MAX_BYTES}")

# ==== Flask サーバー起動 ====
if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", 8000))
    app.logger.info(f"[INFO] Starting server on http://0.0.0.0:{port}")
    serve(app, host="0.0.0.0", port=port)
