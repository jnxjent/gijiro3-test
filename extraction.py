import os
import re
import json
import openai
from dotenv import load_dotenv
from docx import Document

load_dotenv()

TEMPERATURE   = float(os.getenv("TEMPERATURE", 0.7))
DEPLOYMENT_ID = os.getenv("DEPLOYMENT_ID")

async def extract_meeting_info_and_speakers(
    transcribed_text: str,
    word_template_path: str = None
) -> dict:
    """
    1. 生成済み文字起こしを元に話者推定＋全文置換
    2. テンプレートからラベルを取得
    3. 各ラベルに対応する情報を一括で JSON 抽出
    """

    full_transcription = transcribed_text or ""

    # (B) テンプレートからラベル一覧
    table_labels = []
    if word_template_path:
        doc = Document(word_template_path)
        for table in doc.tables:
            for row in table.rows:
                if len(row.cells) >= 1:
                    lbl = row.cells[0].text.strip().rstrip("：:")
                    if lbl:
                        table_labels.append(lbl)

    print(f"[DEBUG] 抽出ラベル一覧: {table_labels}")

    # 初期化
    extracted_info = {lbl: "" for lbl in table_labels}
    extracted_info["推定話者"] = {}
    extracted_info["full_transcription"] = full_transcription

    # (C) 話者推定＋置換
    try:
        sp_prompt = (
            "以下のテキスト中の「[Speaker 0]」「[Speaker 1]」等を実際の話者名または役職に推定変換してください。\n"
            "敬称「さん」は付けず名前のみ、不明な場合は「不明0」等としてください。\n\n"
            f"=== 議事録全文 ===\n{full_transcription}\n\n"
            "【出力形式】純粋に JSON のみ:\n"
            '{ "Speaker 0": "田中", "Speaker 1": "山田", ... }'
        )
        rsp = openai.ChatCompletion.create(
            engine=DEPLOYMENT_ID,
            messages=[
                {"role":"system","content":"あなたは会議の話者名を推定するアシスタントです。"},
                {"role":"user","content":sp_prompt}
            ],
            max_completion_tokens=2000, temperature=0
        )
        sp_raw = rsp.choices[0].message.content.strip()
        # JSON 部分のみ抽出
        jstr = sp_raw[sp_raw.find("{"):sp_raw.rfind("}")+1]
        speaker_map = json.loads(jstr)
        extracted_info["推定話者"] = speaker_map
        print(f"[DEBUG] 話者マップ: {speaker_map}")

        # 置換
        replaced = full_transcription
        for tag, name in speaker_map.items():
            replaced = re.sub(rf"\[?{re.escape(tag)}\]?", f"[{name}]", replaced)
        extracted_info["replaced_transcription"] = replaced

    except Exception as e:
        print(f"[ERROR] 話者推定失敗: {e}")

    # (D) ラベル一括抽出
    if table_labels:
        labels_json = ", ".join(f'"{lbl}"' for lbl in table_labels)
        info_prompt = (
            "以下の会議議事録全文から、次の項目について「可能な限り漏れなく、全ての該当情報を列挙」してください。\n\n"
            f"抽出項目: [{labels_json}]\n\n"
            "=== 議事録全文 ===\n"
            f"{extracted_info.get('replaced_transcription', full_transcription)}\n\n"
            "【出力形式】純粋に JSON オブジェクトのみ:\n"
            "{\n"
            '  "議題": ["～", "～", ...],\n'
            '  "開催日": "～",\n'
            '  "場所": "～",\n'
            '  ...\n'
            "}\n"
            "※「議題」は文字列リスト形式、その他は文字列で回答してください。"
        )
        try:
            rsp = openai.ChatCompletion.create(
                engine=DEPLOYMENT_ID,
                messages=[
                    {"role":"system","content":"あなたは会議議事録から指定情報を網羅的に抽出するアシスタントです。"},
                    {"role":"user","content":info_prompt}
                ],
                max_completion_tokens=8000, temperature=0.3
            )
            raw = rsp.choices[0].message.content.strip()
            print(f"[DEBUG] 生レス:\n{raw}")

            # JSON 部分のみ切り出し＆パース
            body = raw[raw.find("{"):raw.rfind("}")+1]
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                # 不要文字を除去して再試行
                body = re.sub(r"^[^{]*", "", body)
                body = re.sub(r"[^}]*$", "}", body)
                data = json.loads(body)

            # 格納
            for lbl in table_labels:
                v = data.get(lbl, "")
                if isinstance(v, list):
                    extracted_info[lbl] = "\n".join(f"{i+1}. {it}" for i, it in enumerate(v))
                else:
                    extracted_info[lbl] = str(v).strip()

        except Exception as e:
            print(f"[ERROR] 一括抽出失敗: {e}")
            for lbl in table_labels:
                extracted_info[lbl] = "抽出エラー"

    print(f"[DEBUG] 最終 extracted_info: {extracted_info}")
    return extracted_info
