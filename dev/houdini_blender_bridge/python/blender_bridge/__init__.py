"""Blender → Houdini マテリアルブリッジ。

構成:

- ``core``      : hou に依存しない層（マニフェスト、名前、照合）。pytest で直接テストできる
- ``builders``  : シェーダを組み立てる（Principled / MaterialX）
- ``network``   : /obj のネットワーク生成と、Python SOP の実行時ロジック
- ``ui``        : PySide6 のダイアログ
- ``api``       : 外から呼ぶ入口
"""

__version__ = "0.3.0"
