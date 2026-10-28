# Blender ⇄ Houdini マテリアルブリッジ

## 何をするツールか

Blenderで作ったモデル・マテリアルを、Houdiniに**マテリアル込みで一発インポート**するためのツール。

Blender側でモデル(USD)とマテリアル情報(json)を書き出し → Houdini側でそれを読み込んで、
マテリアルネットワーク込みのノード一式を自動生成する。手作業でのマテリアル貼り直しが不要になる。

---

## 使い方

### Blender側（書き出す方）

- **どこに入れる**: `Edit > Preferences > Add-ons > Install...` からアドオンファイル
  （`blender_houdini_bridge_exporter.py`）を選んでインストール・有効化する
- **何が必要**: Blender 4.x以降
- **使い方**:
  1. アセット用のフォルダ（フォルダ名がそのままアセット名として使われる）を作り、
     その直下に `.blend` を保存する
  2. サイドバーの「Houdini Bridge」パネルを開く
  3. 「Export Houdini Bridge」を押す
  4. 出力先は自動で決まる（`<アセットフォルダ>/publish/<バージョン>/`。
     アセット名・バージョンは自動判定される。手入力での上書きも可能）

### Houdini側（読み込む方）

- **どこに入れる**: ツール本体のフォルダをどこかに置き、対応する package json を
  Houdiniの packages フォルダ（例: `Documents/houdini22.0/packages/`）にコピーする
- **何が必要**: Houdini 22.0
- **使い方**:
  1. シェルフの「Import Asset」ツールを押す
  2. マニフェスト（Blender側で書き出した jsonファイル）を選ぶ
  3. 「Import」を押す → マテリアル込みのノードが `/obj` に生成される
