"""houdini_memo: メモ入力/表示ダイアログ（PySide6のみに依存、hou非依存）"""
from __future__ import annotations

from PySide6 import QtWidgets


class MemoWriteDialog(QtWidgets.QDialog):
    """Houdini終了時に表示する「次回への申し送りメモ」入力ダイアログ"""

    def __init__(self, hip_name: str, existing_text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("次回への申し送りメモ")
        self.resize(480, 260)

        layout = QtWidgets.QVBoxLayout(self)

        label = QtWidgets.QLabel(
            f"「{hip_name}」を閉じます。\n次にこのファイルを開いたときのためのメモを残せます。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)

        self.text_edit = QtWidgets.QPlainTextEdit()
        self.text_edit.setPlaceholderText(
            "例: マテリアル調整の途中。foo_shadingノードから続き。"
        )
        self.text_edit.setPlainText(existing_text)
        layout.addWidget(self.text_edit)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.Save).setText("保存して終了")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("メモなしで終了")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def memo_text(self) -> str:
        return self.text_edit.toPlainText()


class MemoShowDialog(QtWidgets.QDialog):
    """Houdini起動時（ファイルオープン時）に表示する、前回のメモ表示ダイアログ"""

    def __init__(self, hip_name: str, text: str, saved_at: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("前回の申し送りメモ")
        self.resize(480, 260)

        layout = QtWidgets.QVBoxLayout(self)

        header = f"「{hip_name}」に前回残されたメモです。"
        if saved_at:
            header += f"\n保存日時: {saved_at}"
        label = QtWidgets.QLabel(header)
        label.setWordWrap(True)
        layout.addWidget(label)

        text_view = QtWidgets.QPlainTextEdit()
        text_view.setPlainText(text)
        text_view.setReadOnly(True)
        layout.addWidget(text_view)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("確認した")
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
