# -*- coding: utf-8 -*-
"""
vfl_sync_tool
=============
Houdini上のWrangle系ノードのVEXコードを、VS Code等の外部エディタで管理している
.vflファイルから同期するためのツール（プロトタイプ／モックアップ）。

使い方（Houdini内）:
    import vfl_sync_tool
    vfl_sync_tool.show()

設計方針:
    - hou に依存しないロジック（.vfl探索・JSON状態管理・更新判定）と
      hou に依存する層（選択ノード取得・パラメータへの書き込み）を分離。
      hou非依存部分は Houdini の外でも import・テストできる
      （このファイル単体で `python vfl_sync_tool.py` を実行すると
      コアロジックのスモークテストが走る）。
    - UIキット(dcc_ui.py)は import せず、必要なPySide6パーツをこのファイルに
      直接書く方針に合わせて単一ファイルで完結させている。

UIの流れ:
    - Refresh               : .vflの再探索と更新判定だけを行う（ノードは書き換えない）。
                              新しい.vflがある行は黄色でハイライトされる。
    - Update Selected Nodes : テーブルで選択した行のノードに.vflの内容を書き込む。
                              行を選択していない場合は "Update All Nodes" になり全行が対象。

未対応・今後の検討事項（モックアップのため簡略化している点）:
    - 対象パラメータは既定で "snippet" を想定（Attribute Wrangle等）。
      違う名前の場合はUI上の "Parm name override" で指定できる。
    - ノードの同定は node.path()、.vflとの突き合わせは node.name() の部分一致で行う
      （例: ノード名 INIT → 01_INIT.vfl）。優先順位は find_vfl_file を参照。
    - ファイル監視(watchdog等)はせず、Refresh / Update ボタン押下時に判定する方式。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# hou は Houdini内でのみ import 可能。外部でのロジックテスト用に保護しておく。
try:
    import hou  # type: ignore
except ImportError:
    hou = None  # type: ignore


STATE_FILENAME = ".houdini_vfl_sync.json"
DEFAULT_VEX_PARM_CANDIDATES = ("snippet", "vex", "code")

# 同期状態
SYNC_NOT_SYNCED = "not_synced"   # 一度も同期していない
SYNC_CHANGED = "changed"         # 前回同期後に.vflが更新された（または別の.vflに一致するようになった）
SYNC_UP_TO_DATE = "up_to_date"   # 最新


# ---------------------------------------------------------------------------
# Core logic (hou非依存) — .vfl探索・状態管理・更新判定
# ---------------------------------------------------------------------------

def find_vfl_file(root_dir: Optional[Path], node_name: str) -> Optional[Path]:
    """root_dir 以下を再帰的に探索し、ファイル名(拡張子なし)に node_name を含む .vfl を返す。

    複数ヒットした場合の優先順位:
        0. 完全一致            例: INIT.vfl
        1. '_' 区切りで一致    例: 01_INIT.vfl / INIT_v2.vfl
        2. その他の部分一致    例: REINIT.vfl
    同順位ならファイル名が短いもの → パス順。
    """
    if root_dir is None or not node_name:
        return None
    root_dir = Path(root_dir)
    if not root_dir.is_dir():
        return None

    candidates = [p for p in root_dir.rglob("*.vfl") if node_name in p.stem]
    if not candidates:
        return None

    def _rank(p: Path):
        stem = p.stem
        if stem == node_name:
            score = 0
        elif stem.endswith("_" + node_name) or stem.startswith(node_name + "_"):
            score = 1
        else:
            score = 2
        return (score, len(stem), str(p))

    return min(candidates, key=_rank)


def load_state(state_path: Path) -> Dict[str, Any]:
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state_path: Path, state: Dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(state_path)


def get_sync_status(state: Dict[str, Any], node_path: str, vfl_path: Path) -> str:
    """ノードと.vflの同期状態を返す（SYNC_* のいずれか）。"""
    entry = state.get(node_path)
    if entry is None:
        return SYNC_NOT_SYNCED
    if entry.get("vfl_path") != str(vfl_path):
        return SYNC_CHANGED  # 部分一致で別ファイルに当たるようになった場合も更新扱い
    if entry.get("mtime") != vfl_path.stat().st_mtime:
        return SYNC_CHANGED
    return SYNC_UP_TO_DATE


def needs_update(state: Dict[str, Any], node_path: str, vfl_path: Path) -> bool:
    """未同期、または前回同期後に.vflが変わっていれば True。"""
    return get_sync_status(state, node_path, vfl_path) != SYNC_UP_TO_DATE


def mark_synced(state: Dict[str, Any], node_path: str, vfl_path: Path) -> None:
    state[node_path] = {
        "vfl_path": str(vfl_path),
        "mtime": vfl_path.stat().st_mtime,
        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# Houdini層 (hou依存) — 選択ノード取得・パラメータ書き込み
# ---------------------------------------------------------------------------

def get_selected_nodes() -> List["hou.Node"]:
    if hou is None:
        raise RuntimeError("hou モジュールが利用できません（Houdini内で実行してください）")
    return list(hou.selectedNodes())


def find_vex_parm(node: "hou.Node"):
    """ノードからVEXコードを保持していそうな文字列パラメータを探す。"""
    for name in DEFAULT_VEX_PARM_CANDIDATES:
        parm = node.parm(name)
        if parm is not None:
            return parm
    return None


def apply_vfl_to_node(node: "hou.Node", vfl_path: Path, parm_name: Optional[str] = None) -> None:
    parm = node.parm(parm_name) if parm_name else find_vex_parm(node)
    if parm is None:
        raise RuntimeError(f"'{node.path()}' に書き込み先パラメータが見つかりません")
    parm.set(vfl_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# UI (PySide6) — 単一ファイル完結
# ---------------------------------------------------------------------------

# 行の色分け: kind -> (背景色, 文字色)。None はデフォルトのまま。
ROW_STYLES = {
    "stale":   ("#e8c547", "#1a1a1a"),  # 新しい.vflあり（黄色）
    "updated": ("#3d6b45", "#ffffff"),  # 今回Updateした
    "error":   ("#8f3b3b", "#ffffff"),
    "missing": (None, "#8a8a8a"),       # .vflが無い・フォルダ未設定
    "ok":      (None, None),            # 最新
}


def _load_qt():
    """PySide6を遅延importする。UIを使わないコアロジックのテストに巻き込まないため。"""
    from PySide6 import QtGui, QtWidgets  # noqa: WPS433 (意図した遅延import)
    return QtWidgets, QtGui


class VflSyncDialog:
    """PySide6.QDialog を継承した実体はこの関数内で動的に構築する
    （モジュール読み込み時にPySide6が無くてもコアロジックだけは使えるようにするため）。
    """

    COL_NODE, COL_NODE_PATH, COL_VFL, COL_STATUS = range(4)

    def __new__(cls, parent=None):
        QtWidgets, QtGui = _load_qt()
        C = VflSyncDialog

        class _Dialog(QtWidgets.QDialog):
            def __init__(self_, parent=None):
                super().__init__(parent)
                self_.setWindowTitle("VFL Sync (mockup)")
                self_.resize(860, 460)
                self_._root_dir: Optional[Path] = None
                self_._entries: Dict[str, Dict[str, Any]] = {}
                self_._build_ui()

            # -- UI構築 --
            def _build_ui(self_):
                layout = QtWidgets.QVBoxLayout(self_)

                folder_row = QtWidgets.QHBoxLayout()
                self_.folder_edit = QtWidgets.QLineEdit()
                self_.folder_edit.setPlaceholderText("VS Code で管理しているフォルダー")
                self_.folder_edit.editingFinished.connect(self_._on_refresh)
                browse_btn = QtWidgets.QPushButton("Browse...")
                browse_btn.clicked.connect(self_._on_browse)
                folder_row.addWidget(QtWidgets.QLabel("Folder:"))
                folder_row.addWidget(self_.folder_edit, 1)
                folder_row.addWidget(browse_btn)
                layout.addLayout(folder_row)

                parm_row = QtWidgets.QHBoxLayout()
                self_.parm_name_edit = QtWidgets.QLineEdit()
                self_.parm_name_edit.setPlaceholderText(
                    "Parm name override（省略時は自動検出: snippet / vex / code）"
                )
                parm_row.addWidget(QtWidgets.QLabel("Parm:"))
                parm_row.addWidget(self_.parm_name_edit, 1)
                layout.addLayout(parm_row)

                add_row = QtWidgets.QHBoxLayout()
                add_btn = QtWidgets.QPushButton("Add Selected Node(s)")
                add_btn.setToolTip("Houdiniで選択中のノードをリストに追加します")
                add_btn.clicked.connect(self_._on_add_selected)
                remove_btn = QtWidgets.QPushButton("Remove Selected Row")
                remove_btn.clicked.connect(self_._on_remove_row)
                refresh_btn = QtWidgets.QPushButton("Refresh")
                refresh_btn.setToolTip(
                    ".vflを再探索して更新の有無だけをチェックします（ノードは書き換えません）"
                )
                refresh_btn.clicked.connect(self_._on_refresh)
                add_row.addWidget(add_btn)
                add_row.addWidget(remove_btn)
                add_row.addStretch(1)
                add_row.addWidget(refresh_btn)
                layout.addLayout(add_row)

                self_.table = QtWidgets.QTableWidget(0, 4)
                self_.table.setHorizontalHeaderLabels(["Node", "Node Path", "VFL File", "Status"])
                header = self_.table.horizontalHeader()
                header.setSectionResizeMode(C.COL_NODE_PATH, QtWidgets.QHeaderView.Stretch)
                header.setSectionResizeMode(C.COL_VFL, QtWidgets.QHeaderView.Stretch)
                self_.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
                self_.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
                self_.table.itemSelectionChanged.connect(self_._update_button_label)
                layout.addWidget(self_.table, 1)

                bottom_row = QtWidgets.QHBoxLayout()
                self_.status_label = QtWidgets.QLabel("")
                self_.update_btn = QtWidgets.QPushButton()
                self_.update_btn.setDefault(True)
                self_.update_btn.setMinimumWidth(200)
                self_.update_btn.clicked.connect(self_._on_update)
                bottom_row.addWidget(self_.status_label, 1)
                bottom_row.addWidget(self_.update_btn)
                layout.addLayout(bottom_row)

                self_._update_button_label()

            # -- ヘルパ --
            def _state_path(self_) -> Optional[Path]:
                if self_._root_dir is None:
                    return None
                return self_._root_dir / STATE_FILENAME

            def _sync_root_from_edit(self_):
                text = self_.folder_edit.text().strip()
                self_._root_dir = Path(text) if text else None

            def _selected_rows(self_) -> List[int]:
                return sorted({idx.row() for idx in self_.table.selectedIndexes()})

            def _update_button_label(self_):
                rows = self_._selected_rows()
                if rows:
                    self_.update_btn.setText(f"Update Selected Nodes ({len(rows)})")
                    self_.update_btn.setToolTip("選択した行のノードに .vfl の内容を書き込みます")
                else:
                    self_.update_btn.setText("Update All Nodes")
                    self_.update_btn.setToolTip("リストの全ノードに .vfl の内容を書き込みます")

            def _node_path_at(self_, row) -> Optional[str]:
                item = self_.table.item(row, C.COL_NODE_PATH)
                return item.text() if item else None

            def _set_row_status(self_, row, text, kind="ok"):
                self_.table.setItem(row, C.COL_STATUS, QtWidgets.QTableWidgetItem(text))
                bg, fg = ROW_STYLES.get(kind, (None, None))
                for col in range(self_.table.columnCount()):
                    item = self_.table.item(row, col)
                    if item is None:
                        continue
                    item.setBackground(QtGui.QBrush(QtGui.QColor(bg)) if bg else QtGui.QBrush())
                    item.setForeground(QtGui.QBrush(QtGui.QColor(fg)) if fg else QtGui.QBrush())

            def _set_vfl_cell(self_, row, vfl_path):
                self_.table.setItem(
                    row, C.COL_VFL,
                    QtWidgets.QTableWidgetItem(str(vfl_path) if vfl_path else "(not found)"),
                )

            def _rescan_row(self_, row, state) -> Optional[Path]:
                """1行分の.vfl再探索と状態表示。更新対象として有効な vfl_path を返す。"""
                entry = self_._entries.get(self_._node_path_at(row))
                if entry is None:
                    return None
                try:
                    node_name = entry["node"].name()
                except Exception:  # Houdini側でノードが削除された等
                    self_._set_vfl_cell(row, None)
                    self_._set_row_status(row, "Node deleted", "error")
                    entry["vfl_path"] = None
                    return None

                vfl_path = find_vfl_file(self_._root_dir, node_name)
                entry["vfl_path"] = vfl_path
                self_._set_vfl_cell(row, vfl_path)

                if self_._root_dir is None:
                    self_._set_row_status(row, "Folder not set", "missing")
                    return None
                if vfl_path is None:
                    self_._set_row_status(row, "VFL not found", "missing")
                    return None

                status = get_sync_status(state, self_._node_path_at(row), vfl_path)
                if status == SYNC_NOT_SYNCED:
                    self_._set_row_status(row, "Not synced yet", "stale")
                elif status == SYNC_CHANGED:
                    self_._set_row_status(row, "New VFL available", "stale")
                else:
                    self_._set_row_status(row, "Up to date", "ok")
                return vfl_path

            def _refresh_all(self_) -> Dict[str, Any]:
                self_._sync_root_from_edit()
                state_path = self_._state_path()
                state = load_state(state_path) if state_path else {}
                stale = 0
                for row in range(self_.table.rowCount()):
                    vfl_path = self_._rescan_row(row, state)
                    if vfl_path is not None and needs_update(state, self_._node_path_at(row), vfl_path):
                        stale += 1
                return {"state": state, "stale": stale}

            # -- ハンドラ --
            def _on_browse(self_):
                directory = QtWidgets.QFileDialog.getExistingDirectory(
                    self_, "Select VS Code project folder"
                )
                if not directory:
                    return
                self_.folder_edit.setText(directory)
                self_._on_refresh()

            def _on_refresh(self_):
                result = self_._refresh_all()
                stale = result["stale"]
                if stale:
                    self_.status_label.setText(f"{stale} node(s) have a newer VFL")
                else:
                    self_.status_label.setText(f"All up to date ({time.strftime('%H:%M:%S')})")

            def _on_add_selected(self_):
                try:
                    nodes = get_selected_nodes()
                except RuntimeError as e:
                    QtWidgets.QMessageBox.warning(self_, "VFL Sync", str(e))
                    return
                if not nodes:
                    QtWidgets.QMessageBox.information(
                        self_, "VFL Sync", "Houdini上でノードを選択してください。"
                    )
                    return
                for node in nodes:
                    node_path = node.path()
                    if node_path in self_._entries:
                        continue
                    self_._entries[node_path] = {"node": node, "vfl_path": None}
                    row = self_.table.rowCount()
                    self_.table.insertRow(row)
                    self_.table.setItem(row, C.COL_NODE, QtWidgets.QTableWidgetItem(node.name()))
                    self_.table.setItem(row, C.COL_NODE_PATH, QtWidgets.QTableWidgetItem(node_path))
                self_._on_refresh()

            def _on_remove_row(self_):
                for row in reversed(self_._selected_rows()):
                    node_path = self_._node_path_at(row)
                    if node_path:
                        self_._entries.pop(node_path, None)
                    self_.table.removeRow(row)
                self_._update_button_label()

            def _on_update(self_):
                self_._sync_root_from_edit()
                state_path = self_._state_path()
                if state_path is None:
                    QtWidgets.QMessageBox.warning(self_, "VFL Sync", "先にフォルダーを指定してください。")
                    return

                target_rows = self_._selected_rows() or list(range(self_.table.rowCount()))
                state = self_._refresh_all()["state"]  # 全行の表示を最新化してから更新
                updated = skipped = errors = 0
                parm_override = self_.parm_name_edit.text().strip() or None

                for row in target_rows:
                    node_path = self_._node_path_at(row)
                    entry = self_._entries.get(node_path)
                    if entry is None:
                        continue
                    vfl_path = entry.get("vfl_path")
                    if vfl_path is None:
                        continue  # ステータスは _refresh_all 側で表示済み

                    if not needs_update(state, node_path, vfl_path):
                        skipped += 1
                        continue

                    try:
                        apply_vfl_to_node(entry["node"], vfl_path, parm_name=parm_override)
                        mark_synced(state, node_path, vfl_path)
                        self_._set_row_status(row, f"Updated {time.strftime('%H:%M:%S')}", "updated")
                        updated += 1
                    except Exception as e:  # モックアップにつき広めにcatchして表示
                        self_._set_row_status(row, f"Error: {e}", "error")
                        errors += 1

                save_state(state_path, state)
                self_.status_label.setText(
                    f"Updated: {updated} / Skipped: {skipped} / Errors: {errors}"
                )

        return _Dialog(parent)


_dialog_ref = None  # ガベージコレクト防止用に保持


def show():
    """Houdiniのシェルフツールから呼ぶエントリーポイント。"""
    global _dialog_ref
    parent = hou.qt.mainWindow() if hou is not None else None
    _dialog_ref = VflSyncDialog(parent)
    _dialog_ref.show()
    return _dialog_ref


# ---------------------------------------------------------------------------
# hou非依存ロジックのスモークテスト（Houdini外から `python vfl_sync_tool.py` で実行可）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sub").mkdir()
        vfl = root / "sub" / "process_points.vfl"
        vfl.write_text("// test vex\n@P.y += 1;\n", encoding="utf-8")

        found = find_vfl_file(root, "process_points")
        assert found == vfl, f"見つからない: {found}"
        assert find_vfl_file(root, "no_such_node") is None

        # 部分一致: ノード名 INIT → 01_INIT.vfl
        init_vfl = root / "sub" / "01_INIT.vfl"
        init_vfl.write_text("// init\n", encoding="utf-8")
        (root / "REINIT.vfl").write_text("// reinit\n", encoding="utf-8")
        assert find_vfl_file(root, "INIT") == init_vfl  # '_'区切りを REINIT より優先

        state_path = root / STATE_FILENAME
        state = load_state(state_path)
        assert state == {}

        node_key = "/obj/geo1/process_points"
        assert get_sync_status(state, node_key, vfl) == SYNC_NOT_SYNCED
        assert needs_update(state, node_key, vfl) is True  # 初回は必ずTrue

        mark_synced(state, node_key, vfl)
        save_state(state_path, state)

        reloaded = load_state(state_path)
        assert get_sync_status(reloaded, node_key, vfl) == SYNC_UP_TO_DATE
        assert needs_update(reloaded, node_key, vfl) is False  # 2回目・未変更ならFalse

        time.sleep(0.01)
        vfl.write_text("// changed\n@P.y += 2;\n", encoding="utf-8")
        assert get_sync_status(reloaded, node_key, vfl) == SYNC_CHANGED  # 変更後は黄色対象

        # 完全一致が現れると別ファイル扱い → 更新対象になる
        init_key = "/obj/geo1/INIT"
        mark_synced(reloaded, init_key, init_vfl)
        exact_vfl = root / "INIT.vfl"
        exact_vfl.write_text("// exact\n", encoding="utf-8")
        new_match = find_vfl_file(root, "INIT")
        assert new_match == exact_vfl
        assert get_sync_status(reloaded, init_key, new_match) == SYNC_CHANGED

        print("OK: core logic smoke test passed")