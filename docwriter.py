import os
import sys
from kowake import load_keywords_from_file, _apply_keyword_replacements
from pathlib import Path
from table_writer import table_writer
from minutes_writer import write_minutes_section

def process_document(word_file_path: str, output_file_path: str, extracted_info: dict, email: str = None):
    """
    統合関数: テーブルの更新と議事録の書き込みを処理する

    :param word_file_path: 読み込む Word テンプレートのファイルパス
    :param output_file_path: 出力先（更新後の Word ファイルのパス）
    :param extracted_info: 生成AIが抽出した辞書データ {label: value, replaced_transcription: "..."}
    :param email: ユーザーメール（個人キーワード辞書用）
    """
    print(f"[INFO] Wordテンプレート: {word_file_path}")
    print(f"[INFO] 出力ファイル: {output_file_path}")
    print(f"[INFO] 抽出情報: {len(extracted_info)} 個")
    print(f"[INFO] email: {email}")

    # --- 追加デバッグ: extracted_info のキーと値のサイズ/タイプを可視化
    debug_map = {}
    for k, v in extracted_info.items():
        if isinstance(v, str):
            debug_map[k] = len(v)
        else:
            debug_map[k] = type(v).__name__
    print(f"[DEBUG] extracted_info keys & sizes: {debug_map}")

    Path(output_file_path).parent.mkdir(parents=True, exist_ok=True)

    if not extracted_info:
        raise ValueError("extracted_info が空です")

    try:
        # ✅ テーブル更新
        table_writer(word_file_path, output_file_path, extracted_info)
    except Exception as e:
        print(f"[ERROR] テーブル更新中にエラー: {e}")
        raise

    # --- 議事録本文の取得 (複数キーに対応)
    replaced_transcription = (
        extracted_info.get("replaced_transcription")
        or extracted_info.get("transcription")
        or extracted_info.get("full_transcript", "")
    )
    print(f"[DEBUG] replaced_transcription length: {len(replaced_transcription)}")
    if not replaced_transcription:
        print("[WARNING] 本文文字列が空です。抽出キー名や前段の結合処理を確認してください。")

    # ── キーワード置換の追加 ────────────────────────────────────
    load_keywords_from_file(email)  # 最新定義をロード（個人辞書対応）
    replaced_transcription, hit = _apply_keyword_replacements(replaced_transcription, email=email)
    # 標準エラーに出力してログストリームに残す
    print(f"[DEBUG] keyword replace hit = {hit}, email={email}", file=sys.stderr, flush=True)
    # ─────────────────────────────────────────────────────────

    try:
        # ✅ 議事録本文の挿入
        write_minutes_section(output_file_path, output_file_path, replaced_transcription)
    except Exception as e:
        print(f"[ERROR] 議事録本文挿入中にエラー: {e}")
        raise

    # ✅ ファイル生成後の検証
    if not os.path.exists(output_file_path):
        raise RuntimeError(f"[ERROR] Wordファイルが生成されていません: {output_file_path}")

    file_size = os.path.getsize(output_file_path)
    if file_size < 1000:
        raise RuntimeError(f"[ERROR] Wordファイルのサイズが異常です（{file_size}バイト）: {output_file_path}")

    print(f"[INFO] Word 議事録生成完了: {output_file_path}")