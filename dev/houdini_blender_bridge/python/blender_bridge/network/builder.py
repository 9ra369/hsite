"""``/obj`` にオブジェクトノードと内部の SOP チェーンを生成する。

生成物::

    /obj/<asset>
     ├── materials                ← matnet（固定名）
     │    └── <material> ...
     ├── usd_import               ← USD Import SOP（geo/ 配下 or 同階層の .usdc を自動解決）
     ├── zup_to_yup               ← Transform rx -90（Blender は Z-up）
     ├── unpack_usd               ← Unpack USD SOP（Output = Polygons）
     ├── assign_materials         ← Python SOP（shop_materialpath / texture_path を書く）
     ├── cleanup_attributes       ← Attribute Delete（usdmaterialpath は残す）
     ├── delete_downfacing        ← Delete（Normal タブで下向きの面を削除）
     ├── clean_geo                ← Clean
     └── OUT                      ← Null (Display / Render)

ノードの並びと Unpack の Output 設定は Houdini 22.0 で実機確認したもの。
Transform は Unpack の**前**（パックされた USD prim の段階で回す）。

``assign_materials`` は ``texture_path``（Primitive, string）も一緒に書く。
basecolor テクスチャの絶対パス（拡張子込み、無ければ空文字）で、
マテリアルネットワークを辿らなくてもジオメトリ側から所在が分かるようにするため
（blender_houdini_bridge_spec_v0_4.md 追補 v0.4a）。本体は
``network/assign_runtime.py`` にある。

``cleanup_attributes`` の後に、``delete_downfacing`` で下向きの面を削除し
（Entity=Primitives、Normal タブ: Direction (0,-1,1) / Spread Angle 5°、
Group は空のまま。実機の delete_downfacing.cpio から採取した設定）、
``clean_geo``（Clean SOP、既定パラメータ）で後片付けする。

matnet をオブジェクトの**中**に置く。アセット 1 つがノード 1 つに完結するので、
削除・複製・別 hip へのコピーがそのまま通る。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import hou

from ..builders.base import (BuildLog, create_node, resolve_texture_path,
                             set_first_parm, set_menu_containing, set_parm)
from ..core import matching
from ..core.naming import sanitize

MATNET_NAME = "materials"

USD_IMPORT_TYPES = ("usdimport",)
UNPACK_USD_TYPES = ("unpackusd",)

# USD Import SOP のファイルパラメータ名（バージョン差の吸収）。
# 実機確認: Houdini 22.0 では "filepath1"（複数レイヤー対応のマルチパラム
# の1番目）。念のため無印の "filepath" 等も候補として残す。
USD_FILE_PARMS = ("filepath1", "filepath", "file", "import_file")

# Unpack USD SOP の Output を Polygons にする。
# パラメータ名も選択肢のトークンも未確定なので、候補名 → 全メニュー走査の順で探す。
UNPACK_OUTPUT_PARMS = ("output", "outputtype", "unpackto", "geotype")
UNPACK_OUTPUT_KEYWORD = "polygons"

# Blender の USD は Z-up。Houdini の Y-up に合わせる回転。
ZUP_TO_YUP_ROTATE_X = -90.0

# Unpack のあとに残っている USD 由来の属性を消す。
# assign_materials の *後* に置くこと。usdmaterialpath は追跡用にわざと残す
# （Primitive Attributes で ``^usdmaterialpath`` として除外する）。
# "usd*" は前方一致、"path" は完全一致なので texture_path はここに巻き込まれず
# 残る（意図的）。パターンをワイルドカードで広げるときは注意すること。
CLEANUP = {
    "primdel": "usd* path ^usdmaterialpath",
    "ptdel": "usd*",
    "vtxdel": "usd*",
    "dtldel": "usd* bridge_assign_source bridge_assign_message",
}

# 下向きの面を削除する Delete SOP。
# パラメータ名・値は実機の delete_downfacing.cpio から採取したもの
# （Group は空のまま。Normal タブの Direction / Spread Angle で選択する）。
DELETE_TYPES = ("delete",)
DELETE_PARMS = {
    "negate": "dele",        # Operation = Delete Selected
    "entity": "primitive",   # Entity = Primitives
    "geotype": "all",        # Geometry Type = All Types
    "affectnormal": "on",    # Normal タブを有効化
    "angle": 5.0,            # Spread Angle
    "removegrp": "on",       # Delete Unused Groups
    "keeppoints": "off",     # Keep Points
}
DELETE_DIRECTION = (0.0, -1.0, 1.0)  # Normal タブの Direction（実機の値そのまま）

# delete_downfacing の後片付け（既定パラメータのまま使う）。
CLEAN_TYPES = ("clean",)


@dataclass
class ImportResult:
    obj: Optional["hou.Node"] = None
    matnet: Optional["hou.Node"] = None
    out: Optional["hou.Node"] = None
    built: Dict[str, "hou.Node"] = field(default_factory=dict)
    rows: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)

    @property
    def object_path(self) -> str:
        return self.obj.path() if self.obj else ""


def _resolve_texture(manifest, mode: str):
    """マニフェスト相対パス -> Houdini のパラメータに入れる文字列。"""

    def resolve(relative: str) -> str:
        return resolve_texture_path(manifest.root, relative, mode)

    return resolve


def _package_root() -> str:
    """``blender_bridge`` パッケージを含むディレクトリの絶対パス。"""
    import blender_bridge
    return Path(blender_bridge.__file__).resolve().parent.parent.as_posix()


def _unique_object_name(base: str) -> str:
    parent = hou.node("/obj")
    name = base
    index = 1
    while parent.node(name) is not None:
        name = f"{base}_{index}"
        index += 1
    return name


def _find_existing(asset_id: str):
    for child in hou.node("/obj").children():
        if child.userData("bridge_asset_id") == asset_id:
            return child
    return None


def build(manifest,
          shader_type: str = "materialx",
          case_insensitive: bool = True,
          texture_path_mode: str = "absolute",
          on_existing: str = "new",
          object_name: str = "",
          apply_zup_fix: bool = True) -> ImportResult:
    """マニフェストからネットワークを 1 式生成する。"""
    from ..builders.registry import get_builder

    result = ImportResult()
    log = BuildLog()

    # --- マニフェスト自体の問題を先に出す -------------------------------
    result.warnings.extend(manifest.issues)
    for text in manifest.exporter_warnings:
        result.warnings.append(f"[Blender] {text}")
    for pair in matching.ambiguous_names(manifest.materials):
        result.warnings.append(
            f"大文字小文字だけが違うマテリアル名があります: {pair}。"
            "USD 側の名前と取り違える可能性があります"
        )

    # --- オブジェクトノード ---------------------------------------------
    base_name = sanitize(object_name or manifest.asset_name, "asset")
    existing = _find_existing(manifest.asset_id)

    if existing is not None and on_existing == "recreate":
        existing.destroy()
        existing = None

    if existing is not None and on_existing in ("update", "skip"):
        obj = existing
        result.info.append(f"既存のオブジェクトを再利用しました: {obj.path()}")
    else:
        obj = hou.node("/obj").createNode("geo", _unique_object_name(base_name))
        if obj.name() != base_name:
            result.warnings.append(
                f"'{base_name}' は既に使われているため '{obj.name()}' で作成しました"
            )
    result.obj = obj

    # 組み立て直さず、読み込んだ実ファイルのパスをそのまま持たせる。
    # アセット名とファイル名が食い違っていても壊れない。
    obj.setUserData("bridge_manifest", manifest.path.as_posix())
    obj.setUserData("bridge_asset_id", manifest.asset_id)
    obj.setUserData("bridge_shader_type", shader_type)
    obj.setUserData("bridge_case_insensitive", "1" if case_insensitive else "0")
    obj.setUserData("bridge_matnet", MATNET_NAME)
    # assign_materials が texture_path 属性を書くときに、マテリアルノードの
    # file パラメータと同じ文字列（absolute / collapse）になるよう揃える。
    obj.setUserData("bridge_texture_path_mode", texture_path_mode)
    # blender_bridge パッケージの置き場所を hip に埋めておく。
    # PYTHONPATH が通っていないセッションで hip を開いても、
    # Python SOP が自力で sys.path に足して復帰できる。
    obj.setUserData("bridge_pythonpath", _package_root())

    # --- matnet とマテリアル ---------------------------------------------
    matnet = obj.node(MATNET_NAME) or obj.createNode("matnet", MATNET_NAME)
    result.matnet = matnet

    builder_cls = get_builder(shader_type)
    builder = builder_cls(matnet, _resolve_texture(manifest, texture_path_mode), log)
    result.built = builder.build_all(
        manifest.materials,
        on_existing="update" if on_existing in ("update", "skip") else "recreate",
    )

    # --- SOP チェーン -----------------------------------------------------
    if obj.node("OUT") is None:
        _build_sop_chain(obj, manifest, log, result, apply_zup_fix)
    else:
        usd_import = obj.node("usd_import")
        if usd_import is not None:
            set_first_parm(usd_import, USD_FILE_PARMS,
                           manifest.geometry_path.as_posix(), log)
        result.info.append("既存の SOP チェーンを再利用しました")

    result.out = obj.node("OUT")
    result.info.extend(log.notes)
    result.warnings.extend(log.warnings)
    result.rows = _report_rows(manifest, result)

    try:
        obj.layoutChildren()
    except hou.OperationFailed:
        pass

    return result


def _build_sop_chain(obj, manifest, log: BuildLog, result: ImportResult,
                     apply_zup_fix: bool = True) -> None:
    # --- USD Import ---------------------------------------------------
    usd_import = create_node(obj, USD_IMPORT_TYPES, "usd_import", log)
    if usd_import is None:
        result.warnings.append(
            "USD Import SOP を作成できませんでした。ノードタイプ名を確認してください"
        )
        return
    set_first_parm(usd_import, USD_FILE_PARMS, manifest.geometry_path.as_posix(), log)
    upstream = usd_import

    # --- Z-up -> Y-up ---------------------------------------------------
    # Blender の USD は Z-up ステージ。Unpack の*前*、パックされた USD prim
    # の段階で回す（実機確認済みの並び）。
    if apply_zup_fix:
        zup = obj.createNode("xform", "zup_to_yup")
        set_parm(zup, "rx", ZUP_TO_YUP_ROTATE_X, log)
        zup.setInput(0, upstream)
        zup.setComment("Blender は Z-up。Houdini の Y-up に合わせる")
        zup.setGenericFlag(hou.nodeFlag.DisplayComment, True)
        upstream = zup
        result.info.append(f"zup_to_yup を追加しました (rx {ZUP_TO_YUP_ROTATE_X})")

    # --- Unpack USD（Output = Polygons）---------------------------------
    unpack = create_node(obj, UNPACK_USD_TYPES, "unpack_usd", log)
    if unpack is None:
        result.warnings.append("Unpack USD SOP を作成できませんでした")
        return
    unpack.setInput(0, upstream)
    set_menu_containing(unpack, UNPACK_OUTPUT_PARMS, UNPACK_OUTPUT_KEYWORD, log)
    upstream = unpack

    # --- マテリアル割り当て ----------------------------------------------
    assign = obj.createNode("python", "assign_materials")
    assign.parm("python").set(ASSIGN_SNIPPET)
    assign.setInput(0, upstream)
    upstream = assign

    # --- 属性の掃除 -------------------------------------------------------
    # assign の *後* に置くこと。usdmaterialpath は追跡用にわざと残す。
    cleanup = obj.createNode("attribdelete", "cleanup_attributes")
    for parm_name, pattern in CLEANUP.items():
        set_parm(cleanup, parm_name, pattern, log)
    cleanup.setInput(0, upstream)
    cleanup.setComment(
        "assign_materials の後に置くこと。usdmaterialpath は追跡用に残す"
    )
    upstream = cleanup

    # --- 下向き面の削除 ---------------------------------------------------
    delete_down = create_node(obj, DELETE_TYPES, "delete_downfacing", log)
    if delete_down is None:
        result.warnings.append("Delete SOP（下向き面の削除）を作成できませんでした")
    else:
        delete_down.setInput(0, upstream)
        for parm_name, value in DELETE_PARMS.items():
            set_parm(delete_down, parm_name, value, log)
        set_parm(delete_down, "dir", DELETE_DIRECTION, log)
        delete_down.setComment("法線が下向きの面を削除")
        delete_down.setGenericFlag(hou.nodeFlag.DisplayComment, True)
        upstream = delete_down

    # --- 後片付け ---------------------------------------------------------
    clean = create_node(obj, CLEAN_TYPES, "clean_geo", log)
    if clean is None:
        result.warnings.append("Clean SOP を作成できませんでした")
    else:
        clean.setInput(0, upstream)
        upstream = clean

    out = obj.createNode("null", "OUT")
    out.setInput(0, upstream)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)


ASSIGN_SNIPPET = """\
# Blender Bridge - マテリアル割り当て
#
# 本体は blender_bridge/network/assign_runtime.py にある。
# 仕様を詰めている間は、hip を触らずにそちらを直せる。
import sys

