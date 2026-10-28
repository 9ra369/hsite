"""File Open / File New 後に、HIP と同じ場所の申し送りメモを表示する。

読み込むファイル名は ``<hipファイル名>.memo.json``。
例: ``shot_010.hip`` を開いた場合は ``shot_010.hip.memo.json`` を読む。

対応する JSON 形式:

    {"text": "前回: ...\\n次回: ...", "saved_at": "2026-08-28T10:00:00"}

または、作業内容を分けたい場合:

    {
      "previous_work": ["煙の見た目を調整した", "キャッシュを書き出した"],
      "next_tasks": ["ライティングを確認する", "flipbook を作成する"]
    }

456.py は Houdini の File > Open と File > New の完了後に Houdini が実行する。
"""
import os
import sys
import traceback

import hou


def _find_scripts_dir():
    """HOUDINI_PATH から、このツールの scripts ディレクトリを見つける。"""
    for entry in hou.houdiniPath():
        candidate = os.path.join(entry, "scripts")
        if os.path.isfile(os.path.join(candidate, "memo_store.py")):
            return candidate
    return None


def _as_lines(value):
    """JSON の文字列または配列を、ダイアログ表示用の文字列にする。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join("- {}".format(item) for item in value if str(item).strip())
    return ""


def _memo_text(memo):
    """旧来の text 形式と、previous_work / next_tasks 形式の両方を表示する。"""
    text = _as_lines(memo.get("text", ""))
    if text:
        return text

    sections = []
    previous = _as_lines(memo.get("previous_work", ""))
    next_tasks = _as_lines(memo.get("next_tasks", ""))
    if previous:
        sections.append("前回までの作業\n{}".format(previous))
    if next_tasks:
        sections.append("これからすべき作業\n{}".format(next_tasks))
    return "\n\n".join(sections)


def _show_memo_after_file_change():
    try:
        scripts_dir = _find_scripts_dir()
        if scripts_dir is None:
            return
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)

        from memo_store import load_memo
        from memo_ui import MemoShowDialog

        hip_path = hou.hipFile.path()
        # 未保存ファイルでは $HIP が実用的なプロジェクトフォルダを指さないため無視する。
        if not hip_path or os.path.basename(hip_path).lower() == "untitled.hip":
            return

        memo = load_memo(hip_path)
        if not memo:
            return

        text = _memo_text(memo)
        if not text:
            return

        try:
            parent = hou.qt.mainWindow()
        except Exception:
            parent = None

        dialog = MemoShowDialog(
            hip_name=os.path.basename(hip_path),
            text=text,
            saved_at=str(memo.get("saved_at", "")),
            parent=parent,
        )
        dialog.exec_()
    except Exception:
        # メモ機能の問題で File Open / New 自体を失敗させない。
        traceback.print_exc()


_show_memo_after_file_change()
