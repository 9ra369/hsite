# hsite

個人用 Houdini 環境（`$HSITE`）の設定・HDA・スクリプトを管理するリポジトリ。

会社のパイプラインは既に完成された状態で使っており、内部の作りを理解しようとしても実態がつかみにくかった。そこで、個人の規模で環境構築を一から組み立ててみることにした。その過程の記録も兼ねている。

## 構成

```
hsite/
└── houdini22.0/
    ├── otls/            # HDA
    ├── python3.11libs/  # Python モジュール（起動時に自動で sys.path に入る）
    ├── scripts/         # 起動スクリプト等
    ├── toolbar/         # シェルフ
    ├── desktop/         # デスクトップレイアウト
    ├── vex/             # VEX include
    └── packages/        # package JSON
```

Houdini は `$HSITE` 直下ではなく `$HSITE/houdiniX.Y` を見にいくため、バージョン番号付きのフォルダが必要になる。

## 設計方針

### ユーザー設定ディレクトリには何も置かない

Houdini の検索パスは優先度の高い順に以下のようになっている。

```
packages で追加されたパス
$HIP
$HOUDINI_USER_PREF_DIR   # Documents/houdiniX.Y
$HSITE/houdiniX.Y
$HFS
```

ユーザー設定ディレクトリが `$HSITE` より優先されるため、両方に同名の HDA があると前者が警告なしに勝つ。スタジオ環境では「個人が site 設定を上書きできる」という機能だが、個人環境では上書きする相手が自分しかいないので、二重管理と原因不明の不整合を生むだけになる。

そのため、ユーザー設定ディレクトリには Houdini が自動生成するもの（`houdini.pref`、アイコンキャッシュ、crash ログ等）だけを置き、自作の資産は一切置かない方針にしている。「自分で作った覚えのあるファイルがそこにあったら異常」という状態を保つと、トラブル時に疑う場所が一つ減る。

なお UI 設定（`houdini.pref`、`hotkeys.pref`）は Houdini が保存時にユーザー設定ディレクトリへ直接書き込むため、`$HSITE` 側に置くことはできない。ここは方針の例外になる。

### 環境変数のレイヤーを3つに絞る

| 変数 | 内容 | 更新頻度 |
|---|---|---|
| `$HSITE` | 完成して安定したツール類 | 低 |
| `$ASSETLIB` | テクスチャ・HDRI・参照データ | 低 |
| `$JOB` | プロジェクト固有のパス | プロジェクトごと |

`$ASSETLIB` を `HOUDINI_PATH` に含めていないのは、これが「Houdini が設定ファイルを探しに行く場所」ではなく単なるアセット置き場だから。検索パスを膨らませると起動時のスキャン対象が増えるだけになる。パラメータ側で `$ASSETLIB/hdri/xxx.exr` のように明示的に参照している。

### バージョンをまたいで共有しないもの

- **HDA** — 前方互換のみ。新しいバージョンで保存すると古いバージョンで開けなくなる
- **`desktop/`、`toolbar/`** — フォーマットがバージョン間で変わる
- **`python3.11libs/`** — フォルダ名自体が Python バージョンに束縛される

バージョンを上げる際は `houdiniX.Y/` を丸ごとコピーし、HDA を開いて保存し直す。移行作業の手間より、共有して壊れたときの原因究明の方がコストが高いという判断。

## セットアップ

```bash
git clone https://github.com/9ra369/hsite.git
```

`Documents/hsite` に配置し、起動バッチで環境変数を設定する。

```bat
set "HSITE=C:/Users/<user>/Documents/hsite"
```

`HOUDINI_PATH` を明示的に設定する場合は、末尾の `&`（標準の検索パス全体に展開される）を必ず残すこと。これを落とすと `$HSITE` も `$HFS` も検索対象から外れる。

## 確認

実際に読み込まれている検索パスとHDAは Python Shell から確認できる。

```python
for p in hou.houdiniPath():
    print(p)

for f in hou.hda.loadedFiles():
    print(f)
```

## メモ

- 空フォルダは Git の追跡対象外のため、構成を保持したい箇所には `.gitkeep` を置いている
- HDA はバイナリなので diff が読めない。リポジトリのサイズが問題になった時点で Git LFS の導入を検討する
