"""生成された Python SOP から毎 cook 呼ばれる、マテリアル割り当ての本体。

ノードにコードを直接埋め込まず、ここを呼ぶだけにしてある。
仕様を詰めている最中は、hip を作り直さずにこのファイルを直せる方が速い。

**まだ検証中の前提**: USD Import SOP → Unpack USD SOP のあとに
``usdmaterialpath`` プリミティブ文字列属性が付く、というのが主経路。
実機で違う名前だった場合に備えて候補を順に探し、
どれを使ったかを detail 属性に残す。

``shop_materialpath`` を書くのと同じループで ``texture_path``（Primitive,
string）も書く。basecolor テクスチャの絶対パスを持たせておくことで、
マテリアルネットワークを辿らなくてもジオメトリ側からテクスチャの所在が
分かるようにするため（blender_houdini_bridge_spec_v0_4.md 追補 v0.4a）。
"""

from __future__ import annotations

import hou

from ..builders.base import resolve_texture_path

# 割り当てキーに使えそうなプリミティブ文字列属性の候補（優先順）。
# 実機で確認できたら先頭を確定させ、残りは消してよい。
PATH_ATTRIB_CANDIDATES = (
    "usdmaterialpath",
    "usd_materialpath",
    "materialpath",
    "usdmaterial",
)

# 割り当て結果を残す detail 属性
STAT_SOURCE = "bridge_assign_source"
STAT_ASSIGNED = "bridge_assigned"
STAT_UNASSIGNED = "bridge_unassigned"
STAT_MESSAGE = "bridge_assign_message"

# basecolor テクスチャの絶対パスを持たせる Primitive string 属性。
# テクスチャが無いマテリアル／未割り当てのプリミティブは空文字のまま。
TEXTURE_PATH_ATTRIB = "texture_path"


def _detail_str(geo, name, value):
    if geo.findGlobalAttrib(name) is None:
        geo.addAttrib(hou.attribType.Global, name, "")
    geo.setGlobalAttribValue(name, str(value))


def _detail_int(geo, name, value):
    if geo.findGlobalAttrib(name) is None:
        geo.addAttrib(hou.attribType.Global, name, 0)
    geo.setGlobalAttribValue(name, int(value))


def _find_path_attrib(geo):
    for name in PATH_ATTRIB_CANDIDATES:
        attrib = geo.findPrimAttrib(name)
        if attrib is not None and attrib.dataType() == hou.attribData.String:
            return attrib
    return None


def _build_texture_paths(data, texture_path_mode: str):
    """マテリアル ident -> basecolor テクスチャの絶対パス。

    マテリアルノードの ``file`` パラメータと同じ解決結果になるよう、
    ビルド側と同じ ``resolve_texture_path`` を使う。テクスチャの無い
    マテリアルはここに現れず、参照側で空文字にフォールバックする。
    """
    paths = {}
    for material in data.materials:
        channel = material.channel("basecolor")
        if channel is not None and channel.is_texture:
            paths[material.ident] = resolve_texture_path(
                data.root, channel.path, texture_path_mode)
    return paths


