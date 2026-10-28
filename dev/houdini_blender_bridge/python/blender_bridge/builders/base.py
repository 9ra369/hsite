"""シェーダビルダーの共通部分。

Houdini のパラメータ名は未検証のものが多いので、``set_parm`` は
**存在しないパラメータで例外を投げず、記録して続行する**。
検証フェーズでは「1 つ名前が違うだけで全部落ちる」のが一番困る。
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Callable, Dict, List, Sequence

import hou


def resolve_texture_path(root: Path, relative: str, mode: str) -> str:
    """マニフェスト相対パス -> Houdini のパラメータに入れる文字列に解決する。

    マテリアルノードの ``file`` パラメータと、``assign_materials`` が書く
    ``texture_path`` 属性が同じ文字列になるよう、解決ロジックをここに集約する
    （blender_houdini_bridge_spec_v0_4.md 追補 v0.4a）。片方だけ絶対パス・
    もう片方が collapse 済み、という食い違いを作らないため。
    """
    absolute = (Path(root) / relative).resolve().as_posix()
    if mode == "collapse":
        try:
            return hou.text.collapseCommonVars(absolute, vars=["$HIP", "$JOB"])
        except AttributeError:
            return absolute
    return absolute


class BuildLog:
    """ビルド中の警告を貯める。UI のレポートにそのまま出す。"""

    def __init__(self) -> None:
        self.warnings: List[str] = []
        self.notes: List[str] = []
        self.missing_parms: List[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        """「実際にはこの名前で設定した」を残す。

        パラメータ名がまだ確定していない箇所があるので、
        何が採用されたかを見えるようにしておく。
        """
        self.notes.append(message)

    def missing(self, node: "hou.Node", parm_name: str) -> None:
        entry = f"{node.type().name()}.{parm_name}"
        if entry not in self.missing_parms:
            self.missing_parms.append(entry)
            self.warnings.append(
                f"パラメータが見つかりません: {entry} "
                f"(Houdini のバージョン差の可能性。ノードで parms() を確認してください)"
            )


def set_parm(node: "hou.Node", name: str, value, log: BuildLog) -> bool:
    """スカラー / タプルのどちらでも受け付けて設定する。"""
    tuple_parm = node.parmTuple(name)
    if tuple_parm is not None and len(tuple_parm) > 1:
        if isinstance(value, (list, tuple)):
            padded = list(value)[:len(tuple_parm)]
            padded += [padded[-1] if padded else 0.0] * (len(tuple_parm) - len(padded))
            tuple_parm.set(tuple(padded))
        else:
            tuple_parm.set(tuple([value] * len(tuple_parm)))
        return True

    parm = node.parm(name)
    if parm is None:
        log.missing(node, name)
        return False

    if isinstance(value, (list, tuple)):
        value = value[0] if value else 0.0
    try:
        parm.set(value)
    except (hou.OperationFailed, TypeError) as exc:
        log.warn(f"{node.path()}.{name} に {value!r} を設定できません: {exc}")
        return False
    return True


def set_first_parm(node: "hou.Node", names: Sequence[str], value, log: BuildLog) -> bool:
    """候補名を順に試す。ノードタイプごとのパラメータ名の揺れを吸収する。"""
    for name in names:
        if node.parm(name) is not None or node.parmTuple(name) is not None:
            return set_parm(node, name, value, log)
    log.warn(
        f"{node.path()}: {list(names)} のいずれのパラメータも見つかりません"
    )
    return False


def set_menu_containing(node: "hou.Node", parm_names: Sequence[str],
                        keyword: str, log: BuildLog) -> bool:
    """メニューパラメータから ``keyword`` に当たる項目を選ぶ。

    Unpack USD SOP の Output のように、パラメータ名も選択肢のトークンも
    バージョンで変わりうるものを、ラベル／トークンの内容から拾うための逃げ道。
    候補名で見つからなければ、そのノードの全メニューパラメータを走査する。
    """
    candidates = [node.parm(name) for name in parm_names]
    candidates = [p for p in candidates if p is not None]
    if not candidates:
        candidates = [
            p for p in node.parms()
            if isinstance(p.parmTemplate(), hou.MenuParmTemplate)
        ]

    key = keyword.strip().lower()

    # 1 周目は完全一致、2 周目で部分一致。
    # 先に部分一致を許すと "Polygons" ではなく "Polygon Soup" を掴みうる。
    for exact in (True, False):
        for parm in candidates:
            template = parm.parmTemplate()
            if not isinstance(template, hou.MenuParmTemplate):
                continue
            tokens = list(template.menuItems())
            labels = list(template.menuLabels())
            for index, token in enumerate(tokens):
                label = labels[index] if index < len(labels) else token
                fields = (token.strip().lower(), label.strip().lower())
                hit = (key in fields) if exact else any(key in f for f in fields)
                if not hit:
                    continue
                try:
                    parm.set(token)
                except (hou.OperationFailed, TypeError):
                    parm.set(index)
                log.note(f"{node.name()}.{parm.name()} = {token} ({label})")
                return True

    log.warn(
        f"{node.path()}: '{keyword}' に当たるメニュー項目が見つかりません。"
        "手で設定してください"
    )
    return False


def create_node(parent: "hou.Node", type_names: Sequence[str], node_name: str,
                log: BuildLog):
    """候補のノードタイプを順に試して作る。作れなければ None。"""
    for type_name in type_names:
        try:
            return parent.createNode(type_name, node_name)
        except hou.OperationFailed:
            continue
    log.warn(f"{parent.path()}: ノードタイプ {list(type_names)} を作成できません")
    return None


class MaterialBuilder(abc.ABC):
    """1 マテリアル分のネットワークを作る。"""

    #: UI のメニューに出る表示名
    label = ""

    def __init__(self, matnet: "hou.Node", resolve: Callable[[str], str], log: BuildLog):
        self.matnet = matnet
        self.resolve = resolve      # マニフェスト相対パス -> Houdini 用パス文字列
        self.log = log

    @abc.abstractmethod
    def build(self, material) -> "hou.Node":
        """新規にネットワークを作り、ルートノードを返す。"""

    def update(self, node: "hou.Node", material) -> "hou.Node":
        """既存ノードを更新する。既定は作り直し。"""
        node.destroy()
        return self.build(material)

    def build_all(self, materials, on_existing: str = "update") -> Dict[str, "hou.Node"]:
        built: Dict[str, "hou.Node"] = {}
        for material in materials:
            existing = self.matnet.node(material.safe_name)
            if existing is not None and on_existing == "skip":
                built[material.safe_name] = existing
                continue
            if existing is not None and on_existing == "recreate":
                existing.destroy()
                existing = None

            node = (self.update(existing, material) if existing is not None
                    else self.build(material))
            if node is None:
                continue
            # 原文の名前を残す。レポートと再照合で使う。
            node.setUserData("blender_material_name", material.source_name)
            node.setUserData("bridge_material_id", material.ident)
            built[material.safe_name] = node

        try:
            self.matnet.layoutChildren()
        except hou.OperationFailed:
            pass
        return built
