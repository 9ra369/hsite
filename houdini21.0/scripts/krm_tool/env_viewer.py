# -*- coding: utf-8 -*-
# === Environment Variable Viewer (shelf tool) ===
import os

try:
    from PySide6 import QtWidgets, QtCore, QtGui
    _QT = "PySide6"
except Exception:
    try:
        from PySide2 import QtWidgets, QtCore, QtGui
        _QT = "PySide2"
    except Exception:
        _QT = None

try:
    import hou
except Exception:
    hou = None


def _build_viewer():
    class EnvViewer(QtWidgets.QWidget):
        def __init__(self, parent=None):
            super(EnvViewer, self).__init__(parent)
            self.setWindowTitle("Environment Variables  [%s]" % _QT)
            self.setWindowFlags(QtCore.Qt.Window)
            self.resize(900, 600)
            self._all_rows = []
            self._build_ui()
            self.reload_env()
            self._center_on_screen()

        def _center_on_screen(self):
            try:
                if hou is not None and hasattr(hou, "qt"):
                    ref = hou.qt.mainWindow().frameGeometry()
                else:
                    scr = QtWidgets.QApplication.primaryScreen()
                    ref = scr.availableGeometry() if scr else None
                if ref is not None:
                    fg = self.frameGeometry()
                    fg.moveCenter(ref.center())
                    self.move(fg.topLeft())
            except Exception:
                pass

        def _build_ui(self):
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(6)

            # 上段: 検索ボックス + 更新ボタン
            top = QtWidgets.QHBoxLayout()
            self.search = QtWidgets.QLineEdit()
            self.search.setPlaceholderText("検索（Key / Value をフィルタ。スペース区切りでAND）")
            self.search.textChanged.connect(self._apply_filter)
            top.addWidget(self.search, 1)
            self.reload_btn = QtWidgets.QPushButton("更新")
            self.reload_btn.clicked.connect(self.reload_env)
            top.addWidget(self.reload_btn)
            layout.addLayout(top)

            # テーブル
            self.table = QtWidgets.QTableWidget(0, 2)
            self.table.setHorizontalHeaderLabels(["Key", "Value"])
            self.table.horizontalHeader().setStretchLastSection(True)
            self.table.setColumnWidth(0, 260)
            self.table.setSortingEnabled(True)
            self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
            self.table.setAlternatingRowColors(True)
            self.table.verticalHeader().setVisible(False)
            self.table.cellDoubleClicked.connect(self._copy_value)
            mono = QtGui.QFont("Consolas")
            mono.setStyleHint(QtGui.QFont.Monospace)
            self.table.setFont(mono)
            layout.addWidget(self.table, 1)

            # 下段: 件数表示
            self.status = QtWidgets.QLabel("")
            layout.addWidget(self.status)

        def reload_env(self):
            self._all_rows = sorted(os.environ.items(), key=lambda kv: kv[0].lower())
            self._apply_filter()

        def _apply_filter(self):
            terms = [t for t in self.search.text().lower().split() if t]

            def match(key, val):
                if not terms:
                    return True
                hay = (key + " " + val).lower()
                return all(t in hay for t in terms)   # スペース区切りはAND

            rows = [(k, v) for (k, v) in self._all_rows if match(k, v)]
            self.table.setSortingEnabled(False)
            self.table.setRowCount(len(rows))
            for r, (k, v) in enumerate(rows):
                self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(k))
                self.table.setItem(r, 1, QtWidgets.QTableWidgetItem(v))
            self.table.setSortingEnabled(True)
            self.status.setText("表示 %d 件 / 全 %d 件" % (len(rows), len(self._all_rows)))

        def _copy_value(self, row, col):
            item = self.table.item(row, 1)
            if item is None:
                return
            QtWidgets.QApplication.clipboard().setText(item.text())
            key_item = self.table.item(row, 0)
            key = key_item.text() if key_item else ""
            self.status.setText("コピーしました: %s" % key)

    return EnvViewer


if not _QT:
    msg = "PySide6 も PySide2 も import できませんでした。"
    print(msg)
    if hou is not None:
        try:
            hou.ui.displayMessage(msg)
        except Exception:
            pass
else:
    parent = hou.qt.mainWindow() if (hou is not None and hasattr(hou, "qt")) else None
    prev = getattr(hou.session, "_env_viewer_win", None) if hou is not None else None
    try:
        if prev is not None:
            prev.close()
            prev.deleteLater()
    except Exception:
        pass
    EnvViewer = _build_viewer()
    win = EnvViewer(parent)
    win.show()
    win.raise_()
    win.activateWindow()
    # セッション中、参照を保持して GC を防ぐ
    if hou is not None:
        hou.session._env_viewer_win = win