def run(node=None):
    """Python SOP の本体。``node`` 省略時は ``hou.pwd()``。"""
    node = node or hou.pwd()
    geo = node.geometry()
    obj = node.parent()

    manifest_path = obj.userData("bridge_manifest")
    if not manifest_path:
        _detail_str(geo, STAT_MESSAGE,
                    "bridge_manifest が親オブジェクトに設定されていません")
        return

    from ..core import manifest as mf
    from ..core import matching

    try:
        data = mf.load(manifest_path)
    except mf.ManifestError as exc:
        _detail_str(geo, STAT_MESSAGE, f"マニフェストを読めません: {exc}")
        raise hou.NodeError(str(exc))

    case_insensitive = obj.userData("bridge_case_insensitive") != "0"
    index = matching.build_index(data.materials, case_insensitive)

    matnet_name = obj.userData("bridge_matnet") or "materials"
    # matnet のパスを親から毎回導出する。
    # オブジェクトノードをリネーム・移動しても割り当てが壊れない。
    matnet_path = f"{obj.path()}/{matnet_name}"

    if geo.findPrimAttrib("shop_materialpath") is None:
        geo.addAttrib(hou.attribType.Prim, "shop_materialpath", "")
    if geo.findPrimAttrib(TEXTURE_PATH_ATTRIB) is None:
        geo.addAttrib(hou.attribType.Prim, TEXTURE_PATH_ATTRIB, "")

    # マテリアルノードの file パラメータと揃える。ビルド時に選んだモードを
    # user data から読む（無ければ既定の absolute）。
    texture_path_mode = obj.userData("bridge_texture_path_mode") or "absolute"
    texture_paths = _build_texture_paths(data, texture_path_mode)

    assigned = 0
    unassigned = 0
    unmatched_tokens = set()

    path_attrib = _find_path_attrib(geo)

    if path_attrib is not None:
        source = path_attrib.name()
        for prim in geo.prims():
            usd_path = prim.attribValue(path_attrib)
            material, how = index.lookup_usd_path(usd_path)
            if material is None:
                unassigned += 1
                if usd_path:
                    unmatched_tokens.add(f"{usd_path} ({how})")
                continue
            prim.setAttribValue("shop_materialpath",
                                f"{matnet_path}/{material.safe_name}")
            prim.setAttribValue(TEXTURE_PATH_ATTRIB,
                                texture_paths.get(material.ident, ""))
            assigned += 1
    else:
        # フォールバック: material_id_attribute があれば使う。
        # v0.3 のエクスポータはこの属性を宣言するだけで書き出していないので、
        # 実際にはここへ来た時点で「割り当て情報が無い」ことが多い。
        source = data.material_id_attribute or "(none)"
        id_attrib = (geo.findPrimAttrib(data.material_id_attribute)
                     if data.material_id_attribute else None)
        if id_attrib is None:
            unassigned = len(geo.prims())
            _detail_str(geo, STAT_SOURCE, "none")
            _detail_int(geo, STAT_ASSIGNED, 0)
            _detail_int(geo, STAT_UNASSIGNED, unassigned)
            _detail_str(
                geo, STAT_MESSAGE,
                "マテリアル割り当てのキーになる属性が見つかりません。"
                f"探した属性: {', '.join(PATH_ATTRIB_CANDIDATES)}。"
                "Inspect USD で実際の属性名を確認してください",
            )
            raise hou.NodeWarning(
                "マテリアル割り当てのキーが見つかりません（Inspect USD で確認）"
            )

        by_ident = {m.ident: m for m in data.materials}
        by_order = list(data.materials)
        for prim in geo.prims():
            raw = prim.attribValue(id_attrib)
            material = None
            if isinstance(raw, str):
                material, _ = index.lookup(raw)
            elif isinstance(raw, int) and 0 <= raw < len(by_order):
                material = by_order[raw]
            if material is None:
                material = by_ident.get(str(raw))
            if material is None:
                unassigned += 1
                continue
            prim.setAttribValue("shop_materialpath",
                                f"{matnet_path}/{material.safe_name}")
            prim.setAttribValue(TEXTURE_PATH_ATTRIB,
                                texture_paths.get(material.ident, ""))
            assigned += 1

    _detail_str(geo, STAT_SOURCE, source)
    _detail_int(geo, STAT_ASSIGNED, assigned)
    _detail_int(geo, STAT_UNASSIGNED, unassigned)

    if unassigned:
        detail = "; ".join(sorted(unmatched_tokens)[:5]) or "-"
        message = (f"{unassigned} プリミティブが未割り当てです "
                   f"(キー: {source}) 例: {detail}")
        _detail_str(geo, STAT_MESSAGE, message)
        raise hou.NodeWarning(message)

    _detail_str(geo, STAT_MESSAGE, f"{assigned} プリミティブを割り当てました")
