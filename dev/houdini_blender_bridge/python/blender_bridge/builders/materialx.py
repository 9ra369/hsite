"""MaterialX (``karmamaterialbuilder`` + ``mtlxstandard_surface``) を組み立てる。

色空間についての注意:

MaterialX の Color Space パラメータは USD 上ではメタデータ扱いで、Hydra が
render delegate に渡さない。そのため Karma は ``filecolorspace`` の値ではなく
``signature`` を見て解釈する（float / vector はデータ、color3 / color4 は
拡張子から自動判定）。

つまり **実質的な色空間指定は signature の方**。``filecolorspace`` は
ビューポート表示と、他ツールへ持ち出すときの意図の記録として設定する。
"""

from __future__ import annotations

from .base import MaterialBuilder, create_node, set_parm

BUILDER_TYPES = ("karmamaterialbuilder", "materialxbuilder", "subnet")
SURFACE_TYPES = ("mtlxstandard_surface",)
IMAGE_TYPES = ("mtlximage",)
NORMALMAP_TYPES = ("mtlxnormalmap",)
MULTIPLY_TYPES = ("mtlxmultiply",)

SPECULAR_GAIN = 2.0

# チャンネル -> (signature, filecolorspace)
SIGNATURE = {
    "basecolor": ("color3", "srgb_texture"),
    "roughness": ("float", "Raw"),
    "metallic": ("float", "Raw"),
    "specular": ("float", "Raw"),
    "normal": ("vector3", "Raw"),
}

# チャンネル -> mtlxstandard_surface の入力名
SURFACE_INPUT = {
    "basecolor": "base_color",
    "roughness": "specular_roughness",
    "metallic": "metalness",
    "specular": "specular",
}

# MaterialX は入力を繋ぐとパラメータ値が無視されるので、
# 係数が要るチャンネルには mtlxmultiply を挟む必要がある。
GAIN = {"specular": SPECULAR_GAIN}


class MaterialXBuilder(MaterialBuilder):
    label = "MaterialX"

    def build(self, material):
        subnet = create_node(self.matnet, BUILDER_TYPES, material.safe_name, self.log)
        if subnet is None:
            return None

        surface = self._find_or_create_surface(subnet)
        if surface is None:
            self.log.warn(
                f"{material.source_name}: mtlxstandard_surface を用意できませんでした"
            )
            return subnet

        for name in ("basecolor", "roughness", "metallic", "specular"):
            self._connect_scalar(subnet, surface, name, material.channel(name))

        self._connect_normal(subnet, surface, material.channel("normal"))

        try:
            subnet.layoutChildren()
        except Exception:  # noqa: BLE001 - レイアウトは失敗しても本質ではない
            pass
        return subnet

    # ------------------------------------------------------------------

    def _find_or_create_surface(self, subnet):
        """karmamaterialbuilder は標準で surface を含むので、まず探す。"""
        for child in subnet.children():
            if child.type().name() in SURFACE_TYPES:
                return child
        return create_node(subnet, SURFACE_TYPES, "mtlxstandard_surface", self.log)

    def _make_image(self, subnet, channel_name, channel):
        node = create_node(subnet, IMAGE_TYPES, f"{channel_name}_tex", self.log)
        if node is None:
            return None
        signature, colorspace = SIGNATURE[channel_name]
        # signature はパラメータ構成を変えるので必ず先に設定する
        set_parm(node, "signature", signature, self.log)
        set_parm(node, "file", self.resolve(channel.path), self.log)
        set_parm(node, "filecolorspace", colorspace, self.log)
        return node

    def _connect_scalar(self, subnet, surface, channel_name, channel) -> None:
        if channel is None:
            return

        target = SURFACE_INPUT[channel_name]
        gain = GAIN.get(channel_name, 1.0)

        if channel.is_texture:
            source = self._make_image(subnet, channel_name, channel)
            if source is None:
                return
            if gain != 1.0:
                source = self._make_gain(subnet, channel_name, source, gain)
                if source is None:
                    return
            surface.setNamedInput(target, source, 0)

        elif channel.is_constant:
            value = channel.value
            if isinstance(value, (list, tuple)):
                set_parm(surface, target, [float(v) for v in list(value)[:3]], self.log)
            else:
                set_parm(surface, target, float(value) * gain, self.log)

    def _make_gain(self, subnet, channel_name, source, gain):
        node = create_node(subnet, MULTIPLY_TYPES, f"{channel_name}_gain", self.log)
        if node is None:
            return source
        node.setNamedInput("in1", source, 0)
        set_parm(node, "in2", gain, self.log)
        return node

    def _connect_normal(self, subnet, surface, channel) -> None:
        if channel is None or not channel.is_texture:
            return

        image = self._make_image(subnet, "normal", channel)
        if image is None:
            return

        normalmap = create_node(subnet, NORMALMAP_TYPES, "normalmap", self.log)
        if normalmap is None:
            surface.setNamedInput("normal", image, 0)
            return

        normalmap.setNamedInput("in", image, 0)
        set_parm(normalmap, "scale", channel.strength, self.log)
        set_parm(normalmap, "space", channel.space, self.log)
        surface.setNamedInput("normal", normalmap, 0)

        if channel.encoding == "directx":
            self.log.warn(
                "DirectX 形式のノーマルマップは MaterialX 経路では自動反転しません。"
                "Green チャンネルを反転したテクスチャを用意してください"
            )
