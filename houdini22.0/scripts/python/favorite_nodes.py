"""favorite_nodes.py

選択したノードネットワークに名前を付けて保存し、名前を選んで読み込むツール。

見た目は Houdini ネイティブ。スタイルシートは当てず、hou.qt のウィジェットと
Houdini のパレットをそのまま使う。テーマを変えても追従する。

保存先は  <root>/<コンテキスト名>/<名前>.cpio  で、フォルダ分けは
Houdini 側の子ノードカテゴリから自動で決まる。

    <root>/
        Sop/
            osm_road_to_curve.cpio
            building_extrude.cpio
        Object/
            crs_transform_setup.cpio
        Driver/
            usd_export.cpio

root は環境変数 FAVNODES_ROOT。未設定なら $HOUDINI_USER_PREF_DIR/favorite_nodes。

使い方:
    Python Panel  … createInterface() をエントリポイントに指定する。
    シェルフツール … import favorite_nodes; favorite_nodes.show()
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import hou

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:                                    # Houdini 19.5 以前
    from PySide2 import QtCore, QtGui, QtWidgets       # type: ignore


EXT = ".cpio"
ENV_ROOT = "FAVNODES_ROOT"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
PREVIEW_LIMIT = 30
LABEL_WIDTH = 96


# ------------------------------------------------------------- native bits


def mono_font(size: int = 9) -> QtGui.QFont:
    font = QtGui.QFont("Consolas", size)
    font.setStyleHint(QtGui.QFont.Monospace)
    font.setFixedPitch(True)
    return font


def _band_colour(widget: QtWidgets.QWidget) -> QtGui.QColor:
    """グループ見出しの帯。Houdini のテーマから作るのでテーマに追従する。"""
    base = widget.palette().color(QtGui.QPalette.Window)
    return base.lighter(118) if base.lightness() < 128 else base.darker(106)


def _dim_colour(widget: QtWidgets.QWidget) -> QtGui.QColor:
    text = widget.palette().color(QtGui.QPalette.WindowText)
    window = widget.palette().color(QtGui.QPalette.Window)
    return QtGui.QColor(
        (text.red() + window.red() * 2) // 3,
        (text.green() + window.green() * 2) // 3,
        (text.blue() + window.blue() * 2) // 3,
    )


def field_label(text: str) -> QtWidgets.QWidget:
    """hou.qt があればそれを使う。無ければ同じ見た目の QLabel。"""
    try:
        label = hou.qt.FieldLabel(text)
        label.setFixedWidth(LABEL_WIDTH)
        return label
    except AttributeError:
        label = QtWidgets.QLabel(text)
        label.setFixedWidth(LABEL_WIDTH)
        label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        return label


def combo_box() -> QtWidgets.QComboBox:
    try:
        return hou.qt.ComboBox()
    except AttributeError:
        return QtWidgets.QComboBox()


def search_field() -> QtWidgets.QLineEdit:
    try:
        return hou.qt.SearchLineEdit()
    except AttributeError:
        edit = QtWidgets.QLineEdit()
        edit.setPlaceholderText("Search")
        return edit


class Group(QtWidgets.QWidget):
    """見出しの帯 + 中身。Import Blender Asset と同じ組み方。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QtWidgets.QLabel(title)
        header.setAutoFillBackground(True)
        header.setContentsMargins(8, 4, 8, 4)
        palette = header.palette()
        palette.setColor(QtGui.QPalette.Window, _band_colour(self))
        palette.setColor(QtGui.QPalette.WindowText, _dim_colour(self))
        header.setPalette(palette)
        outer.addWidget(header)

        body = QtWidgets.QWidget()
        outer.addWidget(body, 1)

        self.grid = QtWidgets.QGridLayout(body)
        self.grid.setContentsMargins(10, 8, 10, 10)
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(5)
        self.grid.setColumnStretch(1, 1)
        self._rows = 0

    def row(self, label: str, widget, tooltip: str = ""):
        """ラベル + コントロールを 1 行足す。"""
        text = field_label(label)
        if tooltip:
            text.setToolTip(tooltip)
            widget.setToolTip(tooltip)
        self.grid.addWidget(text, self._rows, 0,
                            QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        if isinstance(widget, QtWidgets.QLayout):
            self.grid.addLayout(widget, self._rows, 1)
        else:
            self.grid.addWidget(widget, self._rows, 1)
        self._rows += 1
        return widget

    def span(self, widget, stretch: int = 0):
        """ラベル無しで 2 列ぶち抜き。"""
        if isinstance(widget, QtWidgets.QLayout):
            self.grid.addLayout(widget, self._rows, 0, 1, 2)
        else:
            self.grid.addWidget(widget, self._rows, 0, 1, 2)
        if stretch:
            self.grid.setRowStretch(self._rows, stretch)
        self._rows += 1
        return widget

    def hint(self, text: str = "") -> QtWidgets.QLabel:
        """フィールドの下に出す薄い説明。"""
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        palette = label.palette()
        palette.setColor(QtGui.QPalette.WindowText, _dim_colour(self))
        label.setPalette(palette)
        self.grid.addWidget(label, self._rows, 1)
        self._rows += 1
        return label


# ---------------------------------------------------------------- library


@dataclass(frozen=True)
class Entry:
    """ライブラリに保存された 1 件。"""

    name: str
    context: str
    path: Path

    @property
    def modified(self) -> datetime:
        return datetime.fromtimestamp(self.path.stat().st_mtime)

    @property
    def size_kb(self) -> float:
        return self.path.stat().st_size / 1024.0


def library_root() -> Path:
    root = os.environ.get(ENV_ROOT) or os.path.join(
        hou.homeHoudiniDirectory(), "favorite_nodes"
    )
    path = Path(os.path.expandvars(root)).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def known_contexts() -> list:
    """ライブラリに 1 件以上入っているコンテキスト名。"""
    return sorted(
        d.name
        for d in library_root().iterdir()
        if d.is_dir() and any(d.glob("*" + EXT))
    )


def list_entries(context: str) -> list:
    folder = library_root() / context
    if not folder.is_dir():
        return []
    return sorted(
        (Entry(p.stem, context, p) for p in folder.glob("*" + EXT)),
        key=lambda e: e.name.lower(),
    )


def entry_path(context: str, name: str) -> Path:
    return library_root() / context / (name + EXT)


def validate_name(name: str):
    """問題があればエラーメッセージ、なければ None を返す。"""
    if not name:
        return "名前を入力してください"
    if not NAME_PATTERN.match(name):
        return "使えるのは半角英数字・アンダースコア・ハイフンだけです"
    return None


# ---------------------------------------------------------------- houdini


def current_network_editor():
    pane = hou.ui.paneTabUnderCursor()
    if isinstance(pane, hou.NetworkEditor):
        return pane
    pane = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
    return pane if isinstance(pane, hou.NetworkEditor) else None


def current_parent():
    """いまネットワークエディタで開いている階層のノード。"""
    editor = current_network_editor()
    return editor.pwd() if editor else None


def context_of(parent) -> str:
    """子ノードのカテゴリ名。Sop / Object / Driver / Lop など。"""
    return parent.childTypeCategory().name()


def item_label(item) -> str:
    """保存対象の 1 件を 1 行で表す。"""
    if isinstance(item, hou.NetworkBox):
        return "[netbox]  " + item.name()
    if isinstance(item, hou.StickyNote):
        return "[note]    " + item.name()
    if isinstance(item, hou.Node):
        return "{}   ({})".format(item.name(), item.type().name())
    return str(item)


def export_selection(name: str):
    """選択中のアイテムを .cpio に書き出す。"""
    parent = current_parent()
    if parent is None:
        raise RuntimeError("ネットワークエディタが見つかりません")

    items = parent.selectedItems()
    if not items:
        raise RuntimeError("ノードが選択されていません")

    context = context_of(parent)
    path = entry_path(context, name)
    path.parent.mkdir(parents=True, exist_ok=True)

    parent.saveItemsToFile(items, str(path))
    return Entry(name, context, path), len(items)


def import_entry(entry: Entry) -> list:
    """.cpio を現在の階層に読み込み、カーソル位置に置く。"""
    editor = current_network_editor()
    if editor is None:
        raise RuntimeError("ネットワークエディタが見つかりません")

    parent = editor.pwd()
    context = context_of(parent)
    if context != entry.context:
        raise RuntimeError(
            "コンテキストが違います（保存: {} / 現在: {}）".format(entry.context, context)
        )

    before = set(parent.allItems())
    with hou.undos.group("Import " + entry.name):
        parent.loadItemsFromFile(str(entry.path))
        new_items = [i for i in parent.allItems() if i not in before]
        _move_to_cursor(editor, new_items)
        _select_only(new_items)
    return new_items


def _move_to_cursor(editor, items: list) -> None:
    """読み込んだアイテムの左上をカーソル位置に合わせる。"""
    if not items:
        return
    try:
        positions = [i.position() for i in items]
        top_left = hou.Vector2(
            min(p.x() for p in positions), max(p.y() for p in positions)
        )
        offset = editor.cursorPosition() - top_left
        for item in items:
            item.move(offset)
    except (hou.OperationFailed, AttributeError):
        # 位置調整に失敗しても読み込み自体は成功しているので黙って諦める
        pass


def _select_only(items: list) -> None:
    for index, item in enumerate(items):
        try:
            item.setSelected(True, clear_all_selected=(index == 0))
        except AttributeError:
            pass


# ---------------------------------------------------------------- ui


class FavoriteNodesPanel(QtWidgets.QWidget):

    HEADERS = ("Name", "Modified", "Size")

    def __init__(self, embedded: bool = False, parent=None):
        super().__init__(parent)
        self._embedded = embedded

        self._context = None        # いま開いている階層のコンテキスト
        self._entries = {}          # table row -> Entry
        self._selection_count = 0

        self._build()

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self._sync)
        self._timer.start()

        self._sync(force=True)

    # -- construction -------------------------------------------------

    def _build(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_save_group())
        root.addWidget(self._build_library_group(), 1)
        root.addWidget(self._build_log_group())
        root.addLayout(self._build_button_bar())

    def _build_save_group(self) -> Group:
        group = Group("Save")

        self.name_field = QtWidgets.QLineEdit()
        self.name_field.setPlaceholderText("〈保存する名前〉")
        self.name_field.returnPressed.connect(self.on_export)
        self.name_field.textChanged.connect(lambda _t: self._update_state())
        group.row("Name", self.name_field)
        self.name_hint = group.hint("名前を入力してください")

        self.context_label = QtWidgets.QLabel("—")
        self.context_label.setFont(mono_font())
        group.row("Network", self.context_label,
                  "ネットワークエディタで開いている階層。ここのカテゴリが保存先になる")

        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono_font())
        self.preview.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.preview.setFixedHeight(84)
        group.row("Selection", self.preview)

        return group

    def _build_library_group(self) -> Group:
        group = Group("Library")

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        self.context_combo = combo_box()
        self.context_combo.setFixedWidth(150)
        self.context_combo.currentTextChanged.connect(lambda _t: self._reload_list())
        self.filter_field = search_field()
        self.filter_field.textChanged.connect(lambda _t: self._apply_filter())
        head.addWidget(self.context_combo)
        head.addWidget(self.filter_field, 1)
        group.row("Context", head)

        self.table = QtWidgets.QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.itemSelectionChanged.connect(self._update_state)
        self.table.doubleClicked.connect(lambda _index: self.on_import())
        group.span(self.table, stretch=1)

        return group

    def _build_log_group(self) -> Group:
        group = Group("Log")

        self.root_label = QtWidgets.QLabel("")
        self.root_label.setFont(mono_font(8))
        self.root_label.setWordWrap(True)
        palette = self.root_label.palette()
        palette.setColor(QtGui.QPalette.WindowText, _dim_colour(self))
        self.root_label.setPalette(palette)
        group.span(self.root_label)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(mono_font())
        self.log.setFixedHeight(96)
        group.span(self.log)

        return group

    def _build_button_bar(self) -> QtWidgets.QHBoxLayout:
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 8)
        bar.setSpacing(6)

        self.delete_button = QtWidgets.QPushButton("Delete")
        self.delete_button.clicked.connect(self.on_delete)
        bar.addWidget(self.delete_button)
        bar.addStretch(1)

        if not self._embedded:
            close_button = QtWidgets.QPushButton("Close")
            close_button.clicked.connect(self.close)
            bar.addWidget(close_button)

        self.export_button = QtWidgets.QPushButton("Save")
        self.export_button.clicked.connect(self.on_export)
        bar.addWidget(self.export_button)

        self.import_button = QtWidgets.QPushButton("Import")
        self.import_button.setDefault(True)
        self.import_button.clicked.connect(self.on_import)
        bar.addWidget(self.import_button)

        return bar

    # -- state --------------------------------------------------------

    def _sync(self, force: bool = False) -> None:
        """開いている階層と選択を追いかける。タイマーから毎回呼ばれる。"""
        self.root_label.setText(str(library_root()))
        parent = current_parent()

        if parent is None:
            self._context = None
            self._selection_count = 0
            self.context_label.setText("ネットワークエディタが見つかりません")
            self.preview.setPlainText("")
            self._update_state()
            return

        context = context_of(parent)
        changed = force or context != self._context
        self._context = context
        self.context_label.setText("{}   ({})".format(parent.path(), context))

        items = parent.selectedItems()
        if force or len(items) != self._selection_count:
            self._selection_count = len(items)
            self._refresh_preview(items)

        if changed:
            self._reload_contexts(context)
            self._reload_list()

        self._update_state()

    def _refresh_preview(self, items) -> None:
        if not items:
            self.preview.setPlainText("（ノードが選択されていません）")
            return
        lines = [item_label(i) for i in items[:PREVIEW_LIMIT]]
        if len(items) > PREVIEW_LIMIT:
            lines.append("… 他 {} 個".format(len(items) - PREVIEW_LIMIT))
        self.preview.setPlainText("\n".join(lines))

    def _reload_contexts(self, current: str) -> None:
        contexts = known_contexts()
        if current not in contexts:
            contexts.append(current)
            contexts.sort()

        self.context_combo.blockSignals(True)
        self.context_combo.clear()
        self.context_combo.addItems(contexts)
        index = self.context_combo.findText(current)
        self.context_combo.setCurrentIndex(max(index, 0))
        self.context_combo.blockSignals(False)

    def _reload_list(self) -> None:
        context = self.context_combo.currentText()
        entries = list_entries(context) if context else []

        self.table.setRowCount(0)
        self._entries = {}
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)

            name_item = QtWidgets.QTableWidgetItem(entry.name)
            name_item.setFont(mono_font())

            date_item = QtWidgets.QTableWidgetItem(
                entry.modified.strftime("%Y-%m-%d %H:%M"))

            size_item = QtWidgets.QTableWidgetItem(
                "{:.1f} KB".format(entry.size_kb))
            size_item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, date_item)
            self.table.setItem(row, 2, size_item)
            self._entries[row] = entry

        self._fit_columns()
        self._apply_filter()
        self._update_state()

    def _fit_columns(self) -> None:
        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        for column in (1, 2):
            self.table.setColumnWidth(column, self.table.columnWidth(column) + 16)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setStretchLastSection(False)

    def _apply_filter(self) -> None:
        needle = self.filter_field.text().lower()
        for row, entry in self._entries.items():
            self.table.setRowHidden(
                row, bool(needle) and needle not in entry.name.lower())

    def _selected_entry(self):
        rows = {index.row() for index in self.table.selectedIndexes()}
        if not rows:
            return None
        return self._entries.get(rows.pop())

    def _update_state(self) -> None:
        entry = self._selected_entry()
        matches = bool(entry) and entry.context == self._context
        name = self.name_field.text().strip()

        self.export_button.setEnabled(
            self._context is not None and self._selection_count > 0 and bool(name)
        )
        self.import_button.setEnabled(matches)
        self.delete_button.setEnabled(bool(entry))

        # 名前欄の下の説明。押す前に何が起きるか書く
        if self._context is None:
            self.name_hint.setText("ネットワークエディタが見つかりません")
        elif not name:
            self.name_hint.setText("名前を入力してください")
        elif validate_name(name):
            self.name_hint.setText(validate_name(name))
        elif not self._selection_count:
            self.name_hint.setText("ノードが選択されていません")
        else:
            path = entry_path(self._context, name)
            verb = "上書きします" if path.exists() else "保存します"
            self.name_hint.setText("{} / {}{} に {} 個を{}".format(
                self._context, name, EXT, self._selection_count, verb))

        # 読み込みボタンが押せない理由
        if entry and not matches and self._context is not None:
            self.import_button.setToolTip(
                "{} は {} 用です。{} の階層を開いてください".format(
                    entry.name, entry.context, entry.context))
        else:
            self.import_button.setToolTip("")

    # -- log ----------------------------------------------------------

    def _log(self, level: str, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText("{}  {:<5}  {}".format(stamp, level, text))
        self.log.verticalScrollBar().setValue(
            self.log.verticalScrollBar().maximum())

    # -- actions ------------------------------------------------------

    def on_export(self) -> None:
        name = self.name_field.text().strip()
        error = validate_name(name)
        if error:
            self._log("error", error)
            return

        parent = current_parent()
        if parent is None:
            self._log("error", "ネットワークエディタが見つかりません")
            return

        path = entry_path(context_of(parent), name)
        if path.exists():
            choice = hou.ui.displayMessage(
                "{} はすでにあります。上書きしますか？".format(name),
                buttons=("上書き", "キャンセル"),
                close_choice=1,
            )
            if choice != 0:
                return

        try:
            entry, count = export_selection(name)
        except (RuntimeError, hou.OperationFailed) as err:
            self._log("error", str(err))
            return

        self.name_field.clear()
        self._sync(force=True)
        self._select_by_name(entry.name)
        self._log("ok", "{} 個を保存: {}".format(count, entry.path))

    def on_import(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        try:
            items = import_entry(entry)
        except (RuntimeError, hou.OperationFailed) as err:
            self._log("error", str(err))
            return
        self._log("ok", "{} を読み込み: {} 個をカーソル位置に配置".format(
            entry.name, len(items)))

    def on_delete(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        choice = hou.ui.displayMessage(
            "{} を削除しますか？".format(entry.name),
            buttons=("削除", "キャンセル"),
            close_choice=1,
            severity=hou.severityType.Warning,
        )
        if choice != 0:
            return
        entry.path.unlink()
        self._sync(force=True)
        self._log("ok", "削除: {}".format(entry.path))

    def _select_by_name(self, name: str) -> None:
        for row, entry in self._entries.items():
            if entry.name == name:
                self.table.selectRow(row)
                return


# ---------------------------------------------------------------- entry points

_window = None


def createInterface():
    """Python Panel 用のエントリポイント。"""
    return FavoriteNodesPanel(embedded=True)


def show():
    """シェルフツール用。フローティングウィンドウで開く。"""
    global _window
    if _window is not None:
        _window.close()
    _window = FavoriteNodesPanel(embedded=False, parent=hou.qt.mainWindow())
    _window.setWindowFlags(QtCore.Qt.Window)
    _window.setWindowTitle("Favorite Nodes")
    _window.resize(560, 660)
    _window.show()
    return _window