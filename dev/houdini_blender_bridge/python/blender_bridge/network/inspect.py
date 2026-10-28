"""USD を読み込んで、Unpack 後に実際どんな属性が付いてくるかを調べる。

いまは仕様書の「実機で確認すべき項目」を潰している段階なので、
何かを作る前にこれを走らせて、``usdmaterialpath`` が本当に出るのか、
名前は何なのか、値はどういう形なのかを確定させるためのもの。

使い方（Python シェル）::

    from blender_bridge.network import inspect
    print(inspect.report("D:/bridge_assets/building_a/building_a.usdc"))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import hou

from ..builders.base import (BuildLog, create_node, set_first_parm,
                             set_menu_containing)
from .builder import (UNPACK_OUTPUT_KEYWORD, UNPACK_OUTPUT_PARMS,
                      UNPACK_USD_TYPES, USD_FILE_PARMS, USD_IMPORT_TYPES)

# 割り当てキーとして使えそうな属性名（見つかったら強調して出す）
INTERESTING = ("usdmaterialpath", "usd_materialpath", "materialpath",
               "usdmaterial", "shop_materialpath", "path", "name")


@dataclass
class AttribInfo:
    owner: str
    name: str
    data_type: str
    size: int
    samples: List[str] = field(default_factory=list)

    @property
    def interesting(self) -> bool:
        return self.name in INTERESTING


@dataclass
class InspectResult:
    usd_path: str = ""
    prim_count: int = 0
    point_count: int = 0
    attribs: List[AttribInfo] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def candidates(self) -> List[AttribInfo]:
        return [a for a in self.attribs if a.interesting and a.owner == "prim"]


def _samples(geo, attrib, owner: str, limit: int = 4) -> List[str]:
    values = []
    elements = geo.prims() if owner == "prim" else geo.points()
    for element in elements:
        try:
            value = element.attribValue(attrib)
        except hou.OperationFailed:
            break
        text = str(value)
        if text not in values:
            values.append(text)
        if len(values) >= limit:
            break
    return values


def inspect(usd_path: str) -> InspectResult:
    """一時的なオブジェクトを作って USD を読み、属性一覧を返して片付ける。"""
    result = InspectResult(usd_path=str(usd_path))
    log = BuildLog()
    temp = hou.node("/obj").createNode("geo", "bridge_inspect_temp")

    try:
        usd_import = create_node(temp, USD_IMPORT_TYPES, "usd_import", log)
        if usd_import is None:
            result.errors.append("USD Import SOP を作成できませんでした")
            return result
        set_first_parm(usd_import, USD_FILE_PARMS, str(usd_path), log)

        unpack = create_node(temp, UNPACK_USD_TYPES, "unpack_usd", log)
        if unpack is None:
            result.errors.append("Unpack USD SOP を作成できませんでした")
            return result
        unpack.setInput(0, usd_import)
        # 実際に使う設定と同じ状態で調べないと意味がない
        set_menu_containing(unpack, UNPACK_OUTPUT_PARMS, UNPACK_OUTPUT_KEYWORD, log)

        try:
            geo = unpack.geometry()
        except hou.OperationFailed as exc:
            result.errors.append(f"cook に失敗しました: {exc}")
            return result

        result.prim_count = len(geo.prims())
        result.point_count = len(geo.points())
        result.groups = [g.name() for g in geo.primGroups()]

        for owner, attribs in (("detail", geo.globalAttribs()),
                               ("prim", geo.primAttribs()),
                               ("point", geo.pointAttribs()),
                               ("vertex", geo.vertexAttribs())):
            for attrib in attribs:
                info = AttribInfo(
                    owner=owner,
                    name=attrib.name(),
                    data_type=str(attrib.dataType()).rsplit(".", 1)[-1],
                    size=attrib.size(),
                )
                if owner in ("prim", "point"):
                    info.samples = _samples(geo, attrib, owner)
                result.attribs.append(info)

        result.errors.extend(log.warnings)
    finally:
        temp.destroy()

    return result


def report(usd_path: str) -> str:
    """人が読む形に整形する。"""
    data = inspect(usd_path)
    lines = [
        f"USD: {data.usd_path}",
        f"prims: {data.prim_count}   points: {data.point_count}",
        "",
    ]

    if data.errors:
        lines.append("--- errors ---")
        lines.extend(f"  {e}" for e in data.errors)
        lines.append("")

    if data.candidates:
        lines.append("--- 割り当てキーの候補 (prim / string) ---")
        for info in data.candidates:
            lines.append(f"  {info.name}")
            for sample in info.samples:
                lines.append(f"      {sample}")
        lines.append("")
    else:
        lines.append("--- 割り当てキーの候補は見つかりませんでした ---")
        lines.append("  探した名前: " + ", ".join(INTERESTING))
        lines.append("  下の一覧から、面ごとにマテリアルを識別できる属性を探してください")
        lines.append("")

    lines.append("--- attributes ---")
    for owner in ("detail", "prim", "point", "vertex"):
        rows = [a for a in data.attribs if a.owner == owner]
        if not rows:
            continue
        lines.append(f"  [{owner}]")
        for info in rows:
            mark = " *" if info.interesting else "  "
            lines.append(f"   {mark} {info.name:<28} {info.data_type:<8} size={info.size}")
            for sample in info.samples[:2]:
                lines.append(f"        {sample}")

    if data.groups:
        lines.append("")
        lines.append("--- primitive groups ---")
        for name in data.groups:
            lines.append(f"    {name}")

    return "\n".join(lines)
