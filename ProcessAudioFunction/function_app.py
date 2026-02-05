# function_app.py
import logging
import azure.functions as func

# -------------------------------------------------
# アプリ初期化
# -------------------------------------------------
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


# -------------------------------------------------
# HTTPトリガー: 手動テストやAPI呼び出し用
# -------------------------------------------------
@app.function_name(name="ProcessAudio")
@app.route(route="ProcessAudio", methods=["POST"])
def process_audio(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP POST /api/ProcessAudio
    テスト用のエンドポイント。OK応答のみ。
    """
    logging.info("[ProcessAudio] HTTP trigger invoked.")
    return func.HttpResponse("ok", status_code=200)


# -------------------------------------------------
# Queueトリガー: 本番用のバックグラウンド処理
# -------------------------------------------------
@app.function_name(name="ProcessAudioQueue")
@app.queue_trigger(
    arg_name="msg",
    queue_name="%QUEUE_NAME%",                # ←App Settings の QUEUE_NAME を参照
    connection="AzureWebJobsStorage"          # ←既定のストレージを利用
)
def process_audio_queue(msg: func.QueueMessage) -> None:
    """
    Azure Storage Queue からのトリガー。
    議事郎がキューに入れたメッセージを非同期処理。
    """
    try:
        body = msg.get_body().decode("utf-8")
        logging.info(f"[ProcessAudioQueue] Received: {body}")

        # TODO: 実際の音声処理や議事録生成の処理をここに書く
        # 例：
        # process_message(json.loads(body))

    except Exception as e:
        logging.exception("[ProcessAudioQueue] Exception occurred")
        raise e   # 例外を再送出 → poison キューに入る
