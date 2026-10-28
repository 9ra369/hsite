"""アセットライブラリ（``<root>/<asset>/publish/<version>/``）の走査。

blender_export_revision.md の構成に合わせたもの。以前のように
マニフェストのフルパスを毎回手で入力・Browse するのではなく、

    <root>/                      例: 01_assets/
      <asset>/                   例: building/
        publish/
          v001/
            <asset>.json         ← マニフェスト（旧: <asset>.bridge.json）
            geo/<asset>.usdc
            tex/...

という決め打ちの階層を辿って、フォルダ名からアセット／バージョンを
選べるようにするための土台。UI（``ui/dialog.py``）はこれを使って
ルート → アセット → バージョンの3段階コンボボックスを作る。

``hou`` に依存しないので Houdini 無しでテストできる。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

_VERSION_RE = re.compile(r"^v\d{3,}$")

# マニフェストのファイル名候補（優先順）。
# 新: "<asset>.json" / 旧: "<asset>.bridge.json" ・ 案として出ていた
# "<asset>.manifest.json" もまだ確定でないため候補に残しておく。
MANIFEST_NAME_TEMPLATES = (
    "{asset}.json",
    "{asset}.bridge.json",
    "{asset}.manifest.json",
)


def list_assets(root) -> List[str]:
    """``root`` 直下のアセットフォルダ名を一覧する。

    ``publish`` サブフォルダを持つディレクトリだけをアセットとみなす
    （参照用フォルダなど、無関係なものを拾わないため）。
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    names = [
        child.name for child in root.iterdir()
        if child.is_dir() and (child / "publish").is_dir()
    ]
    return sorted(names, key=str.casefold)


def list_versions(root, asset: str) -> List[str]:
    """``<root>/<asset>/publish`` 配下の ``vNNN`` フォルダを新しい順に返す。"""
    publish_dir = Path(root).expanduser() / asset / "publish"
    if not publish_dir.is_dir():
        return []
    versions = [
        child.name for child in publish_dir.iterdir()
        if child.is_dir() and _VERSION_RE.match(child.name)
    ]
    # "v001" < "v002" < ... 桁数が揃っている前提だが、念のため数値でソートする。
    return sorted(versions, key=lambda v: int(v[1:]), reverse=True)


def find_manifest(root, asset: str, version: str) -> Optional[Path]:
    """``<root>/<asset>/publish/<version>/`` からマニフェストを探す。

    まず ``MANIFEST_NAME_TEMPLATES`` の候補名を順に試し、どれも無ければ
    そのフォルダ直下（``geo``/``tex`` の中は見ない）にある ``*.json`` を
    1つだけ拾う。複数あって決められない場合は None（呼び出し側で
    パスを手入力してもらう）。
    """
    version_dir = Path(root).expanduser() / asset / "publish" / version
    if not version_dir.is_dir():
        return None

    for template in MANIFEST_NAME_TEMPLATES:
        candidate = version_dir / template.format(asset=asset)
        if candidate.is_file():
            return candidate

    loose = [p for p in version_dir.glob("*.json") if p.is_file()]
    if len(loose) == 1:
        return loose[0]
    return None
