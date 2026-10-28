# Houdini Blender Bridge

Blender 側が書き出した `<asset>/publish/<version>/` 配下の
`<asset>.json`（マニフェスト）+ `geo/<asset>.usdc` + `tex/...` を
Houdini に取り込み、`/obj` にマテリアル込みのネットワークを生成する
（blender_export_revision.md のフラット構成に対応。旧 `<asset>.bridge.json`
＋同階層 `.usdc` の構成も後方互換で読める）。

## インストール

1. このフォルダを `$HSITE/dev/houdini_blender_bridge` に置く
2. `houdini_blender_bridge.json` を `$HSITE/houdini22.0/packages/` にコピー
3. Houdini を再起動

パスを変える場合は package の `BLENDER_BRIDGE` を書き換える。

確認:

```python
import blender_bridge
print(blender_bridge.__version__)
```

## 使い方

シェルフ `Arcel`（`houdini22.0/toolbar/arcel.shelf` が一元管理するタブ。
`toolbar/blender_bridge.shelf` はツール本体の定義のみを持ち、タブへの登録は
持たない。[_knowledge/houdini_menu_and_shelf_python_execution.md](../../_knowledge/houdini_menu_and_shelf_python_execution.md)参照）に 2 つ入る。

- **Import Asset** — ダイアログを開く
  - **Library** 欄にライブラリのルート（例: `01_assets`）を指定すると、
    直下のアセットフォルダ・`publish/vNNN` バージョンがコンボボックスに
    並び、選ぶだけでマニフェストパスが自動で埋まる
  - コンボボックスに出てこない場合（新規アセット、決め打ちの階層に
    沿わない置き方など）は、下の **Manifest** 欄でこれまで通りパスを
    直接指定・Browse できる
- **Inspect USD** — USD を読んで、Unpack 後の属性一覧を出す（診断用）

Python から直接:

```python
from blender_bridge import api

# ライブラリ構成（<root>/<asset>/publish/<version>/）から探す場合
assets = api.list_assets("D:/Projects/Work/26_Arcel/01_assets")
versions = api.list_versions("D:/Projects/Work/26_Arcel/01_assets", "building")
manifest_path = api.resolve_manifest(
    "D:/Projects/Work/26_Arcel/01_assets", "building", versions[0]
)

result = api.import_asset(manifest_path, shader_type="materialx")  # or "principled"
print(result.object_path)
for line in result.warnings:
    print(line)
```

## 生成されるもの

```
/obj/bldg_test
 ├── materials                ← matnet（固定名）
 │    ├── M_Wall
 │    └── M_Metal
 ├── usd_import               ← USD Import SOP（geo/ 配下 or 同階層の .usdc を自動解決）
 ├── zup_to_yup               ← Transform rx -90（Blender は Z-up）
 ├── unpack_usd               ← Unpack USD SOP（Output = Polygons）
 ├── assign_materials         ← Python SOP（shop_materialpath / texture_path を書く）
 ├── cleanup_attributes       ← Attribute Delete（usdmaterialpath は残す）
 ├── delete_downfacing        ← Delete（Normal タブで下向きの面を削除）
 ├── clean_geo                ← Clean
 └── OUT                      ← Null (Display / Render)
```

ノードの並びと Unpack の Output 設定は Houdini 22.0 で手動確認したもの。
Transform は Unpack の**前**（パックされた USD prim の段階で回す）。

`assign_materials` は `shop_materialpath` に加えて、basecolor テクスチャの
絶対パスを Primitive string 属性 `texture_path` としても書く（無ければ空文字）。
マテリアルネットワークを辿らずに、ジオメトリ側からテクスチャの所在が分かる。

`cleanup_attributes` は `usdmaterialpath` を追跡用にわざと残す
（Primitive Attributes: `usd* path ^usdmaterialpath`）。その後
`delete_downfacing` で下向きの面（床の裏など）を削除する。Entity=Primitives、
Normal タブ（Direction (0,-1,1) / Spread Angle 5°）で選択、Group は空のまま
（実機の delete_downfacing.cpio から採取した設定）。その後
`clean_geo`（Clean SOP、既定パラメータ）で後片付けする。

## うまくいかないときは Inspect USD

`assign_materials` は `usdmaterialpath` プリミティブ属性を主経路にしている。
実機で別の名前だった場合、このノードが警告を出す。

そのときは Inspect USD の出力を見て、面ごとにマテリアルを識別できる属性を探し、
`python/blender_bridge/network/assign_runtime.py` の
`PATH_ATTRIB_CANDIDATES` に追加する。hip は作り直さなくてよい。

## テスト

`core/` は `hou` に依存しないので Houdini 無しで走る。

```
PYTHONPATH=python pytest tests -q
```

## メインメニューに入れる場合

`$BLENDER_BRIDGE/MainMenuCommon.xml`:

```xml
<subMenu id="krm_blender_bridge">
  <label>Blender Bridge</label>
  <scriptItem id="krm_bridge_import">
    <label>Import Asset...</label>
    <scriptCode><![CDATA[
from blender_bridge.ui import dialog
dialog.show()
]]></scriptCode>
  </scriptItem>
</subMenu>
```