import hou

_node = hou.pwd()

# PYTHONPATH が通っていないセッションで hip を開いた場合の保険。
# インポート時にパッケージの置き場所を sys.path に足して復帰する。
_root = _node.parent().userData("bridge_pythonpath")
if _root and _root not in sys.path:
    sys.path.append(_root)

try:
    from blender_bridge.network import assign_runtime
except ImportError:
    raise hou.NodeError(
        "blender_bridge をインポートできません。\\n"
        "期待した場所: %s\\n"
        "Houdini パッケージ (houdini_blender_bridge.json) が読み込まれているか、"
        "そのパスにパッケージが存在するかを確認してください。" % (_root or "(未設定)")
    )

assign_runtime.run(_node)
"""


def _prim_counts_by_material(result: ImportResult) -> Optional[Dict[str, int]]:
    """cleanup 後の実ジオメトリを見て、マテリアルごとの割り当て数を数える。

    マニフェストの記述からは「何個のプリムが実際にそのマテリアルになったか」は
    分からない。チャンネル状態（T/x）をファイルの実在で確認するのと同じ方針で、
    ここも ``shop_materialpath`` の実値を集計して出す。cook できない／属性が
    無い場合は None を返し、呼び出し側で "-" として表示する。
    """
    node = result.out
    if node is None:
        return None
    try:
        geo = node.geometry()
    except hou.OperationFailed:
        return None

    attrib = geo.findPrimAttrib("shop_materialpath")
    if attrib is None:
        return None

    counts: Dict[str, int] = {}
    for prim in geo.prims():
        value = prim.attribValue(attrib)
        if not value:
            continue
        name = value.rsplit("/", 1)[-1]
        counts[name] = counts.get(name, 0) + 1
    return counts


def _report_rows(manifest, result: ImportResult) -> List[dict]:
    """レポート表示用の行データ。"""
    used_by = {}
    object_names = {o.ident: o.source_name for o in manifest.objects}
    for entry in manifest.assignments:
        used_by.setdefault(entry.get("material_id"), []).append(
            object_names.get(entry.get("object_id"), entry.get("object_id", "?"))
        )

    prim_counts = _prim_counts_by_material(result)

    rows = []
    for material in manifest.materials:
        node = result.built.get(material.safe_name)
        states = {}
        for name in ("basecolor", "roughness", "metallic", "specular", "normal"):
            channel = material.channel(name)
            if channel is None:
                states[name] = "-"
            elif channel.is_texture:
                path = manifest.texture_path(channel)
                states[name] = "T" if path.is_file() else "x"
            else:
                states[name] = "C"

        prims = "-" if prim_counts is None else prim_counts.get(material.safe_name, 0)

        rows.append({
            "source_name": material.source_name,
            "node": node.path() if node else "(未生成)",
            "used_by": ", ".join(sorted(set(used_by.get(material.ident, [])))) or "-",
            "channels": states,
            "prims": prims,
        })
    return rows
