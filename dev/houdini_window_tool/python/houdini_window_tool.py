"""窓抽出 → UV → 3D 配置 を Houdini の中から回すツール。

UI パーツは ``dcc_ui.py`` から必要な分だけこのファイルに取り込んである
（Section / Form / PathField / Segmented / ToggleRow / StatusPill /
MessageBar / QueueList / LogPanel とトークン一式）。単体で配れる代わり、
dcc_ui 側でトークンや QSS を直したときは下の「UI kit」節を貼り直す。

配置
----
このファイルと ``extract_windows.py`` ``project_windows.vex``
``houdini_import_window_points.py`` を同じフォルダに置く。

シェルフツールから::

    import sys
    sys.path.append("D:/Projects/Work/scripts/python/window_centroids")
    import houdini_window_tool
    houdini_window_tool.show()

OpenCV の扱い
-------------
Houdini 同梱 Python には cv2 が入っていない。そこで実行方法を 2 通り持つ。

* in-process — ``import cv2`` が通るならその場で回す。速いしログも直に取れる
* subprocess — 外部の venv Python に ``extract_windows.py`` を渡して回す

起動時にどちらになるか判定して画面上部に出す。subprocess のインタプリタは
環境変数 ``WINDOW_TOOL_PYTHON`` を既定値にしてあるので、houdini.env に
書いておけば毎回入力しなくていい。

見た目の確認だけなら ``python houdini_window_tool.py`` で Houdini 無しでも開く。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from string import Template

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:                                    # Houdini 19.5 以前
    from PySide2 import QtCore, QtGui, QtWidgets       # type: ignore

try:
    import hou
except ImportError:                                    # 素の Python で見た目確認
    hou = None


# =====================================================================
# UI kit — dcc_ui.py からの取り込み。ここは編集しない
# =====================================================================


# --------------------------------------------------------------- tokens

#: HTML キットの :root と同じ値。ここだけ書き換えれば全体の見た目が変わる。
TOKENS = {
    "s_page": "#191a1c",
    "s_panel": "#212327",
    "s_raise": "#2a2c31",
    "s_well": "#151619",

    "rule_hard": "#0e0f11",
    "rule_soft": "#33363c",

    "fg": "#e6e7ea",
    "fg_dim": "#9298a2",
    "fg_faint": "#636870",

    # アンバーは操作にだけ使う（選択・フォーカス・主要ボタン）
    "accent": "#eda94a",
    "accent_ink": "#1e1608",

    # 状態。操作には使わない
    "ok": "#8fbf6b",
    "warn": "#cf8b3a",
    "err": "#d0605a",
    "info": "#6f9ed6",

    "radius": "2px",
}

LEVELS = ("ok", "warn", "err", "info", "mute")

_UI_FAMILIES = ("Inter", "Segoe UI Variable Text", "Segoe UI", "Noto Sans JP",
                "Hiragino Kaku Gothic ProN", "Yu Gothic UI")
_MONO_FAMILIES = ("JetBrains Mono", "Cascadia Mono", "SF Mono", "Consolas",
                  "Roboto Mono", "DejaVu Sans Mono", "Monospace")


def _available_families():
    database = QtGui.QFontDatabase
    try:                                   # Qt 6: static
        return set(database.families())
    except TypeError:                      # Qt 5: instance
        return set(database().families())


def _pick(candidates, fallback):
    available = _available_families()
    for name in candidates:
        if name in available:
            return name
    return fallback


def ui_font(size: int = 12, bold: bool = False) -> QtGui.QFont:
    font = QtGui.QFont(_pick(_UI_FAMILIES, "Sans Serif"), size)
    font.setBold(bold)
    return font


def mono_font(size: int = 11) -> QtGui.QFont:
    return QtGui.QFont(_pick(_MONO_FAMILIES, "Monospace"), size)



# ----------------------------------------------------------- stylesheet

# Qt のスタイルシートは CSS 変数を持たないので Template で流し込む。
# QSS に $ は出てこないので衝突しない。
_QSS = Template("""
QWidget {
    background: $s_page;
    color: $fg;
}

QLabel { background: transparent; }
QLabel[role="label"]   { color: $fg_dim; }
QLabel[role="caption"] { color: $fg_faint; }
QLabel[role="head"] {
    color: $fg_faint;
    font-size: 10px;
    font-weight: 600;
}

/* ---- ui::section ---- */
QFrame#Section {
    background: $s_panel;
    border: 1px solid $rule_soft;
    border-radius: $radius;
}
QLabel#SectionHead {
    color: $fg_faint;
    padding: 7px 11px;
    border-bottom: 1px solid $rule_soft;
    font-weight: 600;
}

