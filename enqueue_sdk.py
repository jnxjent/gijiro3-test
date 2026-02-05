import os, json
from azure.storage.queue import QueueClient
cs = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
qname = "audio-processing"
payload = {
  "job_id": "pingSDK001",
  "blob_url": "https://midacteststorage.blob.core.windows.net/mom/audio/sampleSuperSUPER.mp3",
  "template_blob_url": "https://midacteststorage.blob.core.windows.net/mom/audio/word/%E8%AD%B0%E4%BA%8B%E9%8C%B2%E3%83%86%E3%83%B3%E3%83%97%E3%83%AC%E3%83%BC%E3%83%88_%E6%A8%99%E6%BA%96.docx"
}
qc = QueueClient.from_connection_string(cs, qname)
qc.send_message(json.dumps(payload, ensure_ascii=False))
print("enqueued:", payload["job_id"])
