# Window Extraction (Arcel シェルフ)

建物テクスチャから窓の位置を検出し、UV座標を経由して3Dモデルへ配置するツール。
`houdini_blender_bridge` と同じ構成（`dev/` に自己完結パッケージを置き、
`houdini22.0/packages/` にコピーして有効化）で `Arcel` シェルフに追加されている。

## 構成

```
dev/houdini_window_tool/
├── houdini_window_tool.json         ← package定義（houdini22.0/packages/ にコピー済み）
├── python/
│   ├── extract_windows.py           ← 窓マスク/centroid抽出（cv2使用）
│   ├── houdini_window_tool.py       ← Houdini内で動くQt UI本体（Arcelシェルフから起動）
│   └── houdini_import_window_points.py  ← Python SOPに貼る/ツールから自動投入されるコード
└── toolbar/houdini_window_tool.shelf     ← window_extract ツールの定義のみ
```

"Arcel" シェルフタブ自体（どのツールをタブに並べるか）は
`houdini22.0/toolbar/arcel.shelf` 側で一元管理している。同名の
`<toolshelf>` を複数の `.shelf` ファイルで定義してもマージされず
後勝ちで上書きされるため（[_knowledge/houdini_menu_and_shelf_python_execution.md](../../_knowledge/houdini_menu_and_shelf_python_execution.md)参照）、
ここでは `<tool name="window_extract">` の定義だけを持つ。

`houdini_window_tool.json` の `env` で `python/` を `PYTHONPATH` に追加しているので、
シェルフツールからは `import houdini_window_tool` するだけで読み込める。

## 使い方

1. Houdiniのシェルフの **Arcel** タブから **Window Extraction** を実行する。
2. Textures Folder（テクスチャの入ったフォルダ）と Output（書き出し先）を指定する。
3. Detection Method（White Frame / Glass）と Area 範囲を必要に応じて調整する。
4. **Scan** で対象ファイルを確認し、**Extract** で実行する。
   Houdini同梱Pythonに `cv2` があればそのまま実行され（画面上部に表示される）、
   無い場合は Run の Python 欄に `cv2` の入った外部インタプリタ（venvなど）を指定する。
5. **Build Network** で、抽出結果からポイントを生成する Geometry ノードを作る。
   Model SOP を指定すると、Vertex Split → Divide → Attribute Promote → flatten →
   投影用 Attribute Wrangle までの一式を組む。

## 既知の欠落: `project_windows.vex`

`houdini_window_tool.py` の Build Network は、Model SOP 指定時に投影用ワイヤー
(`project_windows` Attribute Wrangle)のスニペットとして
`dev/houdini_window_tool/python/project_windows.vex` を読み込む
(`VEX_FILE = HERE / "project_windows.vex"`)。

**このファイルは元の配布物 4点（`extract_windows.py` /
`houdini_import_window_points.py` / `houdini_window_tool.py` / `README.md`）に
含まれていなかったため、まだ用意できていない。** Model SOP を指定して
Build Network を押すと `FileNotFoundError` になる。Model SOP を空にすれば
（ポイント生成のみのネットワークになるので）今のところ問題なく動く。

`houdini_import_window_points.py` の元READMEには手動セットアップ用に

```c
vector uvw = set(v@uv.x, v@uv.y, 0.0);
int prim = -1;
vector primuvw;
xyzdist(2, uvw, prim, primuvw);
v@P = primuv(3, "P", prim, primuvw);
```

という例が載っているが、`_build_one()` が実際に組む配線は
`project.setInput(0, points)` / `setInput(1, flatten)` / `setInput(2, promote)`
であり、Houdini VEXの `xyzdist()`/`primuv()` の整数引数は0始まり（1番目の入力=0）
なので、この配線に対する正しい呼び出しは `xyzdist(1, ...)` / `primuv(2, "P", ...)`
になるはず（＝上記サンプルはズレている可能性がある）。実機の挙動が未確認のため、
このリポジトリでは推測でVEXコードを書き起こしていない。`project_windows.vex`
の実体、または挙動が確認済みの正しいインデックスを教えてもらえれば追記する。

## 出力フォーマット・CLIオプション

`extract_windows.py` は元の仕様のまま。詳細（`--detection` / `--glass-colour` /
`--seed` / `--min-area` / `--events` などのフラグ、`_centroids.json` の座標系
定義）は各ファイルのdocstring・コード内コメントを参照。

`houdini_import_window_points.py` はJSON1個、または `extract_windows.py` の
出力フォルダ（`*_centroids.json` をまとめて読む）のどちらも受け付ける。
ツール経由で作られた Python SOP には `jsonpath` / `texturefilter` パラメータが
追加され、そちらの値が優先される。
