"""外から呼ぶ入口。

シェルフツール、メインメニュー、hython からのバッチ実行はすべてここを通す。
UI はこの上に乗っているだけなので、UI 無しでも同じことができる。

    from blender_bridge import api
    result = api.import_asset("D:/bridge_assets/building_a/building_a.bridge.json",
                              shader_type="materialx")
    print(result.object_path)
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .core import library
from .core import manifest as mf
from .network import builder


def load_manifest(manifest_path):
    """マニフェストを読む。UI がプレビュー表示に使う。"""
    return mf.load(manifest_path)


def list_assets(root) -> List[str]:
    """ライブラリルート直下のアセットフォルダ名を一覧する（UI のアセット欄用）。"""
    return library.list_assets(root)


def list_versions(root, asset: str) -> List[str]:
    """``<root>/<asset>/publish`` の ``vNNN`` を新しい順に返す（UI のバージョン欄用）。"""
    return library.list_versions(root, asset)


def resolve_manifest(root, asset: str, version: str) -> Optional[Path]:
    """ルート・アセット・バージョンからマニフェストのパスを決める。

    見つからなければ None（UI 側は手入力の path_field にフォールバックする）。
    """
    return library.find_manifest(root, asset, version)


def import_asset(manifest_path,
                 shader_type: str = "materialx",
                 case_insensitive: bool = True,
                 texture_path_mode: str = "absolute",
                 on_existing: str = "new",
                 object_name: str = "",
                 apply_zup_fix: bool = True):
    """マニフェスト 1 つ分のネットワークを ``/obj`` に生成する。"""
    data = mf.load(manifest_path)
    return builder.build(
        data,
        shader_type=shader_type,
        case_insensitive=case_insensitive,
        texture_path_mode=texture_path_mode,
        on_existing=on_existing,
        object_name=object_name,
        apply_zup_fix=apply_zup_fix,
    )


def find_manifests(root, max_depth: int = 5) -> List[Path]:
    """``*.bridge.json``（旧命名）を再帰的に探す。

    ``<root>/<asset>/publish/<version>/`` という決め打ちの階層に対しては
    ``list_assets`` / ``list_versions`` / ``resolve_manifest`` の方を使う。
    こちらはそれ以外の自由な置き方をしているライブラリ向けの汎用フォールバック
    として残す。
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return []

    found: List[Path] = []
    base_depth = len(root.parts)
    for path in root.rglob("*.bridge.json"):
        if len(path.parent.parts) - base_depth > max_depth:
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        found.append(path)
    return sorted(found)


def repair(node=None) -> list:
    """既に hip にあるブリッジのオブジェクトを、現在のコードに合わせて直す。

    Python SOP のスニペットとパッケージの置き場所を書き直す。
    ``ModuleNotFoundError: No module named 'blender_bridge'`` が出る
    古い hip を、作り直さずに復帰させるためのもの。

        from blender_bridge import api
        print(api.repair())
    """
    import hou

    from .network.builder import ASSIGN_SNIPPET, _package_root

    targets = [node] if node is not None else [
        child for child in hou.node("/obj").children()
        if child.userData("bridge_asset_id")
    ]

    fixed = []
    root = _package_root()
    for obj in targets:
        obj.setUserData("bridge_pythonpath", root)
        assign = obj.node("assign_materials")
        if assign is not None and assign.parm("python") is not None:
            assign.parm("python").set(ASSIGN_SNIPPET)
            fixed.append(obj.path())
    return fixed


def inspect_usd(usd_path) -> str:
    """USD を読んで実際の属性一覧を返す（診断用）。"""
    from .network import inspect
    return inspect.report(str(usd_path))
