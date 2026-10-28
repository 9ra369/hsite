"""インポート用のダイアログ。

blender_export_revision.md の構成（``<root>/<asset>/publish/<version>/``）
に合わせて、上に「Library」欄を追加した。ルートフォルダを指定すると
アセット／バージョンをコンボボックスから選べ、マニフェストのパスは
自動で組み立てる。ライブラリの構造に沿わない置き方をしている場合や、
コンボボックスに欲しいものがまだ無い場合（Blender側で新規に作った
直後など）は、下の「Manifest」欄でこれまで通りパスを直接指定・Browse
できる。
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:  # Houdini 19.5 以前
    from PySide2 import QtCore, QtWidgets  # type: ignore

import hou

from .. import api
from ..builders.registry import CHOICES

CHANNEL_ORDER = ("basecolor", "roughness", "metallic", "specular", "normal")
CHANNEL_HEADS = ("D", "R", "M", "S", "N")

_dialog = None


class ImportDialog(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import Blender Asset")
        self.setMinimumSize(760, 620)
        self._manifest = None
        self._build_ui()

    # ------------------------------------------------------------------ ui

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)

        # --- library ------------------------------------------------------
        # <root>/<asset>/publish/<version>/ をルートフォルダ指定だけで辿れる
        # ようにする。見つからない・当てはまらない場合は下の Manifest 欄で
        # 手入力すればよい（新規アセット、ライブラリ構成に沿わない置き方など）。
        box = QtWidgets.QGroupBox("Library")
        form = QtWidgets.QVBoxLayout(box)

        row = QtWidgets.QHBoxLayout()
        self.lib_root = QtWidgets.QLineEdit()
        self.lib_root.setPlaceholderText(".../01_assets")
        self.lib_root.editingFinished.connect(self._reload_assets)
        root_browse = QtWidgets.QPushButton("Browse…")
        root_browse.setFixedWidth(90)
        root_browse.clicked.connect(self._browse_root)
        row.addWidget(self.lib_root)
        row.addWidget(root_browse)
        form.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Asset"))
        self.lib_asset = QtWidgets.QComboBox()
        self.lib_asset.currentIndexChanged.connect(self._reload_versions)
        row.addWidget(self.lib_asset, 1)
        row.addWidget(QtWidgets.QLabel("Version"))
        self.lib_version = QtWidgets.QComboBox()
        self.lib_version.currentIndexChanged.connect(self._use_library_selection)
        row.addWidget(self.lib_version, 1)
        form.addLayout(row)

        self.lib_status = QtWidgets.QLabel(
            "ルートフォルダを指定すると、直下のアセットフォルダ一覧から選べます"
        )
        self.lib_status.setWordWrap(True)
        self.lib_status.setStyleSheet("color:#9a9a9a;")
        form.addWidget(self.lib_status)
        layout.addWidget(box)

        # --- manifest ---------------------------------------------------
        box = QtWidgets.QGroupBox("Manifest")
        form = QtWidgets.QVBoxLayout(box)

        row = QtWidgets.QHBoxLayout()
        self.path_field = QtWidgets.QLineEdit()
        self.path_field.setPlaceholderText(".../<asset>.json")
        self.path_field.editingFinished.connect(self._reload)
        browse = QtWidgets.QPushButton("Browse…")
        browse.setFixedWidth(90)
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_field)
        row.addWidget(browse)
        form.addLayout(row)

        self.summary = QtWidgets.QLabel("マニフェストを指定してください")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:#9a9a9a;")
        form.addWidget(self.summary)
        layout.addWidget(box)

        # --- options ----------------------------------------------------
        box = QtWidgets.QGroupBox("Options")
        grid = QtWidgets.QGridLayout(box)

        grid.addWidget(QtWidgets.QLabel("Shader Type"), 0, 0)
        self.shader = QtWidgets.QComboBox()
        for value, label in CHOICES:
            self.shader.addItem(label, value)
        grid.addWidget(self.shader, 0, 1)

        grid.addWidget(QtWidgets.QLabel("Name Matching"), 1, 0)
        self.matching = QtWidgets.QComboBox()
        self.matching.addItem("Case-Insensitive", True)
        self.matching.addItem("Exact", False)
        grid.addWidget(self.matching, 1, 1)

        grid.addWidget(QtWidgets.QLabel("Texture Paths"), 2, 0)
        self.tex_mode = QtWidgets.QComboBox()
        self.tex_mode.addItem("Absolute", "absolute")
        self.tex_mode.addItem("Collapse $HIP / $JOB", "collapse")
        grid.addWidget(self.tex_mode, 2, 1)

        grid.addWidget(QtWidgets.QLabel("On Existing"), 3, 0)
        self.on_existing = QtWidgets.QComboBox()
        self.on_existing.addItem("Create New", "new")
        self.on_existing.addItem("Update Existing", "update")
        self.on_existing.addItem("Recreate", "recreate")
        grid.addWidget(self.on_existing, 3, 1)

        grid.addWidget(QtWidgets.QLabel("Object Name"), 4, 0)
        self.object_name = QtWidgets.QLineEdit()
        self.object_name.setPlaceholderText("（空ならアセット名から自動生成）")
        grid.addWidget(self.object_name, 4, 1)

        self.zup_fix = QtWidgets.QCheckBox("Z-up → Y-up (Transform rx -90)")
        self.zup_fix.setChecked(True)
        self.zup_fix.setToolTip(
            "Blender の USD は Z-up ステージ。Unpack の後に回転を挟みます"
        )
        grid.addWidget(self.zup_fix, 5, 1)
        grid.setColumnStretch(1, 1)
        layout.addWidget(box)

        # --- report -----------------------------------------------------
        self.table = QtWidgets.QTableWidget(0, 3 + len(CHANNEL_HEADS) + 1)
        self.table.setHorizontalHeaderLabels(
            ["Blender Name", "Houdini Node", "Used By", *CHANNEL_HEADS, "Prims"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        self.log.setPlaceholderText(
            "T = テクスチャ / C = 定数 / x = ファイル欠損 / - = チャンネル無し"
        )
        layout.addWidget(self.log)

        # --- actions ----------------------------------------------------
        row = QtWidgets.QHBoxLayout()
        self.inspect_button = QtWidgets.QPushButton("Inspect USD")
        self.inspect_button.setToolTip(
            "USD を読み込んで、Unpack 後に実際どんな属性が付くかを調べます。\n"
            "割り当てがうまくいかないときに、まずこれを見てください"
        )
        self.inspect_button.clicked.connect(self._inspect)
        self.import_button = QtWidgets.QPushButton("Import")
        self.import_button.setDefault(True)
        self.import_button.clicked.connect(self._import)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)

        row.addWidget(self.inspect_button)
        row.addStretch(1)
        row.addWidget(close)
        row.addWidget(self.import_button)
        layout.addLayout(row)

        self._set_enabled(False)

    def _set_enabled(self, state: bool) -> None:
        self.import_button.setEnabled(state)
        self.inspect_button.setEnabled(state)

    # ------------------------------------------------------------- library

    def _browse_root(self) -> None:
        start = self.lib_root.text() or hou.expandString("$HIP")
        picked = hou.ui.selectFile(
            start_directory=start or None,
            title="Select asset library root (例: 01_assets)",
            file_type=hou.fileType.Directory,
            chooser_mode=hou.fileChooserMode.Read,
        )
        if picked:
            self.lib_root.setText(hou.expandString(picked).rstrip("/"))
            self._reload_assets()

    def _reload_assets(self) -> None:
        root = self.lib_root.text().strip()
        self.lib_asset.blockSignals(True)
        self.lib_asset.clear()
        if root:
            assets = api.list_assets(root)
            self.lib_asset.addItems(assets)
            if assets:
                self.lib_status.setText(f"{len(assets)} 件のアセットが見つかりました")
            else:
                self.lib_status.setText(
                    f"{root} の直下に publish フォルダを持つアセットが見つかりません"
                )
        else:
            self.lib_status.setText(
                "ルートフォルダを指定すると、直下のアセットフォルダ一覧から選べます"
            )
        self.lib_asset.blockSignals(False)
        self._reload_versions()

    def _reload_versions(self) -> None:
        root = self.lib_root.text().strip()
        asset = self.lib_asset.currentText()
        self.lib_version.blockSignals(True)
        self.lib_version.clear()
        if root and asset:
            versions = api.list_versions(root, asset)
            self.lib_version.addItems(versions)
            if not versions:
                self.lib_status.setText(
                    f"{asset} に publish/vNNN フォルダが見つかりません"
                )
        self.lib_version.blockSignals(False)
        self._use_library_selection()

    def _use_library_selection(self) -> None:
        root = self.lib_root.text().strip()
        asset = self.lib_asset.currentText()
        version = self.lib_version.currentText()
        if not (root and asset and version):
            return

        manifest_path = api.resolve_manifest(root, asset, version)
        if manifest_path is None:
            self.lib_status.setText(
                f"{asset}/publish/{version} 内にマニフェスト(.json)が"
                "見つかりません。下の Manifest 欄で手入力してください"
            )
            return

        self.lib_status.setText(f"見つかりました: {manifest_path}")
        self.path_field.setText(str(manifest_path))
        self._reload()

    # ------------------------------------------------------------- events

    def _browse(self) -> None:
        start = self.path_field.text() or hou.expandString("$HIP")
        picked = hou.ui.selectFile(
            start_directory=os.path.dirname(start) or None,
            title="Select bridge manifest",
            file_type=hou.fileType.Any,
            pattern="*.json",
            chooser_mode=hou.fileChooserMode.Read,
        )
        if picked:
            self.path_field.setText(hou.expandString(picked).rstrip("/"))
            self._reload()

    def _reload(self) -> None:
        path = self.path_field.text().strip()
        if not path:
            self._set_enabled(False)
            return

        try:
            self._manifest = api.load_manifest(path)
        except Exception as exc:  # noqa: BLE001 - UI に出す
            self._manifest = None
            self._set_enabled(False)
            self.summary.setText(f"<span style='color:#c96a5c;'>{exc}</span>")
            self.table.setRowCount(0)
            return

        data = self._manifest
        self.summary.setText(
            f"<b>{data.asset_name}</b> &nbsp; schema {data.schema_version} &nbsp; "
            f"objects {len(data.objects)} &nbsp; materials {len(data.materials)} &nbsp; "
            f"geometry {data.geometry_path.name}"
        )
        if not self.object_name.text():
            self.object_name.setPlaceholderText(f"（空なら {data.asset_name}）")

        lines = [f"[issue] {t}" for t in data.issues]
        lines += [f"[Blender] {t}" for t in data.exporter_warnings]
        self.log.setPlainText("\n".join(lines))
        self.table.setRowCount(0)
        self._set_enabled(True)

    def _inspect(self) -> None:
        if self._manifest is None:
            return
        try:
            text = api.inspect_usd(self._manifest.geometry_path)
        except Exception as exc:  # noqa: BLE001
            text = f"Inspect に失敗しました: {exc}"
        self.log.setPlainText(text)

    def _import(self) -> None:
        if self._manifest is None:
            return

        try:
            result = api.import_asset(
                self.path_field.text().strip(),
                shader_type=self.shader.currentData(),
                case_insensitive=self.matching.currentData(),
                texture_path_mode=self.tex_mode.currentData(),
                on_existing=self.on_existing.currentData(),
                object_name=self.object_name.text().strip(),
                apply_zup_fix=self.zup_fix.isChecked(),
            )
        except Exception as exc:  # noqa: BLE001
            self.log.setPlainText(f"インポートに失敗しました: {exc}")
            return

        self._fill_table(result.rows)

        lines = [f"生成先: {result.object_path}"]
        lines += [f"[info] {t}" for t in result.info]
        lines += [f"[warn] {t}" for t in result.warnings]
        self.log.setPlainText("\n".join(lines))

        if result.obj is not None:
            try:
                result.obj.setSelected(True, clear_all_selected=True)
            except hou.OperationFailed:
                pass

    def _fill_table(self, rows) -> None:
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [row["source_name"], row["node"], row["used_by"]]
            values += [row["channels"].get(name, "-") for name in CHANNEL_ORDER]
            values.append(row.get("prims", "-"))
            for column, text in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(text))
                if column >= 3:
                    item.setTextAlignment(QtCore.Qt.AlignCenter)
                self.table.setItem(row_index, column, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch
        )


def show():
    """シェルフツール / メニューから呼ぶ。"""
    global _dialog
    if _dialog is None:
        _dialog = ImportDialog(parent=hou.qt.mainWindow())
    _dialog.show()
    _dialog.raise_()
    return _dialog
