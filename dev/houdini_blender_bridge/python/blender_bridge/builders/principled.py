"""Principled Shader (``principledshader::2.0``) を組み立てる。

1 マテリアル = 1 ノードで済むので、まず動く状態を確認するにはこちらが早い。

**重要**: Principled Shader は「テクスチャ値 × パラメータ値」で評価される。
テクスチャを有効にしたら、対応する定数側を base color は (1,1,1)、
スカラーは 1.0 にしないと Blender より暗く／弱くなる。
"""

from __future__ import annotations

from .base import MaterialBuilder, set_parm

NODE_TYPES = ("principledshader::2.0", "principledshader")

# 4.4: Blender の Specular IOR Level 0.5 が Houdini の Reflectivity 1.0 に対応する。
SPECULAR_GAIN = 2.0

# (定数パラメータ, テクスチャ有効化, テクスチャパス, テクスチャ無しのときの既定値)
SCALAR_PARMS = {
    "roughness": ("rough", "rough_useTexture", "rough_texture", 0.5),
    "metallic": ("metallic", "metallic_useTexture", "metallic_texture", 0.0),
    "specular": ("reflect", "reflect_useTexture", "reflect_texture", 1.0),
}


class PrincipledBuilder(MaterialBuilder):
    label = "Principled Shader"

    def build(self, material):
        from .base import create_node
        node = create_node(self.matnet, NODE_TYPES, material.safe_name, self.log)
        if node is None:
            return None
        self._apply(node, material)
        return node

    def update(self, node, material):
        self._apply(node, material)
        return node

    # ------------------------------------------------------------------

    def _apply(self, node, material) -> None:
        self._basecolor(node, material.channel("basecolor"))
        for name, (const, use_tex, tex, default) in SCALAR_PARMS.items():
            gain = SPECULAR_GAIN if name == "specular" else 1.0
            self._scalar(node, material.channel(name), const, use_tex, tex, default, gain)
        self._normal(node, material.channel("normal"))

    def _basecolor(self, node, channel) -> None:
        if channel is None:
            return
        if channel.is_texture:
            set_parm(node, "basecolor_useTexture", 1, self.log)
            set_parm(node, "basecolor_texture", self.resolve(channel.path), self.log)
            # テクスチャと乗算されるので白にしておく
            set_parm(node, "basecolor", [1.0, 1.0, 1.0], self.log)
        elif channel.is_constant:
            set_parm(node, "basecolor_useTexture", 0, self.log)
            value = channel.value
            if not isinstance(value, (list, tuple)):
                value = [value, value, value]
            set_parm(node, "basecolor", list(value)[:3], self.log)

    def _scalar(self, node, channel, const, use_tex, tex, default, gain) -> None:
        if channel is None:
            set_parm(node, use_tex, 0, self.log)
            set_parm(node, const, default, self.log)
            return

        if channel.is_texture:
            set_parm(node, use_tex, 1, self.log)
            set_parm(node, tex, self.resolve(channel.path), self.log)
            # 素通しなら 1.0、値域変換が要るチャンネルはここが係数になる
            set_parm(node, const, gain, self.log)
        elif channel.is_constant:
            set_parm(node, use_tex, 0, self.log)
            value = channel.value
            if isinstance(value, (list, tuple)):
                value = value[0] if value else default
            set_parm(node, const, float(value) * gain, self.log)

    def _normal(self, node, channel) -> None:
        if channel is None or not channel.is_texture:
            set_parm(node, "baseBumpAndNormal_enable", 0, self.log)
            return

        set_parm(node, "baseBumpAndNormal_enable", 1, self.log)
        set_parm(node, "baseBumpAndNormal_type", "normal", self.log)
        set_parm(node, "baseNormal_texture", self.resolve(channel.path), self.log)
        set_parm(node, "baseNormal_scale", channel.strength, self.log)
        # Blender の Normal Map ノードは OpenGL 規約 (+Y up) なので既定は反転なし
        set_parm(node, "baseNormal_flipY", 1 if channel.encoding == "directx" else 0,
                 self.log)
        if channel.space == "object":
            self.log.warn(
                f"{material_name(node)}: オブジェクト空間ノーマルは未対応です。"
                "タンジェント空間として接続しました"
            )


def material_name(node) -> str:
    return node.userData("blender_material_name") or node.name()
