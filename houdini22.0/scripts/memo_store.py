"""houdini_memo: メモの保存・読み込み（hou非依存）

このモジュールは Houdini API (hou) に一切依存しない。
hipファイルパスの文字列だけを受け取り、同じフォルダに
サイドカーファイル "<hipファイル名>.memo.json" として
メモを保存/読み込みする。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional

MEMO_SUFFIX = ".memo.json"


def memo_path_for(hip_path: str) -> Path:
    """hipファイルパスに対応するメモファイルのパスを返す"""
    p = Path(hip_path)
    return p.with_name(p.name + MEMO_SUFFIX)


def load_memo(hip_path: str) -> Optional[dict]:
    """保存済みメモを dict で返す。無い/壊れている場合は None。

    形式: {"text": str, "saved_at": "ISO8601", "hip_file": str}
    """
    path = memo_path_for(hip_path)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_memo(hip_path: str, text: str) -> None:
    """メモを保存する。空文字(空白のみ含む)なら既存メモを削除する"""
    text = text.strip()
    if not text:
        clear_memo(hip_path)
        return

    data = {
        "text": text,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "hip_file": Path(hip_path).name,
    }
    path = memo_path_for(hip_path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clear_memo(hip_path: str) -> None:
    """メモファイルを削除する（存在しなければ何もしない）"""
    path = memo_path_for(hip_path)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
