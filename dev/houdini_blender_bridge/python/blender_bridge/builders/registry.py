"""shader_type の文字列から Builder クラスを引く。

Arnold や Redshift を足す場合もここに登録するだけで済む。
"""

from __future__ import annotations

from .materialx import MaterialXBuilder
from .principled import PrincipledBuilder

_REGISTRY = {
    "materialx": MaterialXBuilder,
    "mtlx": MaterialXBuilder,
    "principled": PrincipledBuilder,
    "principledshader": PrincipledBuilder,
}

#: UI のメニュー用 (value, label)
CHOICES = (
    ("materialx", "MaterialX"),
    ("principled", "Principled Shader"),
)


def get_builder(shader_type: str):
    key = (shader_type or "").strip().lower()
    if key not in _REGISTRY:
        raise ValueError(
            f"未知の shader_type '{shader_type}' "
            f"(使えるのは {', '.join(sorted(set(_REGISTRY)))})"
        )
    return _REGISTRY[key]