/* ---- 入力欄は「掘る」 ---- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {
    background: $s_well;
    border: 1px solid $rule_hard;
    border-radius: $radius;
    padding: 4px 7px;
    selection-background-color: $accent;
    selection-color: $accent_ink;
}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: $accent;
}
QLineEdit[state="bad"] { border-color: $err; }
QLineEdit:disabled, QPlainTextEdit:disabled { color: $fg_faint; }

/* ---- 押せるものは「盛る」 ---- */
QPushButton {
    background: $s_raise;
    border: 1px solid $rule_hard;
    border-radius: $radius;
    padding: 4px 12px;
    color: $fg;
}
QPushButton:hover  { background: #34373d; }
QPushButton:pressed { background: #1e2024; }
QPushButton:disabled { color: $fg_faint; background: $s_panel; }

QPushButton[variant="primary"] {
    background: $accent;
    border-color: $accent;
    color: $accent_ink;
    font-weight: 600;
}
QPushButton[variant="primary"]:hover   { background: #f5b862; }
QPushButton[variant="primary"]:disabled { background: #4a4234; color: #7d7565; border-color: #4a4234; }
QPushButton[variant="ghost"] {
    background: transparent;
    border-color: $rule_soft;
    color: $fg_dim;
}
QPushButton[variant="ghost"]:hover { color: $fg; border-color: $fg_faint; }
QPushButton[variant="danger"] {
    background: transparent;
    border-color: $err;
    color: $err;
}

/* ---- ui::segmented ---- */
QWidget#Segmented QPushButton {
    border-radius: 0;
    border-right-width: 0;
    padding: 4px 13px;
    color: $fg_dim;
}
QWidget#Segmented QPushButton:first-child { border-top-left-radius: $radius; border-bottom-left-radius: $radius; }
QWidget#Segmented QPushButton:last-child  { border-right-width: 1px; border-top-right-radius: $radius; border-bottom-right-radius: $radius; }
QWidget#Segmented QPushButton:checked {
    background: $accent;
    color: $accent_ink;
    font-weight: 600;
}

QComboBox {
    background: $s_raise;
    border: 1px solid $rule_hard;
    border-radius: $radius;
    padding: 4px 7px;
}
QComboBox:focus { border-color: $accent; }
QComboBox::drop-down { border: 0; width: 18px; }
QComboBox QAbstractItemView {
    background: $s_raise;
    border: 1px solid $rule_hard;
    selection-background-color: $accent;
    selection-color: $accent_ink;
    outline: 0;
}

QCheckBox { spacing: 7px; }
QCheckBox::indicator {
    width: 12px; height: 12px;
    background: $s_well;
    border: 1px solid $rule_hard;
    border-radius: $radius;
}
QCheckBox::indicator:checked { background: $accent; border-color: $accent; }

/* ---- 一覧 ---- */
QTableWidget, QTableView, QTreeWidget, QListWidget {
    background: $s_panel;
    border: 1px solid $rule_hard;
    border-radius: $radius;
    gridline-color: transparent;
    outline: 0;
    alternate-background-color: $s_panel;
}
QTableWidget::item, QTreeWidget::item, QListWidget::item {
    padding: 4px 6px;
    border-bottom: 1px solid $rule_soft;
}
QTableWidget::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {
    background: $accent;
    color: $accent_ink;
}
QHeaderView::section {
    background: $s_panel;
    color: $fg_faint;
    border: 0;
    border-bottom: 1px solid $rule_soft;
    padding: 5px 6px;
    font-weight: 600;
}

/* ---- ui::message_bar ---- */
QFrame#MessageBar {
    background: $s_raise;
    border: 0;
    border-left: 2px solid $rule_soft;
    border-radius: 0;
}
QFrame#MessageBar[level="ok"]   { border-left-color: $ok; }
QFrame#MessageBar[level="warn"] { border-left-color: $warn; }
QFrame#MessageBar[level="err"]  { border-left-color: $err; }
QFrame#MessageBar[level="info"] { border-left-color: $info; }
QLabel#MessageTitle[level="ok"]   { color: $ok; font-weight: 600; }
QLabel#MessageTitle[level="warn"] { color: $warn; font-weight: 600; }
QLabel#MessageTitle[level="err"]  { color: $err; font-weight: 600; }
QLabel#MessageTitle[level="info"] { color: $info; font-weight: 600; }
QLabel#MessageBody { color: $fg_dim; }

/* ---- ui::status_pill ---- */
QLabel#StatusPill {
    border-radius: 8px;
    padding: 1px 8px;
    font-size: 10px;
}
QLabel#StatusPill[level="ok"]   { background: rgba(143,191,107,0.13); color: $ok; }
QLabel#StatusPill[level="warn"] { background: rgba(207,139,58,0.13);  color: $warn; }
QLabel#StatusPill[level="err"]  { background: rgba(208,96,90,0.13);   color: $err; }
QLabel#StatusPill[level="info"] { background: rgba(111,158,214,0.13); color: $info; }
QLabel#StatusPill[level="mute"] { background: $s_raise;               color: $fg_faint; }

/* ---- ui::structure_preview / log ---- */
QPlainTextEdit#Preview, QPlainTextEdit#Log {
    background: $s_well;
    border: 1px solid $rule_hard;
    color: $fg_dim;
}

QProgressBar {
    background: $s_well;
    border: 0;
    border-radius: 2px;
    height: 4px;
    text-align: center;
}
QProgressBar::chunk { background: $accent; border-radius: 2px; }
QProgressBar[state="done"]::chunk { background: $ok; }
QProgressBar[state="fail"]::chunk { background: $err; }

QFrame#Row      { background: $s_panel; border: 0; border-bottom: 1px solid $rule_soft; }
QFrame#Row[active="true"] { background: $s_raise; }
QFrame#Divider  { background: $rule_soft; border: 0; max-height: 1px; }

QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical   { background: transparent; width: 10px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle { background: #3c4046; border-radius: 5px; min-height: 24px; min-width: 24px; }
QScrollBar::handle:hover { background: #4c5158; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QToolTip {
    background: $s_raise;
    color: $fg;
    border: 1px solid $rule_hard;
    padding: 4px 7px;
}
""")


def stylesheet(tokens: dict = None) -> str:
    """ダイアログのルートに setStyleSheet() する。"""
    return _QSS.substitute(dict(TOKENS, **(tokens or {})))


def _repolish(widget: QtWidgets.QWidget) -> None:
    """動的プロパティを変えた後、スタイルを適用し直す。"""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()



# ------------------------------------------------------------ ui::section

class Section(QtWidgets.QFrame):
    """見出し付きの枠。中身は ``body`` レイアウトに積む。

    中身が 1〜2 行しかないなら使わない。枠だけ増えて密度が下がる。
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Section")

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QtWidgets.QLabel(title.upper())
        head.setObjectName("SectionHead")
        font = ui_font(9, bold=True)
        font.setLetterSpacing(QtGui.QFont.PercentageSpacing, 113)
        head.setFont(font)
        outer.addWidget(head)

        inner = QtWidgets.QWidget()
        outer.addWidget(inner)

        self.body = QtWidgets.QVBoxLayout(inner)
        self.body.setContentsMargins(11, 10, 11, 11)
        self.body.setSpacing(6)

    def add(self, widget_or_layout):
        if isinstance(widget_or_layout, QtWidgets.QLayout):
            self.body.addLayout(widget_or_layout)
        else:
            self.body.addWidget(widget_or_layout)
        return widget_or_layout


# ----------------------------------------------------------- ui::param_row

class Form(QtWidgets.QWidget):
    """ラベル右揃え + コントロールの行を積む。

    ラベル幅はパネル内で固定する。行ごとに変えると視線が左右に揺れる。
    """

    def __init__(self, label_width: int = 112, parent=None):
        super().__init__(parent)
        self.label_width = label_width
        self._grid = QtWidgets.QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(6)
        self._grid.setColumnStretch(1, 1)
        self._rows = 0

    def row(self, label: str, widget, tooltip: str = ""):
        """1 行足して、渡したウィジェットをそのまま返す。"""
        text = QtWidgets.QLabel(label)
        text.setProperty("role", "label")
        text.setFixedWidth(self.label_width)
        text.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        if tooltip:
            text.setToolTip(tooltip)
            widget.setToolTip(tooltip)

        align = (QtCore.Qt.AlignRight | QtCore.Qt.AlignTop
                 if isinstance(widget, (QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit))
                 else QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        self._grid.addWidget(text, self._rows, 0, align)
        if isinstance(widget, QtWidgets.QLayout):
            self._grid.addLayout(widget, self._rows, 1)
        else:
            self._grid.addWidget(widget, self._rows, 1)
        self._rows += 1
        return widget

    def span(self, widget):
        """ラベル無しで 2 列ぶち抜き。"""
        self._grid.addWidget(widget, self._rows, 0, 1, 2)
        self._rows += 1
        return widget



# ---------------------------------------------------------- ui::path_field

class PathField(QtWidgets.QWidget):
    """パス入力 + Browse + 解決結果の表示。

    変数を展開した結果を必ず見せる。``$JOB`` の中身を勘違いしたまま
    実行する事故が一番多い。

    Houdini から使うときは resolver に hou.expandString を渡す::

        PathField(mode="file", pattern="*.bridge.json", resolver=hou.expandString)
    """

    changed = QtCore.Signal(str)

    def __init__(self, mode: str = "dir", pattern: str = "", resolver=None,
                 placeholder: str = "", parent=None):
        super().__init__(parent)
        self.mode = mode
        self.pattern = pattern
        self.resolver = resolver or os.path.expandvars

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)

        self.edit = QtWidgets.QLineEdit()
        self.edit.setFont(mono_font(10))
        self.edit.setPlaceholderText(placeholder)
        self.edit.textChanged.connect(self._on_change)

        browse = QtWidgets.QPushButton("Browse…")
        browse.setFixedWidth(84)
        browse.clicked.connect(self._browse)

        row.addWidget(self.edit, 1)
        row.addWidget(browse)
        outer.addLayout(row)

        self.readout = QtWidgets.QLabel("")
        self.readout.setFont(mono_font(9))
        self.readout.setProperty("role", "caption")
        self.readout.setWordWrap(True)
        outer.addWidget(self.readout)

        self._on_change("")

    # --- api ---------------------------------------------------------

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:
        self.edit.setText(value)

    def resolved(self) -> str:
        raw = self.text()
        return self.resolver(raw) if raw else ""

    def is_valid(self) -> bool:
        path = self.resolved()
        if not path:
            return False
        return os.path.isdir(path) if self.mode == "dir" else os.path.isfile(path)

    # --- internals ---------------------------------------------------

    def _on_change(self, _text: str) -> None:
        raw = self.text()
        if not raw:
            self.readout.setText("")
            self.edit.setProperty("state", "")
            _repolish(self.edit)
            self.changed.emit("")
            return

        path = self.resolved()
        ok = self.is_valid()
        colour = TOKENS["ok"] if ok else TOKENS["err"]
        suffix = "" if ok else "  （見つかりません）"
        self.readout.setText(f"→ <span style='color:{colour};'>{path}{suffix}</span>")
        self.readout.setTextFormat(QtCore.Qt.RichText)
        self.edit.setProperty("state", "" if ok else "bad")
        _repolish(self.edit)
        self.changed.emit(path)

    def _browse(self) -> None:
        start = self.resolved() or os.getcwd()
        if not os.path.isdir(start):
            start = os.path.dirname(start) or os.getcwd()

        if self.mode == "dir":
            picked = QtWidgets.QFileDialog.getExistingDirectory(self, "フォルダを選ぶ", start)
        else:
            picked, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "ファイルを選ぶ", start, self.pattern or "All files (*)")
        if picked:
            self.edit.setText(picked)


# ----------------------------------------------------------- ui::segmented

class Segmented(QtWidgets.QWidget):
    """排他の選択肢。2〜4 個まで。

    5 個以上、またはラベルが長いときは QComboBox にする。
    """

    changed = QtCore.Signal(object)

    def __init__(self, options, current=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Segmented")

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)
        self._values = []

        for index, option in enumerate(options):
            value, label = option if isinstance(option, (tuple, list)) else (option, str(option))
            button = QtWidgets.QPushButton(label)
            button.setCheckable(True)
            button.setCursor(QtCore.Qt.PointingHandCursor)
            # QSS の padding は sizeHint に反映されないので、自前で最小幅を出す。
            # 選択中はボールドになるぶん幅が増えるため、ボールドで測る。
            bold = QtGui.QFont(button.font())
            bold.setBold(True)
            metrics = QtGui.QFontMetrics(bold)
            button.setMinimumWidth(metrics.horizontalAdvance(label) + 32)
            self._group.addButton(button, index)
            layout.addWidget(button)
            self._values.append(value)

        layout.addStretch(1)
        self._group.idClicked.connect(
            lambda i: self.changed.emit(self._values[i])
        )
        self.setValue(current if current is not None else self._values[0])

    def value(self):
        index = self._group.checkedId()
        return self._values[index] if index >= 0 else None

    def setValue(self, value) -> None:
        if value in self._values:
            self._group.button(self._values.index(value)).setChecked(True)


# ---------------------------------------------------------- ui::toggle_row

class ToggleRow(QtWidgets.QWidget):
    """オン/オフ。結果が分かりにくいものだけ説明を添える。"""

    toggled = QtCore.Signal(bool)

    def __init__(self, text: str, description: str = "", checked: bool = False,
                 parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(1)

        self.check = QtWidgets.QCheckBox(text)
        self.check.setChecked(checked)
        self.check.toggled.connect(self.toggled)
        layout.addWidget(self.check)

        if description:
            note = QtWidgets.QLabel(description)
            note.setProperty("role", "caption")
            note.setWordWrap(True)
            note.setContentsMargins(19, 0, 0, 0)
            layout.addWidget(note)

    def isChecked(self) -> bool:
        return self.check.isChecked()

    def setChecked(self, value: bool) -> None:
        self.check.setChecked(value)



# --------------------------------------------------------- ui::status_pill

class StatusPill(QtWidgets.QLabel):
    """1 件の結果ラベル。色 + 語。語だけ読んでも分かる状態に保つ。

    ツールごとに「OK / 成功 / done」と揺らさない。語彙を固定して使い回す。
    """

    def __init__(self, text: str, level: str = "mute", parent=None):
        super().__init__(text, parent)
        self.setObjectName("StatusPill")
        self.setFont(ui_font(9))
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setSizePolicy(QtWidgets.QSizePolicy.Maximum,
                           QtWidgets.QSizePolicy.Maximum)
        self.setLevel(level)
        self._fit()

    def setText(self, text: str) -> None:
        super().setText(text)
        self._fit()

    def _fit(self) -> None:
        metrics = QtGui.QFontMetrics(self.font())
        self.setMinimumWidth(metrics.horizontalAdvance(self.text()) + 20)

    def setLevel(self, level: str) -> None:
        self.setProperty("level", level if level in LEVELS else "mute")
        _repolish(self)



# --------------------------------------------------------- ui::message_bar

class MessageBar(QtWidgets.QFrame):
    """1 件の出来事。何が起きたかと、次に何をすればいいかをセットで書く。

    謝らない。ぼかさない。「失敗しました」で終わらせない。
    """

    def __init__(self, title: str, body: str = "", level: str = "info", parent=None):
        super().__init__(parent)
        self.setObjectName("MessageBar")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(11, 8, 11, 9)
        layout.setSpacing(2)

        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("MessageTitle")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)

        self.body = QtWidgets.QLabel(body)
        self.body.setObjectName("MessageBody")
        self.body.setWordWrap(True)
        self.body.setVisible(bool(body))
        layout.addWidget(self.body)

        self.setLevel(level)

    def setLevel(self, level: str) -> None:
        level = level if level in ("ok", "warn", "err", "info") else "info"
        for widget in (self, self.title):
            widget.setProperty("level", level)
            _repolish(widget)

    def setMessage(self, title: str, body: str = "", level: str = None) -> None:
        self.title.setText(title)
        self.body.setText(body)
        self.body.setVisible(bool(body))
        if level:
            self.setLevel(level)



# ---------------------------------------------------------- ui::queue_list

class QueueList(QtWidgets.QScrollArea):
    """複数件をまとめて処理する画面。1 行 1 件。

    失敗しても止めない。残りを流し切って、最後に「失敗だけ再実行」を出す。
    失敗の理由は行に書く。ログを開かせない。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)

        holder = QtWidgets.QWidget()
        self.setWidget(holder)
        self._layout = QtWidgets.QVBoxLayout(holder)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addStretch(1)
        self._rows = {}

    def add(self, key: str, name: str, note: str = ""):
        row = QtWidgets.QFrame()
        row.setObjectName("Row")

        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(11)

        text = QtWidgets.QVBoxLayout()
        text.setSpacing(0)
        title = QtWidgets.QLabel(name)
        title.setFont(mono_font(10))
        note_label = QtWidgets.QLabel(note)
        note_label.setProperty("role", "caption")
        text.addWidget(title)
        text.addWidget(note_label)
        layout.addLayout(text, 1)

        pill = StatusPill("queued", "mute")
        pill.setFixedWidth(78)
        layout.addWidget(pill)

        bar = QtWidgets.QProgressBar()
        bar.setTextVisible(False)
        bar.setFixedWidth(120)
        # QSS の height は効かないので実寸で指定する
        bar.setFixedHeight(5)
        bar.setValue(0)
        layout.addWidget(bar)

        self._layout.insertWidget(self._layout.count() - 1, row)
        self._rows[key] = {"row": row, "note": note_label, "pill": pill, "bar": bar}
        return row

    def update(self, key: str, status: str = None, level: str = None,
               progress: int = None, note: str = None) -> None:
        entry = self._rows.get(key)
        if entry is None:
            return
        if status is not None:
            entry["pill"].setText(status)
        if level is not None:
            entry["pill"].setLevel(level)
            entry["bar"].setProperty(
                "state", {"ok": "done", "err": "fail"}.get(level, ""))
            _repolish(entry["bar"])
            entry["row"].setProperty("active", "true" if level == "info" else "false")
            _repolish(entry["row"])
        if progress is not None:
            entry["bar"].setValue(progress)
        if note is not None:
            entry["note"].setText(note)

    def clear(self) -> None:
        for entry in self._rows.values():
            entry["row"].setParent(None)
        self._rows.clear()



# ----------------------------------------------------------- ui::log_panel

class LogPanel(QtWidgets.QWidget):
    """経過の記録。重要度で絞れるようにして、既定では warn 以上だけ出す。

    バッチ実行後に相談されるとき、保存できるだけで話が早くなる。
    """

    ORDER = {"ok": 0, "info": 0, "warn": 1, "err": 2}
    PREFIX = {"ok": "ok", "info": "info", "warn": "warn", "err": "error"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._records = []

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        self.filter = Segmented(
            [(0, "all"), (1, "warn+"), (2, "error")], current=1)
        self.filter.changed.connect(lambda _v: self._render())
        head.addWidget(self.filter)
        head.addStretch(1)

        copy_button = QtWidgets.QPushButton("コピー")
        copy_button.setProperty("variant", "ghost")
        copy_button.clicked.connect(self._copy)
        save_button = QtWidgets.QPushButton("ファイルに保存")
        save_button.setProperty("variant", "ghost")
        save_button.clicked.connect(self._save)
        head.addWidget(copy_button)
        head.addWidget(save_button)
        layout.addLayout(head)

        self.view = QtWidgets.QPlainTextEdit()
        self.view.setObjectName("Log")
        self.view.setReadOnly(True)
        self.view.setFont(mono_font(10))
        layout.addWidget(self.view, 1)

    def log(self, level: str, text: str) -> None:
        self._records.append((level if level in self.ORDER else "info", text))
        self._render()

    def clear(self) -> None:
        self._records = []
        self._render()

    def text(self) -> str:
        return "\n".join(f"{self.PREFIX[lv]:>5}  {tx}" for lv, tx in self._records)

    # --- internals ---------------------------------------------------

    def _visible(self):
        threshold = self.filter.value() or 0
        return [r for r in self._records if self.ORDER[r[0]] >= threshold]

    def _render(self) -> None:
        colours = {"ok": TOKENS["ok"], "info": TOKENS["info"],
                   "warn": TOKENS["warn"], "err": TOKENS["err"]}
        rows = []
        for level, text in self._visible():
            label = self.PREFIX[level]
            rows.append(
                f"<div><span style='color:{colours[level]};'>{label:>5}</span>"
                f"&nbsp;&nbsp;<span style='color:{TOKENS['fg_dim']};'>{text}</span></div>"
            )
        self.view.clear()
        self.view.appendHtml("".join(rows) or
                             f"<span style='color:{TOKENS['fg_faint']};'>（記録なし）</span>")
        self.view.verticalScrollBar().setValue(
            self.view.verticalScrollBar().maximum())

    def _copy(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.text())

    def _save(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "ログを保存", "log.txt", "Text (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.text())


# =====================================================================
# ここからツール本体
# =====================================================================

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
EXTRACTOR = HERE / "extract_windows.py"
VEX_FILE = HERE / "project_windows.vex"
IMPORTER = HERE / "houdini_import_window_points.py"

SETTINGS_NAME = "window_centroids_tool.json"
EVENT_PREFIX = "@@"

DEFAULTS = {
    "textures": "",
    "out_dir": "",
    "match": "basecolor",
    "recursive": False,
    "detection": "white-frame",
    "glass_colour": "auto",
    "min_area": 30,
    "max_area": 100000,
    "debug": True,
    "interpreter": "",
    "model": "",
    "build_scope": "selected",
}


# --------------------------------------------------------------- 設定の保存

def settings_path() -> Path:
    if hou is not None:
        pref = hou.expandString("$HOUDINI_USER_PREF_DIR")
        if pref:
            return Path(pref) / SETTINGS_NAME
    return HERE / SETTINGS_NAME


def load_settings() -> dict:
    values = dict(DEFAULTS)
    path = settings_path()
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            values.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
    return values


def save_settings(values: dict) -> None:
    try:
        settings_path().write_text(json.dumps(values, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------- 実行バックエンド

def has_local_cv2() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


def default_interpreter() -> str:
    return os.environ.get("WINDOW_TOOL_PYTHON", "")


def expand(value: str) -> str:
    """$JOB などを Houdini の流儀で展開する。"""
    if not value:
        return ""
    return hou.expandString(value) if hou is not None else os.path.expandvars(value)


def build_arguments(values: dict) -> list[str]:
    """CLI と in-process の両方で使う引数列。"""
    arguments = [
        expand(values["textures"]),
        "--out-dir", expand(values["out_dir"]),
        "--match", values["match"],
        "--detection", values["detection"],
        "--glass-colour", values["glass_colour"],
        "--min-area", str(values["min_area"]),
        "--max-area", str(values["max_area"]),
        "--events",
    ]
    if values["recursive"]:
        arguments.append("--recursive")
    if values["debug"]:
        arguments.append("--debug")
    return arguments


class Runner(QtCore.QObject):
    """抽出を別スレッドで回し、1 行ずつ GUI スレッドへ流す。"""

    line = QtCore.Signal(str)
    finished = QtCore.Signal(int)

    def __init__(self, values: dict, interpreter: str, parent=None):
        super().__init__(parent)
        self.values = values
        self.interpreter = interpreter
        self._thread = None
        self._process = None
        self._cancelled = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

    # --- internals ---------------------------------------------------

    def _run(self) -> None:
        try:
            code = self._run_subprocess() if self.interpreter else self._run_in_process()
        except Exception as exc:
            self.line.emit(f"ERROR: {exc}")
            code = 1
        self.finished.emit(code)

    def _run_in_process(self) -> int:
        import extract_windows
        arguments = extract_windows.build_parser().parse_args(build_arguments(self.values))
        extract_windows.run(arguments, log=self.line.emit)
        return 0

    def _run_subprocess(self) -> int:
        command = [self.interpreter, str(EXTRACTOR)] + build_arguments(self.values)
        self.line.emit("$ " + subprocess.list2cmdline(command))
        # Windows でコンソール窓が一瞬出るのを抑える。
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags)
        for line in self._process.stdout:
            if self._cancelled:
                break
            self.line.emit(line.rstrip())
        self._process.wait()
        return self._process.returncode


# 選択中のセグメントは無効化しても色が残るので、ここだけ上書きする。
_EXTRA_QSS = """
QWidget#Segmented QPushButton:checked:disabled {
    background: %(s_panel)s;
    color: %(fg_faint)s;
    border-color: %(rule_hard)s;
    font-weight: 400;
}
"""


# ------------------------------------------------------------------- 本体

class WindowTool(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Window Extraction")
        self.setStyleSheet(stylesheet() + _EXTRA_QSS % TOKENS)
        self.setFont(ui_font(12))
        self.resize(760, 880)

        self.values = load_settings()
        self.runner = None
        self.results = {}      # texture name -> event dict
        self.out_dir = ""

        self._build()
        self._restore()
        self._update_backend_note()

    # --- 組み立て -----------------------------------------------------

    def _build(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(11)

        self.backend_bar = MessageBar("", "", "info")
        root.addWidget(self.backend_bar)

        # --- 入力 -----------------------------------------------------
        source = Section("Textures")
        form = Form(label_width=104)
        self.textures = form.row(
            "Folder", PathField(mode="dir", resolver=expand,
                                       placeholder="$JOB/textures"),
            "テクスチャの入ったフォルダ。1 枚だけ処理したいならそのフォルダを指定して Name Filter で絞る")
        self.out_field = form.row(
            "Output", PathField(mode="dir", resolver=expand,
                                       placeholder="$JOB/output/windows"),
            "マスク・JSON・プレビューの書き出し先")
        self.match = form.row("Name Filter", QtWidgets.QLineEdit(),
                              "ファイル名にこの文字を含む画像だけ処理する。空にすると全画像")
        self.match.setFont(mono_font(10))
        source.add(form)
        self.recursive = source.add(ToggleRow(
            "サブフォルダも探す", "建物ごとにフォルダを分けている場合に使う"))
        self.textures.changed.connect(lambda _p: self._autofill_output())
        root.addWidget(source)

        # --- 検出設定 -------------------------------------------------
        options = Section("Detection")
        option_form = Form(label_width=104)
        self.detection = option_form.row(
            "Method", Segmented([("white-frame", "White Frame"),
                                        ("glass", "Glass")]),
            "White Frame は白枠に囲まれた領域を窓とする。枠が繋がっていないテクスチャでは Glass")
        self.glass_colour = option_form.row(
            "Glass Colour", Segmented([("auto", "Auto"), ("cyan", "Cyan"),
                                              ("dark", "Dark")]),
            "Glass のときだけ効く")
        # Form.row のツールチップはウィジェットにしか付かないので、
        # 複数コントロールの行はいったん QWidget に包む。
        area_holder = QtWidgets.QWidget()
        area = QtWidgets.QHBoxLayout(area_holder)
        area.setContentsMargins(0, 0, 0, 0)
        area.setSpacing(6)
        self.min_area = QtWidgets.QSpinBox()
        self.min_area.setRange(1, 10_000_000)
        self.max_area = QtWidgets.QSpinBox()
        self.max_area.setRange(1, 10_000_000)
        for box in (self.min_area, self.max_area):
            box.setFixedWidth(96)
            box.setFont(mono_font(10))
        area.addWidget(self.min_area)
        area.addWidget(QtWidgets.QLabel("〜"))
        area.addWidget(self.max_area)
        area.addStretch(1)
        option_form.row("Area (px)", area_holder, "この範囲外の連結成分は捨てる。ノイズが残るなら下限を上げる")
        options.add(option_form)
        self.debug = options.add(ToggleRow(
            "プレビュー画像も書き出す", "<stem>_preview.png に検出枠・重心・ID を描く"))
        self.detection.changed.connect(self._sync_detection)
        root.addWidget(options)

        # --- 実行 -----------------------------------------------------
        run_section = Section("Run")
        run_form = Form(label_width=104)
        self.interpreter = run_form.row(
            "Python", PathField(mode="file", resolver=expand,
                                       placeholder="$WINDOW_TOOL_PYTHON"),
            "cv2 の入った外部 Python。Houdini 側に cv2 があるなら空でよい")
        run_section.add(run_form)

        self.queue = QueueList()
        self.queue.setMinimumHeight(150)
        run_section.add(self.queue)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        self.scan_button = QtWidgets.QPushButton("Scan")
        self.scan_button.setProperty("variant", "ghost")
        self.scan_button.clicked.connect(lambda: self.scan())
        self.run_button = QtWidgets.QPushButton("Extract")
        self.run_button.setProperty("variant", "primary")
        self.run_button.setFixedWidth(120)
        self.run_button.clicked.connect(self.extract)
        buttons.addWidget(self.scan_button)
        buttons.addStretch(1)
        buttons.addWidget(self.run_button)
        run_section.add(buttons)
        root.addWidget(run_section)

        # --- Houdini へ ------------------------------------------------
        network = Section("Build Network")
        network_form = Form(label_width=104)
        self.texture_combo = QtWidgets.QComboBox()
        self.texture_combo.setFont(mono_font(10))
        network_form.row("Texture", self.texture_combo, "ネットワークを作る対象の JSON")

        model_holder = QtWidgets.QWidget()
        model_row = QtWidgets.QHBoxLayout(model_holder)
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(6)
        self.model = QtWidgets.QLineEdit()
        self.model.setFont(mono_font(10))
        self.model.setPlaceholderText("/obj/building/OUT")
        pick = QtWidgets.QPushButton("選択から")
        pick.setProperty("variant", "ghost")
        pick.setFixedWidth(84)
        pick.clicked.connect(self._pick_selected)
        model_row.addWidget(self.model, 1)
        model_row.addWidget(pick)
        network_form.row("Model SOP", model_holder,
                         "UV 展開済みモデルの SOP。空ならポイント生成だけ作る")
        self.build_scope = network_form.row(
            "Scope", Segmented([("selected", "Selected"), ("all", "All")]),
            "All は全テクスチャぶん作る。投影は Selected のときだけ")
        network.add(network_form)

        network_buttons = QtWidgets.QHBoxLayout()
        self.build_button = QtWidgets.QPushButton("Build")
        self.build_button.setProperty("variant", "primary")
        self.build_button.setFixedWidth(120)
        self.build_button.clicked.connect(self.build_network)
        network_buttons.addStretch(1)
        network_buttons.addWidget(self.build_button)
        network.add(network_buttons)
        root.addWidget(network)

        # --- ログ -----------------------------------------------------
        self.log = LogPanel()
        # 既定は warn 以上だが、このツールは進行そのものが info なので all で開く。
        self.log.filter.setValue(0)
        self.log.view.setMinimumHeight(120)
        root.addWidget(self.log, 1)

        footer = QtWidgets.QHBoxLayout()
        self.open_output = QtWidgets.QPushButton("出力フォルダを開く")
        self.open_output.setProperty("variant", "ghost")
        self.open_output.clicked.connect(self._open_output)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        footer.addWidget(self.open_output)
        footer.addStretch(1)
        footer.addWidget(close)
        root.addLayout(footer)

    # --- 設定の出し入れ ------------------------------------------------

    def _restore(self) -> None:
        values = self.values
        self.textures.setText(values["textures"])
        self.out_field.setText(values["out_dir"])
        self.match.setText(values["match"])
        self.recursive.setChecked(values["recursive"])
        self.detection.setValue(values["detection"])
        self.glass_colour.setValue(values["glass_colour"])
        self.min_area.setValue(int(values["min_area"]))
        self.max_area.setValue(int(values["max_area"]))
        self.debug.setChecked(values["debug"])
        self.interpreter.setText(values["interpreter"] or default_interpreter())
        self.model.setText(values["model"])
        self.build_scope.setValue(values["build_scope"])
        self._sync_detection(self.detection.value())

    def collect(self) -> dict:
        return {
            "textures": self.textures.text(),
            "out_dir": self.out_field.text(),
            "match": self.match.text().strip(),
            "recursive": self.recursive.isChecked(),
            "detection": self.detection.value(),
            "glass_colour": self.glass_colour.value(),
            "min_area": self.min_area.value(),
            "max_area": self.max_area.value(),
            "debug": self.debug.isChecked(),
            "interpreter": self.interpreter.text(),
            "model": self.model.text().strip(),
            "build_scope": self.build_scope.value(),
        }

    def closeEvent(self, event) -> None:
        if self.runner is not None:
            self.runner.cancel()
        save_settings(self.collect())
        super().closeEvent(event)

    # --- 小物 ---------------------------------------------------------

    def _sync_detection(self, value=None) -> None:
        self.glass_colour.setEnabled((value or self.detection.value()) == "glass")

    def _autofill_output(self) -> None:
        if not self.out_field.text() and self.textures.text():
            base = Path(self.textures.text().rstrip("/\\"))
            self.out_field.setText(_forward(str(base.parent / "output" / "windows")))

    def _pick_selected(self) -> None:
        if hou is None:
            return
        selected = [n for n in hou.selectedNodes() if isinstance(n, hou.SopNode)]
        if not selected:
            self.log.log("warn", "SOP が選択されていない")
            return
        self.model.setText(selected[0].path())

    def _open_output(self) -> None:
        path = expand(self.out_field.text())
        if not path or not os.path.isdir(path):
            self.log.log("warn", "出力フォルダがまだ無い")
            return
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", path])

    def _resolved_interpreter(self) -> str:
        given = expand(self.interpreter.text())
        if given:
            return given
        return "" if has_local_cv2() else expand(default_interpreter())

    def _update_backend_note(self) -> None:
        interpreter = self._resolved_interpreter()
        if not interpreter and has_local_cv2():
            self.backend_bar.setMessage(
                "この Houdini の Python で直接実行します",
                "cv2 が読み込めました。外部 Python の指定は不要です。", "ok")
        elif interpreter:
            self.backend_bar.setMessage(
                "外部 Python で実行します",
                f"{interpreter} に extract_windows.py を渡します。", "info")
        else:
            self.backend_bar.setMessage(
                "cv2 が見つかりません",
                "Houdini 同梱 Python に cv2 が無く、外部 Python も未設定です。"
                "Run の Python 欄に venv の python.exe を指定するか、"
                "houdini.env に WINDOW_TOOL_PYTHON を書いてください。", "err")

    # --- 走査と抽出 -----------------------------------------------------

    def _validate(self) -> bool:
        if not self.textures.is_valid():
            self.log.log("err", "テクスチャフォルダが見つからない")
            return False
        if not self.out_field.text():
            self.log.log("err", "出力フォルダが未設定")
            return False
        if self.min_area.value() > self.max_area.value():
            self.log.log("err", "Area の下限が上限を超えている")
            return False
        return True

    def scan(self, quiet: bool = False) -> None:
        """抽出せずに、どのファイルが対象になるかだけ出す。"""
        if not self.textures.is_valid():
            self.log.log("err", "テクスチャフォルダが見つからない")
            return
        root = Path(self.textures.resolved())
        suffixes = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".tga", ".webp"}
        paths = root.rglob("*") if self.recursive.isChecked() else root.glob("*")
        needle = self.match.text().strip().casefold()
        files = sorted(p for p in paths
                       if p.is_file() and p.suffix.lower() in suffixes
                       and (not needle or needle in p.name.casefold()))
        self.queue.clear()
        self.results.clear()
        self.texture_combo.clear()
        for path in files:
            self.queue.add(path.name, path.name, "queued")
        if not quiet:
            self.log.log("info" if files else "warn",
                         f"{len(files)} 件が対象" if files else "対象ファイルが無い")

    def extract(self) -> None:
        if not self._validate():
            return
        interpreter = self._resolved_interpreter()
        if not interpreter and not has_local_cv2():
            self.log.log("err", "cv2 が使えない。Python 欄に venv のインタプリタを指定する")
            return
        if interpreter and not os.path.isfile(interpreter):
            self.log.log("err", f"インタプリタが見つからない: {interpreter}")
            return

        self.scan(quiet=True)
        values = self.collect()
        save_settings(values)
        self.out_dir = expand(values["out_dir"])
        self.results.clear()
        self.texture_combo.clear()

        self.run_button.setEnabled(False)
        self.run_button.setText("実行中…")
        self.runner = Runner(values, interpreter, self)
        self.runner.line.connect(self._on_line)
        self.runner.finished.connect(self._on_finished)
        self.runner.start()

    def _on_line(self, line: str) -> None:
        if line.startswith(EVENT_PREFIX):
            try:
                self._on_event(json.loads(line[len(EVENT_PREFIX):]))
            except ValueError:
                pass
            return
        level = "err" if ("ERROR" in line or "FAILED" in line) else "info"
        self.log.log(level, line)

    def _on_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "start":
            self.queue.clear()
            for name in event.get("files", []):
                self.queue.add(name, name, "queued")
        elif kind == "begin":
            self.queue.update(event["name"], status="working", level="info", progress=40)
        elif kind == "item":
            if event.get("status") == "ok":
                count = event.get("windows", 0)
                size = f"{event.get('image_width')}×{event.get('image_height')}"
                self.queue.update(event["name"], status="done", level="ok", progress=100,
                                  note=f"{count} windows  ·  {size}")
                self.results[event["name"]] = event
                self.texture_combo.addItem(event["name"])
                if count == 0:
                    self.log.log("warn", f"{event['name']}: 窓が 1 つも取れていない")
            else:
                self.queue.update(event["name"], status="failed", level="err", progress=100,
                                  note=event.get("message", ""))
        elif kind == "end":
            total = event.get("total", 0)
            ok = event.get("ok", 0)
            level = "ok" if ok == total and total else ("warn" if ok else "err")
            self.log.log(level, f"完了: {ok}/{total} テクスチャ, {event.get('windows', 0)} windows")

    def _on_finished(self, code: int) -> None:
        self.run_button.setEnabled(True)
        self.run_button.setText("Extract")
        self.runner = None
        if code != 0:
            self.log.log("err", f"終了コード {code}")

    # --- ネットワーク生成 -------------------------------------------------

    def build_network(self) -> None:
        if hou is None:
            self.log.log("err", "Houdini の中でだけ使える")
            return
        if not self.results:
            self.log.log("warn", "先に Extract を実行する")
            return

        names = ([self.texture_combo.currentText()]
                 if self.build_scope.value() == "selected"
                 else list(self.results))
        model = self.model.text().strip() if self.build_scope.value() == "selected" else ""
        if self.build_scope.value() == "all" and self.model.text().strip():
            self.log.log("warn", "All のときは投影を作らない。テクスチャごとにモデルが違うため")

        created = []
        for name in names:
            if not name:
                continue
            try:
                node = self._build_one(name, model)
            except Exception as exc:
                self.log.log("err", f"{name}: {exc}")
                continue
            created.append(node)
            self.log.log("ok", f"{name} → {node.path()}")

        if created:
            created[-1].setCurrent(True, clear_all_selected=True)
            hou.node("/obj").layoutChildren()

    def _build_one(self, texture: str, model_path: str):
        stem = Path(texture).stem
        container = hou.node("/obj").createNode("geo", f"windows_{_safe(stem)}")

        points = container.createNode("python", "import_windows")
        _add_path_parms(points)
        points.parm("jsonpath").set(_forward(os.path.join(self.out_dir, stem + "_centroids.json")))
        points.parm("texturefilter").set("")
        points.parm("python").set(IMPORTER.read_text(encoding="utf-8"))

        if not model_path or hou.node(model_path) is None:
            if model_path:
                self.log.log("warn", f"SOP が見つからない: {model_path}。ポイントだけ作る")
            output = container.createNode("null", "OUT")
            output.setInput(0, points)
            output.setDisplayFlag(True)
            output.setRenderFlag(True)
            container.layoutChildren()
            return container

        # 仕様書 6 章のノード構成。input1 と input2 は必ず同じ Vertex Split から分岐させる。
        source = container.createNode("object_merge", "source_model")
        _set_first(source, {"objpath1": model_path, "xformtype": 1}, self.log)

        split = container.createNode("vertexsplit", "split_uv")
        _set_first(split, {"splitattribs": "uv", "attribs": "uv", "splitattrs": "uv"},
                   self.log, quiet=True)
        split.setInput(0, source)

        divide = container.createNode("divide", "make_convex")
        _set_first(divide, {"convex": 1}, self.log)
        _set_first(divide, {"numsides": 4}, self.log)
        divide.setInput(0, split)

        promote = container.createNode("attribpromote", "uv_to_point")
        _set_first(promote, {"inname": "uv"}, self.log)
        _set_first(promote, {"inclass": 0}, self.log)   # vertex
        _set_first(promote, {"outclass": 1}, self.log)  # point
        promote.setInput(0, divide)

        flatten = container.createNode("attribwrangle", "flatten_uv")
        flatten.parm("class").set(2)                    # points
        flatten.parm("snippet").set("@P = set(@uv.x, @uv.y, 0);")
        flatten.setInput(0, promote)

        project = container.createNode("attribwrangle", "project_windows")
        project.parm("class").set(2)
        project.parm("snippet").set(VEX_FILE.read_text(encoding="utf-8"))
        project.setInput(0, points)
        project.setInput(1, flatten)
        project.setInput(2, promote)

        output = container.createNode("null", "OUT")
        output.setInput(0, project)
        output.setDisplayFlag(True)
        output.setRenderFlag(True)
        container.layoutChildren()
        return container


# ------------------------------------------------------------------ 補助

def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


def _forward(path: str) -> str:
    return path.replace("\\", "/")


def _add_path_parms(node) -> None:
    """Python SOP に jsonpath / texturefilter を足す。"""
    group = node.parmTemplateGroup()
    group.append(hou.StringParmTemplate(
        "jsonpath", "JSON", 1,
        string_type=hou.stringParmType.FileReference))
    group.append(hou.StringParmTemplate("texturefilter", "Texture Filter", 1))
    node.setParmTemplateGroup(group)


def _set_first(node, candidates: dict, log, quiet: bool = False) -> bool:
    """パラメータ名がバージョンで揺れるノード向け。最初に見つかった名前に入れる。"""
    for name, value in candidates.items():
        parm = node.parm(name)
        if parm is not None:
            parm.set(value)
            return True
    if not quiet:
        log.log("warn", f"{node.name()}: {'/'.join(candidates)} が見つからない。手で設定する")
    return False


# ------------------------------------------------------------------ 起動

_dialog = None


def show():
    """シェルフから呼ぶ入口。ダイアログは 1 つだけ保持する。"""
    global _dialog
    parent = hou.qt.mainWindow() if hou is not None else None
    if _dialog is None:
        _dialog = WindowTool(parent)
    _dialog.show()
    _dialog.raise_()
    return _dialog


if __name__ == "__main__":
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = WindowTool()
    dialog.show()
    application.exec()
