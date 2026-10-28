"""USD 上の Material prim 名と、マニフェストのマテリアルを突き合わせる。

エクスポータの v0.3 は ``usd_name`` を書かないので、Blender が USD 用にどう
サニタイズしたかをこちら側で推測して照合する必要がある。照合キーは 3 つ:

1. ``source_name``（Blender の原文）
2. ``safe_name``（エクスポータがサニタイズしたもの）
3. ``sanitize(source_name)``（こちらでサニタイズしたもの）

これらの完全一致を先に試し、それでも決まらない場合にだけ大文字小文字を無視する。

大文字小文字を無視して複数ヒットした場合は **曖昧として扱い、採用しない**。
黙って片方を選ぶと、片方のマテリアルが静かに間違ったシェーダを指すことになる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .naming import sanitize

# 照合の結果ラベル。レポートの Match 列にそのまま出る。
EXACT = "exact"
CASE_INSENSITIVE = "case-insensitive"
AMBIGUOUS = "ambiguous"
UNMATCHED = "unmatched"


@dataclass
class Index:
    exact: Dict[str, object] = field(default_factory=dict)
    folded: Dict[str, List[object]] = field(default_factory=dict)
    case_insensitive: bool = True

    def lookup(self, name: str) -> Tuple[Optional[object], str]:
        """(material, 照合方法) を返す。見つからなければ (None, 理由)。"""
        if not name:
            return None, UNMATCHED

        hit = self.exact.get(name)
        if hit is not None:
            return hit, EXACT

        if not self.case_insensitive:
            return None, UNMATCHED

        candidates = self.folded.get(name.casefold()) or []
        unique = {id(m): m for m in candidates}
        if len(unique) == 1:
            return next(iter(unique.values())), CASE_INSENSITIVE
        if len(unique) > 1:
            return None, AMBIGUOUS
        return None, UNMATCHED

    def lookup_usd_path(self, usd_path: str) -> Tuple[Optional[object], str]:
        """``/root/_materials/M_Wall`` のような USD パスから引く。

        照合するのは **末尾トークンだけ**。Blender の USD エクスポータが
        マテリアルをどのスコープに置くかはバージョンで変わるため、
        パス全体で照合すると壊れやすい。
        """
        if not usd_path:
            return None, UNMATCHED
        token = usd_path.rstrip("/").rsplit("/", 1)[-1]
        return self.lookup(token)


def build_index(materials, case_insensitive: bool = True) -> Index:
    index = Index(case_insensitive=case_insensitive)
    for material in materials:
        keys = {material.source_name, material.safe_name, sanitize(material.source_name)}
        for key in keys:
            if not key:
                continue
            index.exact.setdefault(key, material)
            index.folded.setdefault(key.casefold(), []).append(material)
    return index


def ambiguous_names(materials) -> List[str]:
    """大文字小文字だけが違うマテリアル名の組を列挙する（事前警告用）。"""
    buckets: Dict[str, List[str]] = {}
    for material in materials:
        buckets.setdefault(material.source_name.casefold(), []).append(material.source_name)
    return sorted(
        ", ".join(sorted(set(names)))
        for names in buckets.values()
        if len(set(names)) > 1
    )
