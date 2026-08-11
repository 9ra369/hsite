"""現在のhipを次のバージョンで保存する。

    26_AAA_v002.hiplc -> 26_AAA_v003.hiplc
    26_AAA_v009.hiplc -> 26_AAA_v010.hiplc
    26_AAA_v099.hiplc -> 26_AAA_v100.hiplc
"""

import os
import re

import hou

VERSION_PATTERN = re.compile(r"[vV](\d+)")


def bump_version(stem):
    """ファイル名（拡張子なし）の中で最後に現れる v### を +1 する。"""
    matches = list(VERSION_PATTERN.finditer(stem))
    if not matches:
        raise ValueError("バージョン表記 v### が見つかりません: {}".format(stem))

    m = matches[-1]
    digits = m.group(1)
    # zfill で桁数を維持。009 -> 010、099 -> 100 と自然に繰り上がる
    bumped = str(int(digits) + 1).zfill(len(digits))
    return "{}{}{}{}".format(stem[:m.start()], m.group(0)[0], bumped, stem[m.end():])


def next_version_path(path):
    """フルパスを受け取り、バージョンを1つ上げたフルパスを返す。"""
    directory, filename = os.path.split(path.replace("\\", "/"))
    stem, ext = os.path.splitext(filename)
    return "{}/{}{}".format(directory, bump_version(stem), ext)


def _save_next_version(skip_existing=True, verbose=True):
    """現在開いているhipを次バージョンとして保存し、そのパスを返す。"""
    current = hou.hipFile.path()

    if os.path.basename(current).startswith("untitled."):
        raise hou.OperationFailed("未保存のシーンです。先に一度名前を付けて保存してください。")

    new_path = next_version_path(current)

    # 既に存在するバージョンは飛ばして、空いている番号まで進める
    while skip_existing and os.path.exists(new_path):
        new_path = next_version_path(new_path)

    hou.hipFile.save(new_path)

    if verbose:
        filename = os.path.basename(new_path)
        message = "Saved as: {}".format(filename)
        if hou.isUIAvailable():
            hou.ui.setStatusMessage(message, hou.severityType.ImportantMessage)
            hou.ui.displayMessage(message, title="Save Next Version")
        print(message)

    return new_path


def save_next_version(skip_existing=True, verbose=True):
    """_save_next_version のエラーを必ずダイアログ表示してから呼び出し元に再送出する。"""
    try:
        return _save_next_version(skip_existing=skip_existing, verbose=verbose)
    except Exception as e:
        error_message = "Save Next Version failed: {}".format(e)
        print(error_message)
        if hou.isUIAvailable():
            hou.ui.displayMessage(
                error_message,
                title="Save Next Version - Error",
                severity=hou.severityType.Error,
            )
        raise


# show_env（env_viewer.py）と同じく、Houdiniのメニュー scriptPath 実行では
# __name__ が "__main__" にならないため、ガードを付けずトップレベルで直接実行する。
save_next_version()
