# -*- coding: utf-8 -*-
"""
Poly Haven Asset Downloader - Houdini Shelf Tool
=================================================

A PySide UI tool that browses Poly Haven's (https://polyhaven.com) public
HDRI and Model libraries with a thumbnail grid, and downloads the selected
asset straight into a folder of your choice. Models are automatically
wired up into a ready-to-use geo network.

INSTALL
    1. Right-click a shelf tab -> "New Tool..."
    2. Open the "Script" tab, set the language to Python
    3. Paste the entire contents of this file in
    4. Set a Label / Icon and save
    5. Click the new shelf button to open the dialog

REQUIREMENTS
    - Houdini 19.0+ (Python 3 / hou module)
    - Uses whichever PySide ships with your Houdini build
      (PySide2 on 19.0-20.0, PySide6 on 20.5+)
    - No extra pip installs needed

ABOUT THE NETWORKING APPROACH
    Houdini's bundled QtNetwork (QNetworkAccessManager) often lacks a working
    OpenSSL backend, which causes "TLS initialization failed" errors on
    HTTPS requests. To avoid that entirely, this tool does NOT use QtNetwork.
    It uses Python's own urllib/ssl (bundled with Houdini's Python) for all
    HTTP calls, running them on QThread workers so the UI never blocks.

WHAT HAPPENS ON DOWNLOAD
    - HDRI: the file is downloaded, and (optionally) an Environment Light
      object is created at /obj with its env_map pointed at the file.
    - Model (FBX): the chosen format's main file plus every texture it
      references ("include" files from the API) are downloaded into their
      own folder, preserving relative paths so the model can find its
      textures. Then (optionally) a network is built at /obj/geoN:
          file1      - loads the downloaded model
          transform1 - rotates -90 on X to convert Poly Haven's Z-up
                       export convention to Houdini's Y-up (toggle-able)
          material1  - assigns an auto-built Principled Shader that uses
                       whichever diffuse/normal/rough/metal maps were found
          null1      - output node, display/render flag set
    - Model (USD): same as above but uses usdimport1 instead of file1,
      with input_unpack=on and unpack_geomtype=polygons.
    - Texture: all map files for the chosen format/resolution are downloaded
      into their own folder ({asset_id}_{res}_{fmt}/). Then (optionally) a
      Principled Shader is created at the configured Material path using
      whichever diffuse/normal/rough/metal/disp maps were found.

POLY HAVEN API TERMS (IMPORTANT)
    - Free for non-commercial / academic use. Commercial use may require a
      separate license - see
      https://github.com/Poly-Haven/Public-API/blob/master/ToS.md
    - Every request must send an identifying User-Agent. Change USER_AGENT
      below to include your own tool name / contact email.
    - Poly Haven requires visible credit near anything built with their
      assets (this tool shows a "Powered by Poly Haven" link at the bottom).
"""

import os
import json
import ssl
from collections import deque
import urllib.request
import urllib.error

import hou

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets


API_BASE = "https://api.polyhaven.com"
# Change this to include your own tool name / contact info (required by ToS)
USER_AGENT = "HoudiniPolyHavenDownloader/1.0 (contact: your-email@example.com)"

def _default_dest():
    """$JOB/tex を既定の保存先にする。
    $JOB が未定義（バッチを経由しない起動）の場合はデスクトップにフォールバック。"""
    if hou.getenv("JOB"):
        return hou.expandString("$JOB/tex")
    return os.path.join(os.path.expanduser("~"), "Desktop")


DEFAULT_DEST = _default_dest()
DEFAULT_MAT_PATH = "/mat"

ICON_SIZE = 110
MAX_CONCURRENT_THUMBS = 6

# gltf は除外 (Houdini では直接ロード不可)
MODEL_FORMAT_ORDER = ["fbx", "usd"]
HDRI_FORMAT_ORDER = ["exr", "hdr"]
# Textures: PNG優先 (軽量)、EXRは16bit HDRテクスチャ用
TEXTURE_FORMAT_ORDER = ["png", "exr", "jpg"]

# Textures API の各マップキー名 -> 分類マッピング
# キー名は "{map_type}_{fmt}" 形式 (例: nor_gl_png, rough_exr)
# または diffuse のみフォーマット名そのもの (例: "png", "exr")
TEXTURE_MAP_KEY_RULES = [
    ("basecolor",    ["_diff_", "_diffuse_", "_albedo_", "_col_"]),
    ("normal_gl",    ["_nor_gl_"]),
    ("normal_dx",    ["_nor_dx_"]),
    ("normal",       ["_nor_"]),
    ("roughness",    ["_rough_"]),
    ("metallic",     ["_metal_"]),
    ("ao",           ["_ao_"]),
    ("displacement", ["_disp_", "_height_"]),
    ("arm",          ["_arm_"]),   # AO/Roughness/Metal packed
]


def _res_sort_key(r):
    try:
        return float(r.lower().replace("k", ""))
    except Exception:
        return 0


# ----------------------------------------------------------------------
# HTTP helpers (plain urllib, no QtNetwork -> sidesteps Houdini's SSL issue)
# ----------------------------------------------------------------------
def _build_ssl_context():
    try:
        return ssl.create_default_context()
    except Exception:
        return None


_SSL_CONTEXT = _build_ssl_context()


def _urlopen(url, timeout=30):
    """urlopen wrapper with a fallback to an unverified context, for
    environments where the CA certificate store can't be found."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT)
    except (ssl.SSLCertVerificationError, urllib.error.URLError):
        insecure_ctx = ssl._create_unverified_context()
        return urllib.request.urlopen(req, timeout=timeout, context=insecure_ctx)


def http_get_json(url):
    with _urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_bytes(url, timeout=20):
    with _urlopen(url, timeout=timeout) as resp:
        return resp.read()


# ----------------------------------------------------------------------
# Background worker threads
# ----------------------------------------------------------------------
class AssetListWorker(QtCore.QThread):
    finished_ok = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, asset_type, parent=None):
        super(AssetListWorker, self).__init__(parent)
        self.asset_type = asset_type

    def run(self):
        try:
            data = http_get_json("%s/assets?t=%s" % (API_BASE, self.asset_type))
            self.finished_ok.emit(data)
        except Exception as e:
            self.failed.emit(str(e))


class FilesWorker(QtCore.QThread):
    finished_ok = QtCore.Signal(str, object)
    failed = QtCore.Signal(str, str)

    def __init__(self, asset_id, parent=None):
        super(FilesWorker, self).__init__(parent)
        self.asset_id = asset_id

    def run(self):
        try:
            data = http_get_json("%s/files/%s" % (API_BASE, self.asset_id))
            self.finished_ok.emit(self.asset_id, data)
        except Exception as e:
            self.failed.emit(self.asset_id, str(e))


class ThumbnailWorker(QtCore.QThread):
    finished_ok = QtCore.Signal(str, object)
    failed = QtCore.Signal(str, str)

    def __init__(self, asset_id, url, parent=None):
        super(ThumbnailWorker, self).__init__(parent)
        self.asset_id = asset_id
        self.url = url

    def run(self):
        try:
            data = http_get_bytes(self.url)
            self.finished_ok.emit(self.asset_id, data)
        except Exception as e:
            self.failed.emit(self.asset_id, str(e))


class MultiFileDownloadWorker(QtCore.QThread):
    """Downloads one or more (url, dest_path, size) jobs sequentially.
    Job 0 is treated as the "main" file and its path is returned on success.
    Used for both single-file HDRI downloads and multi-file model downloads
    (main model file + every texture it references)."""

    progress = QtCore.Signal(int, int)
    finished_ok = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def __init__(self, jobs, parent=None):
        super(MultiFileDownloadWorker, self).__init__(parent)
        self.jobs = jobs

    def run(self):
        total = sum(j[2] for j in self.jobs) or 1
        done_bytes = 0
        main_path = self.jobs[0][1]
        current_tmp = None
        try:
            for url, dest_path, _size in self.jobs:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                tmp_path = dest_path + ".part"
                current_tmp = tmp_path
                with _urlopen(url, timeout=60) as resp:
                    chunk_size = 1024 * 256
                    with open(tmp_path, "wb") as f:
                        while True:
                            chunk = resp.read(chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            done_bytes += len(chunk)
                            self.progress.emit(done_bytes, total)
                os.replace(tmp_path, dest_path)
                current_tmp = None
            self.finished_ok.emit(main_path)
        except Exception as e:
            if current_tmp and os.path.exists(current_tmp):
                try:
                    os.remove(current_tmp)
                except Exception:
                    pass
            self.failed.emit(str(e))


# ----------------------------------------------------------------------
# Main dialog
# ----------------------------------------------------------------------
class PolyHavenDownloader(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super(PolyHavenDownloader, self).__init__(parent)
        self.setWindowTitle("Poly Haven Downloader")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Window)
        self.resize(880, 700)

        self.asset_type = "hdris"     # "hdris" or "models"
        self.assets = {}              # asset_id -> metadata
        self.files_cache = {}         # asset_id -> /files/{id} response
        self.current_id = None
        self.current_fmt_map = {}     # {format: {resolution: file_entry}}
        self._all_ids = []

        self.thumb_pixmap_cache = {}  # asset_id -> QPixmap (full thumbnail)
        self.thumb_pending = set()    # asset_id currently downloading
        self.thumb_queue = deque()    # asset_ids waiting to be downloaded
        self._thumb_workers = []      # keep references alive

        self._asset_worker = None
        self._files_worker = None
        self._download_worker = None
        self._pending_kind = None
        self._pending_tex_paths = {}
        self._pending_fmt = None      # ダウンロード時のフォーマット ("fbx"/"usd")

        self._placeholder_icon = self._make_placeholder_icon()

        self._build_ui()
        self._fetch_asset_list()

    @staticmethod
    def _make_placeholder_icon():
        pix = QtGui.QPixmap(ICON_SIZE, ICON_SIZE)
        pix.fill(QtGui.QColor(45, 45, 45))
        return QtGui.QIcon(pix)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)

        # Asset type + search + sort
        top_layout = QtWidgets.QHBoxLayout()
        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.addItems(["HDRIs", "Models", "Textures"])
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        top_layout.addWidget(self.type_combo)

        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Search by name, category or tag...")
        self.search_edit.textChanged.connect(self._apply_filter)
        top_layout.addWidget(self.search_edit, 1)

        self.sort_combo = QtWidgets.QComboBox()
        self.sort_combo.addItems(["Name", "Most Downloaded", "Newest"])
        self.sort_combo.currentIndexChanged.connect(self._refresh_list)
        top_layout.addWidget(self.sort_combo)
        main_layout.addLayout(top_layout)

        # Thumbnail grid + side info panel
        splitter = QtWidgets.QSplitter()

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setViewMode(QtWidgets.QListView.IconMode)
        self.list_widget.setIconSize(QtCore.QSize(ICON_SIZE, ICON_SIZE))
        self.list_widget.setGridSize(QtCore.QSize(ICON_SIZE + 20, ICON_SIZE + 40))
        self.list_widget.setResizeMode(QtWidgets.QListView.Adjust)
        self.list_widget.setMovement(QtWidgets.QListView.Static)
        self.list_widget.setSpacing(6)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setWordWrap(True)
        self.list_widget.currentItemChanged.connect(self._on_selection_changed)
        splitter.addWidget(self.list_widget)

        side_widget = QtWidgets.QWidget()
        side_layout = QtWidgets.QVBoxLayout(side_widget)
        self.preview_label = QtWidgets.QLabel("Select an asset")
        self.preview_label.setFixedSize(180, 180)
        self.preview_label.setAlignment(QtCore.Qt.AlignCenter)
        self.preview_label.setStyleSheet("background:#222; color:#888; border:1px solid #444;")
        self.info_label = QtWidgets.QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setTextFormat(QtCore.Qt.RichText)
        side_layout.addWidget(self.preview_label)
        side_layout.addWidget(self.info_label)
        side_layout.addStretch()
        splitter.addWidget(side_widget)
        splitter.setSizes([600, 280])
        main_layout.addWidget(splitter, 1)

        # Format / resolution
        opts_layout = QtWidgets.QHBoxLayout()
        opts_layout.addWidget(QtWidgets.QLabel("Format:"))
        self.fmt_combo = QtWidgets.QComboBox()
        self.fmt_combo.currentIndexChanged.connect(self._update_resolutions)
        opts_layout.addWidget(self.fmt_combo)
        opts_layout.addWidget(QtWidgets.QLabel("Resolution:"))
        self.res_combo = QtWidgets.QComboBox()
        opts_layout.addWidget(self.res_combo)
        opts_layout.addStretch()
        main_layout.addLayout(opts_layout)

        # Destination
        dest_layout = QtWidgets.QHBoxLayout()
        dest_layout.addWidget(QtWidgets.QLabel("Destination:"))
        self.dest_edit = QtWidgets.QLineEdit(DEFAULT_DEST)
        dest_layout.addWidget(self.dest_edit, 1)
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_dest)
        dest_layout.addWidget(browse_btn)
        main_layout.addLayout(dest_layout)

        # Material output path (models only)
        self.mat_path_widget = QtWidgets.QWidget()
        mat_path_layout = QtWidgets.QHBoxLayout(self.mat_path_widget)
        mat_path_layout.setContentsMargins(0, 0, 0, 0)
        mat_path_layout.addWidget(QtWidgets.QLabel("Material path:"))
        self.mat_path_edit = QtWidgets.QLineEdit(DEFAULT_MAT_PATH)
        self.mat_path_edit.setToolTip(
            "Houdini scene path where the Principled Shader will be created.\n"
            "Examples: /mat  /obj/myGeo/shopnet1"
        )
        mat_path_layout.addWidget(self.mat_path_edit, 1)
        mat_browse_btn = QtWidgets.QPushButton("Browse...")
        mat_browse_btn.clicked.connect(self._browse_mat_path)
        mat_path_layout.addWidget(mat_browse_btn)
        main_layout.addWidget(self.mat_path_widget)

        # Options
        self.fix_axis_check = QtWidgets.QCheckBox(
            "Fix up-axis on import (rotate -90 on X: Z-up -> Y-up)"
        )
        self.fix_axis_check.setChecked(True)
        main_layout.addWidget(self.fix_axis_check)

        self.create_node_check = QtWidgets.QCheckBox()
        self.create_node_check.setChecked(True)
        main_layout.addWidget(self.create_node_check)
        self._update_create_node_label()

        # Progress + download button
        bottom_layout = QtWidgets.QHBoxLayout()
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setValue(0)
        bottom_layout.addWidget(self.progress_bar, 1)
        self.download_btn = QtWidgets.QPushButton("Download")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self._start_download)
        bottom_layout.addWidget(self.download_btn)
        main_layout.addLayout(bottom_layout)

        # Credit (required by Poly Haven ToS)
        credit = QtWidgets.QLabel(
            'Assets by <a href="https://polyhaven.com">Poly Haven</a> (CC0) '
            '&middot; <a href="https://github.com/Poly-Haven/Public-API">Public API</a>'
        )
        credit.setOpenExternalLinks(True)
        main_layout.addWidget(credit)

        self.status_label = QtWidgets.QLabel("Loading...")
        main_layout.addWidget(self.status_label)

        self._update_type_specific_visibility()

    def _update_create_node_label(self):
        if self.asset_type == "hdris":
            self.create_node_check.setText(
                "Create and assign an Environment Light node after download"
            )
        elif self.asset_type == "textures":
            self.create_node_check.setText(
                "Build Principled Shader at Material path after download"
            )
        else:
            self.create_node_check.setText(
                "Build geo1/loader/transform1/material1/null1 network after download"
            )

    def _update_type_specific_visibility(self):
        is_model = self.asset_type == "models"
        is_texture = self.asset_type == "textures"
        self.fix_axis_check.setVisible(is_model)
        self.mat_path_widget.setVisible(is_model or is_texture)

    # ------------------------------------------------------------------
    # Material path browsing
    # ------------------------------------------------------------------
    def _browse_mat_path(self):
        """ユーザーに手入力してもらうか、既存のmatnetノードをリストして選択させる。"""
        # Houdini内のmatnet候補を収集
        candidates = [DEFAULT_MAT_PATH]
        try:
            for node in hou.node("/").allSubChildren():
                if node.type().name() in ("matnet", "shopnet", "materiallibrary"):
                    p = node.path()
                    if p not in candidates:
                        candidates.append(p)
        except Exception:
            pass

        path, ok = QtWidgets.QInputDialog.getItem(
            self,
            "Select Material Path",
            "Houdini path for the Principled Shader:\n"
            "(existing matnet nodes are listed; you can also type any path)",
            candidates,
            editable=True,
        )
        if ok and path.strip():
            self.mat_path_edit.setText(path.strip())

    # ------------------------------------------------------------------
    # Asset type switching
    # ------------------------------------------------------------------
    def _on_type_changed(self, index):
        mapping = {0: "hdris", 1: "models", 2: "textures"}
        self.asset_type = mapping.get(index, "hdris")
        self.assets = {}
        self.files_cache = {}
        self.current_id = None
        self.current_fmt_map = {}
        self._all_ids = []
        self.thumb_queue.clear()

        self.list_widget.clear()
        self.fmt_combo.clear()
        self.res_combo.clear()
        self.download_btn.setEnabled(False)
        self.preview_label.setText("Select an asset")
        self.preview_label.setPixmap(QtGui.QPixmap())
        self.info_label.setText("")

        self._update_create_node_label()
        self._update_type_specific_visibility()
        self.status_label.setText("Loading...")
        self._fetch_asset_list()

    # ------------------------------------------------------------------
    # Asset list
    # ------------------------------------------------------------------
    def _fetch_asset_list(self):
        self._asset_worker = AssetListWorker(self.asset_type, self)
        self._asset_worker.finished_ok.connect(self._on_asset_list_ok)
        self._asset_worker.failed.connect(self._on_asset_list_failed)
        self._asset_worker.start()

    def _on_asset_list_ok(self, data):
        self.assets = data
        self.status_label.setText("%d assets loaded" % len(self.assets))
        self._refresh_list()

    def _on_asset_list_failed(self, msg):
        self.status_label.setText("Failed to fetch asset list: %s" % msg)

    def _sorted_ids(self):
        ids = list(self.assets.keys())
        mode = self.sort_combo.currentIndex()
        if mode == 1:
            ids.sort(key=lambda i: self.assets[i].get("download_count", 0), reverse=True)
        elif mode == 2:
            ids.sort(key=lambda i: self.assets[i].get("date_published", 0), reverse=True)
        else:
            ids.sort(key=lambda i: self.assets[i].get("name", i).lower())
        return ids

    def _refresh_list(self):
        if not self.assets:
            return
        self._all_ids = self._sorted_ids()
        self._apply_filter()

    def _apply_filter(self):
        text = self.search_edit.text().strip().lower()
        self.list_widget.clear()
        self.thumb_queue.clear()

        for aid in self._all_ids:
            meta = self.assets[aid]
            haystack = " ".join(
                [meta.get("name", ""), aid]
                + meta.get("categories", [])
                + meta.get("tags", [])
            ).lower()
            if text and text not in haystack:
                continue

            item = QtWidgets.QListWidgetItem(meta.get("name", aid))
            item.setData(QtCore.Qt.UserRole, aid)
            item.setTextAlignment(QtCore.Qt.AlignHCenter)

            if aid in self.thumb_pixmap_cache:
                item.setIcon(self._icon_for(self.thumb_pixmap_cache[aid]))
            else:
                item.setIcon(self._placeholder_icon)
                if aid not in self.thumb_pending:
                    self.thumb_queue.append(aid)

            self.list_widget.addItem(item)

        self._process_thumb_queue()

    @staticmethod
    def _icon_for(pixmap):
        scaled = pixmap.scaled(
            ICON_SIZE, ICON_SIZE, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
        )
        return QtGui.QIcon(scaled)

    # ------------------------------------------------------------------
    # Thumbnail loading (grid + preview share the same cache/workers)
    # ------------------------------------------------------------------
    def _process_thumb_queue(self):
        while self.thumb_queue and len(self.thumb_pending) < MAX_CONCURRENT_THUMBS:
            aid = self.thumb_queue.popleft()
            if aid in self.thumb_pixmap_cache or aid in self.thumb_pending:
                continue
            url = self.assets.get(aid, {}).get("thumbnail_url")
            if url:
                self._start_thumb_worker(aid, url)

    def _start_thumb_worker(self, aid, url):
        self.thumb_pending.add(aid)
        worker = ThumbnailWorker(aid, url, self)
        worker.finished_ok.connect(self._on_thumb_ok)
        worker.failed.connect(self._on_thumb_failed)
        self._thumb_workers.append(worker)
        worker.start()

    def _ensure_thumb_loaded(self, aid, url):
        if aid in self.thumb_pixmap_cache or aid in self.thumb_pending or not url:
            return
        self._start_thumb_worker(aid, url)

    def _on_thumb_ok(self, aid, data):
        self.thumb_pending.discard(aid)
        self._thumb_workers = [w for w in self._thumb_workers if w.asset_id != aid]

        pix = QtGui.QPixmap()
        pix.loadFromData(data)
        if not pix.isNull():
            self.thumb_pixmap_cache[aid] = pix
            self._apply_icon_to_item(aid, pix)
            if aid == self.current_id:
                self._set_preview_pixmap(pix)

        self._process_thumb_queue()

    def _on_thumb_failed(self, aid, msg):
        self.thumb_pending.discard(aid)
        self._thumb_workers = [w for w in self._thumb_workers if w.asset_id != aid]
        if aid == self.current_id:
            self.preview_label.setText("Failed to load image")
        self._process_thumb_queue()

    def _apply_icon_to_item(self, aid, pix):
        icon = self._icon_for(pix)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(QtCore.Qt.UserRole) == aid:
                item.setIcon(icon)

    def _set_preview_pixmap(self, pix):
        scaled = pix.scaled(
            180, 180, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
        )
        self.preview_label.setPixmap(scaled)

    # ------------------------------------------------------------------
    # Selection handling
    # ------------------------------------------------------------------
    def _on_selection_changed(self, current, _previous):
        if current is None:
            return
        aid = current.data(QtCore.Qt.UserRole)
        self.current_id = aid
        meta = self.assets.get(aid, {})
        max_res = meta.get("max_resolution", ["?", "?"])
        info = (
            "<b>%s</b><br>ID: %s<br>Categories: %s<br>"
            "Downloads: %s<br>Max resolution: %sx%s"
            % (
                meta.get("name", aid), aid,
                ", ".join(meta.get("categories", [])),
                meta.get("download_count", "?"),
                max_res[0], max_res[1],
            )
        )
        self.info_label.setText(info)
        self.download_btn.setEnabled(False)
        self.fmt_combo.blockSignals(True)
        self.fmt_combo.clear()
        self.fmt_combo.blockSignals(False)
        self.res_combo.clear()

        if aid in self.thumb_pixmap_cache:
            self._set_preview_pixmap(self.thumb_pixmap_cache[aid])
        else:
            self.preview_label.setText("Loading...")
            self.preview_label.setPixmap(QtGui.QPixmap())
            self._ensure_thumb_loaded(aid, meta.get("thumbnail_url"))

        if aid in self.files_cache:
            self._populate_options(self.files_cache[aid])
        else:
            self._files_worker = FilesWorker(aid, self)
            self._files_worker.finished_ok.connect(self._on_files_ok)
            self._files_worker.failed.connect(self._on_files_failed)
            self._files_worker.start()

    def _on_files_ok(self, aid, files):
        self.files_cache[aid] = files
        if self.current_id == aid:
            self._populate_options(files)

    def _on_files_failed(self, aid, msg):
        if self.current_id == aid:
            self.status_label.setText("Failed to fetch file info: %s" % msg)

    # ------------------------------------------------------------------
    # Format/resolution parsing
    #
    # HDRI:    files["hdri"][resolution][format]     = {url, size}
    # Model:   files[format][resolution][format]     = {url, size}
    # Texture: 2パターンが存在する:
    #   パターンA: files[channel][resolution][format] = {url, size}
    #     channel = "Diffuse", "nor_gl", "Roughness", etc.
    #   パターンB: files[format][resolution][map_key] = {url, size}
    #     map_key = "png"(diffuse) / "nor_gl_png" / "rough_png" など
    #
    # self.current_fmt_map は常に {format: {resolution: maps_entry}} に正規化。
    # Textures の maps_entry = {channel_or_mapkey: {url, size}} の辞書。
    # ------------------------------------------------------------------

    _TEXTURE_CHANNELS = {
        "Diffuse", "nor_gl", "nor_dx", "Roughness", "Metalness",
        "Displacement", "AO", "Emission", "Opacity", "Specular",
        "Translucency", "arm",
        "diffuse", "roughness", "metalness", "displacement", "ao",
        "emission", "opacity", "specular",
    }

    def _detect_texture_structure(self, files):
        """Textures API レスポンス構造を自動判別。戻り値: 'A', 'B', 'unknown'"""
        for key, val in files.items():
            if not isinstance(val, dict):
                continue
            # パターンB: トップキーがフォーマット名
            if key in ("png", "exr", "jpg", "jpeg"):
                return "B"
            # パターンA: トップキーがチャンネル名 (大文字始まりまたは既知チャンネル)
            if key in self._TEXTURE_CHANNELS or (key and key[0].isupper()):
                for inner_val in val.values():
                    if isinstance(inner_val, dict):
                        for k2 in inner_val.keys():
                            if k2 in ("png", "exr", "jpg", "jpeg"):
                                return "A"
        return "unknown"

    def _build_format_map(self, files):
        fmap = {}
        if self.asset_type == "hdris":
            for res, fmts in files.get("hdri", {}).items():
                if not isinstance(fmts, dict):
                    continue
                for fmt, entry in fmts.items():
                    fmap.setdefault(fmt, {})[res] = entry

        elif self.asset_type == "textures":
            structure = self._detect_texture_structure(files)

            if structure == "A":
                # files[channel][resolution][format] = {url, size}
                # → fmap[format][resolution] = {channel: {url, size}, ...}
                for channel, resmap in files.items():
                    if not isinstance(resmap, dict):
                        continue
                    for res, fmts in resmap.items():
                        if not isinstance(fmts, dict):
                            continue
                        for fmt, file_entry in fmts.items():
                            if fmt not in ("png", "exr", "jpg", "jpeg"):
                                continue
                            if not isinstance(file_entry, dict):
                                continue
                            fmap.setdefault(fmt, {}).setdefault(res, {})[channel] = file_entry

            elif structure == "B":
                # files[format][resolution][map_key] = {url, size}
                for fmt in TEXTURE_FORMAT_ORDER:
                    resmap = files.get(fmt)
                    if not isinstance(resmap, dict):
                        continue
                    for res, maps_dict in resmap.items():
                        if not isinstance(maps_dict, dict):
                            continue
                        fmap.setdefault(fmt, {})[res] = maps_dict

            else:
                # フォールバック: 総当たり (パターンBを先に、次にパターンAを試みる)
                for fmt in TEXTURE_FORMAT_ORDER:
                    resmap = files.get(fmt)
                    if isinstance(resmap, dict):
                        for res, maps_dict in resmap.items():
                            if isinstance(maps_dict, dict):
                                fmap.setdefault(fmt, {})[res] = maps_dict
                if not fmap:
                    for channel, resmap in files.items():
                        if not isinstance(resmap, dict):
                            continue
                        for res, fmts in resmap.items():
                            if not isinstance(fmts, dict):
                                continue
                            for fmt, file_entry in fmts.items():
                                if not isinstance(file_entry, dict):
                                    continue
                                fmap.setdefault(fmt, {}).setdefault(res, {})[channel] = file_entry

            if not fmap:
                top_keys = list(files.keys())[:6]
                self.status_label.setText(
                    "Texture structure unrecognized. Top keys: %s" % top_keys
                )

        else:
            for fmt in MODEL_FORMAT_ORDER:
                resmap = files.get(fmt)
                if not isinstance(resmap, dict):
                    continue
                for res, inner in resmap.items():
                    entry = inner.get(fmt) if isinstance(inner, dict) else None
                    if entry:
                        fmap.setdefault(fmt, {})[res] = entry
        return fmap

    def _populate_options(self, files):
        self.current_fmt_map = self._build_format_map(files)
        formats = list(self.current_fmt_map.keys())
        if self.asset_type == "hdris":
            preferred = HDRI_FORMAT_ORDER
        elif self.asset_type == "textures":
            preferred = TEXTURE_FORMAT_ORDER
        else:
            preferred = MODEL_FORMAT_ORDER
        formats.sort(key=lambda f: preferred.index(f) if f in preferred else len(preferred))

        self.fmt_combo.blockSignals(True)
        self.fmt_combo.clear()
        self.fmt_combo.addItems(formats)
        self.fmt_combo.blockSignals(False)

        self._update_resolutions()
        self.download_btn.setEnabled(bool(formats))

    def _update_resolutions(self, *_args):
        fmt = self.fmt_combo.currentText()
        resolutions = list(self.current_fmt_map.get(fmt, {}).keys())
        resolutions.sort(key=_res_sort_key)

        self.res_combo.blockSignals(True)
        self.res_combo.clear()
        self.res_combo.addItems(resolutions)
        if "4k" in resolutions:
            self.res_combo.setCurrentIndex(resolutions.index("4k"))
        elif resolutions:
            self.res_combo.setCurrentIndex(max(0, len(resolutions) - 2))
        self.res_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    def _browse_dest(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose destination folder", self.dest_edit.text()
        )
        if d:
            self.dest_edit.setText(d)

    def _start_download(self):
        if not self.current_id:
            return
        fmt = self.fmt_combo.currentText()
        res = self.res_combo.currentText()
        entry = self.current_fmt_map.get(fmt, {}).get(res)
        if not entry:
            QtWidgets.QMessageBox.warning(
                self, "Error", "No file found for the selected format/resolution"
            )
            return

        dest_dir = hou.expandString(self.dest_edit.text())
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", "Could not create destination folder: %s" % e)
            return

        if self.asset_type == "hdris":
            filename = "%s_%s.%s" % (self.current_id, res, fmt)
            main_path = os.path.join(dest_dir, filename)
            jobs = [(entry["url"], main_path, entry.get("size", 0))]
            self._pending_kind = "hdri"
            self._pending_tex_paths = {}
            self._pending_fmt = None
        elif self.asset_type == "textures":
            # entry は {map_key: {url, size}, ...} の辞書
            tex_dir = os.path.join(dest_dir, "%s_%s_%s" % (self.current_id, res, fmt))
            tex_paths = {}   # map_key -> dest_path
            jobs = []
            for map_key, map_entry in entry.items():
                if not isinstance(map_entry, dict) or "url" not in map_entry:
                    continue
                filename = os.path.basename(map_entry["url"])
                dest_path = os.path.join(tex_dir, filename)
                jobs.append((map_entry["url"], dest_path, map_entry.get("size", 0)))
                tex_paths[map_key] = dest_path
            if not jobs:
                QtWidgets.QMessageBox.warning(self, "Error", "No downloadable maps found.")
                return
            self._pending_kind = "texture"
            self._pending_tex_paths = tex_paths
            self._pending_fmt = fmt
        else:
            model_dir = os.path.join(dest_dir, "%s_%s_%s" % (self.current_id, res, fmt))
            main_filename = os.path.basename(entry["url"])
            main_path = os.path.join(model_dir, main_filename)
            jobs = [(entry["url"], main_path, entry.get("size", 0))]
            tex_paths = {}
            for rel, inc in entry.get("include", {}).items():
                tex_dest = os.path.join(model_dir, rel)
                jobs.append((inc["url"], tex_dest, inc.get("size", 0)))
                tex_paths[rel] = tex_dest
            self._pending_kind = "model"
            self._pending_tex_paths = tex_paths
            self._pending_fmt = fmt

        self.download_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(sum(j[2] for j in jobs) or 1)
        self.status_label.setText("Downloading %s..." % self.current_id)

        self._download_worker = MultiFileDownloadWorker(jobs, self)
        self._download_worker.progress.connect(self._on_download_progress)
        self._download_worker.finished_ok.connect(self._on_download_finished_ok)
        self._download_worker.failed.connect(self._on_download_failed)
        self._download_worker.start()

    def _on_download_progress(self, downloaded, total):
        if total > 0:
            self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(downloaded)

    def _on_download_finished_ok(self, main_path):
        self.status_label.setText("Download complete: %s" % main_path)
        self.download_btn.setEnabled(True)

        if self.create_node_check.isChecked():
            if self._pending_kind == "hdri":
                self._create_env_light(main_path)
            elif self._pending_kind == "texture":
                self._create_texture_material(self.current_id, self._pending_tex_paths)
            else:
                self._create_model_network(
                    main_path,
                    self._pending_tex_paths,
                    self._pending_fmt or "fbx",
                )

        QtWidgets.QMessageBox.information(
            self, "Done", "Download complete:\n%s" % main_path
        )

    def _on_download_failed(self, msg):
        self.status_label.setText("Download failed: %s" % msg)
        QtWidgets.QMessageBox.critical(self, "Download Failed", msg)
        self.download_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # HDRI -> Environment Light
    # ------------------------------------------------------------------
    def _create_env_light(self, hdri_path):
        try:
            obj = hou.node("/obj")
            name = "envlight_%s" % self.current_id
            node = obj.node(name) or obj.createNode("envlight", name)
            node.parm("env_map").set(hdri_path)
            node.moveToGoodPosition()
        except Exception as e:
            hou.ui.displayMessage(
                "Could not automatically create the Environment Light node.\n"
                "Please assign the HDRI manually.\n(%s)" % e,
                severity=hou.severityType.Warning,
            )

    # ------------------------------------------------------------------
    # Material creation helpers
    # ------------------------------------------------------------------
    def _classify_textures(self, tex_paths):
        """tex_paths の各エントリをテクスチャ種別に分類して返す。

        tex_paths は以下いずれかの形式に対応:
          - Textures API パターンA: {channel_name: abs_path}
              channel_name = "Diffuse" / "nor_gl" / "Roughness" など
          - Textures API パターンB: {map_key: abs_path}
              map_key = "png" (diffuse) / "nor_gl_png" / "rough_png" など
          - Model API:   {relative_path: abs_path}
              ファイル名のサフィックスでマッチング
        戻り値: {"basecolor": path, "normal": path, ...}
        """
        found = {}

        # パターンA: チャンネル名が直接キーになっている場合の直接マッピング
        CHANNEL_TO_MAP = {
            "diffuse": "basecolor",
            "albedo": "basecolor",
            "color": "basecolor",
            "col": "basecolor",
            "nor_gl": "normal",
            "nor_dx": "normal",       # nor_gl がなければ採用
            "normal": "normal",
            "roughness": "roughness",
            "rough": "roughness",
            "metalness": "metallic",
            "metallic": "metallic",
            "metal": "metallic",
            "displacement": "displacement",
            "disp": "displacement",
            "height": "displacement",
            "ao": "ao",
            "arm": "arm",
            "emission": "emission",
        }

        # まずパターンAの直接チャンネル名で分類
        for key, path in tex_paths.items():
            lower = key.lower()
            map_type = CHANNEL_TO_MAP.get(lower)
            if map_type and map_type not in found:
                # nor_dx は nor_gl より低優先
                if lower == "nor_dx" and "normal" in found:
                    continue
                found[map_type] = path

        # 次にパターンBとModelのサフィックスマッチングで補完
        for key, path in tex_paths.items():
            lower = key.lower()

            # diffuse: キー名がフォーマット名のみ ("png", "exr", "jpg")
            if lower in ("png", "exr", "jpg", "jpeg") and "basecolor" not in found:
                found["basecolor"] = path
                continue
            # nor_gl_* 優先
            if ("_nor_gl_" in lower or lower.startswith("nor_gl_")) and "normal" not in found:
                found["normal"] = path
                continue
            # nor_dx_* は nor_gl がなければ
            if ("_nor_dx_" in lower or lower.startswith("nor_dx_")) and "normal" not in found:
                found["normal"] = path
                continue

            for map_type, patterns in TEXTURE_MAP_KEY_RULES:
                if map_type in found:
                    continue
                if any(p in lower for p in patterns):
                    found[map_type] = path
                    break

        return found

    def _apply_maps_to_shader(self, shader, maps):
        """分類済みマップを Principled Shader のパラメータにセットする。"""
        def set_if_exists(parm_name, value):
            p = shader.parm(parm_name)
            if p is not None:
                p.set(value)

        if "basecolor" in maps:
            set_if_exists("basecolor_useTexture", 1)
            set_if_exists("basecolor_texture", maps["basecolor"])
        if "roughness" in maps:
            set_if_exists("rough_useTexture", 1)
            set_if_exists("rough_texture", maps["roughness"])
        if "metallic" in maps:
            set_if_exists("metallic_useTexture", 1)
            set_if_exists("metallic_texture", maps["metallic"])
        if "normal" in maps:
            set_if_exists("baseBumpAndNormal_enable", 1)
            set_if_exists("baseNormal_useTexture", 1)
            set_if_exists("baseNormal_texture", maps["normal"])
        if "displacement" in maps:
            set_if_exists("dispTex_enable", 1)
            set_if_exists("dispTex_texture", maps["displacement"])

    def _create_texture_material(self, asset_id, tex_paths):
        """Textures アセット用: Principled Shader のみ作成し、マップを割り当てる。"""
        maps = self._classify_textures(tex_paths)
        if not maps:
            hou.ui.displayMessage(
                "Downloaded textures, but could not classify any maps.\n"
                "Please assign textures manually.",
                severity=hou.severityType.Warning,
            )
            return
        try:
            mat_lib = self._get_or_create_matnet()
            shader = mat_lib.createNode("principledshader::2.0", "polyhaven_%s" % asset_id)
            self._apply_maps_to_shader(shader, maps)
            shader.moveToGoodPosition()
        except Exception as e:
            hou.ui.displayMessage(
                "Downloaded textures, but could not create the shader.\n(%s)" % e,
                severity=hou.severityType.Warning,
            )

    def _get_or_create_matnet(self):
        """mat_path_edit に入力されたパスに matnet を確保して返す。
        存在する場合はそのまま使い、存在しない場合は作成する。"""
        raw = self.mat_path_edit.text().strip() or DEFAULT_MAT_PATH
        node = hou.node(raw)
        if node is not None:
            return node
        # 親パスを辿って作成
        parent_path, name = raw.rsplit("/", 1) if "/" in raw else ("/", raw)
        parent_path = parent_path or "/"
        parent = hou.node(parent_path)
        if parent is None:
            # フォールバック: /mat を使う
            node = hou.node("/mat")
            if node is None:
                node = hou.node("/").createNode("matnet", "mat")
            return node
        return parent.createNode("matnet", name)

    def _build_material(self, asset_id, tex_paths):
        """Model アセット用: Principled Shader を作成してマップをセット。シェーダノードを返す。"""
        maps = self._classify_textures(tex_paths)
        if not maps:
            return None
        try:
            mat_lib = self._get_or_create_matnet()
            shader = mat_lib.createNode("principledshader::2.0", "polyhaven_%s" % asset_id)
            self._apply_maps_to_shader(shader, maps)
            shader.moveToGoodPosition()
            return shader
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Model -> geo network
    #   FBX:  /obj/geo1/file1 -> transform1 -> material1 -> null1
    #   USD:  /obj/geo1/usdimport1 (input_unpack=on, unpack_geomtype=polygons)
    #         -> transform1 -> material1 -> null1
    # ------------------------------------------------------------------
    def _create_model_network(self, main_path, tex_paths, fmt):
        try:
            obj = hou.node("/obj")
            geo = obj.createNode("geo", run_init_scripts=False)

            # ---- ローダーノード (形式によって切り替え) ----
            if fmt == "usd":
                loader_node = geo.createNode("usdimport", node_name="usdimport1")
                loader_node.parm("filepath1").set(main_path)

                # input_unpack: パラメータ名は Houdini バージョンによって異なる場合があるため
                # 複数候補を試みる
                for parm_name in ("input_unpack", "unpack", "unpackgeo"):
                    p = loader_node.parm(parm_name)
                    if p is not None:
                        p.set(1)
                        break

                # unpack_geomtype = polygons
                for parm_name in ("unpack_geomtype", "unpackgeomtype", "geomtype"):
                    p = loader_node.parm(parm_name)
                    if p is not None:
                        # メニューパラメータ: "polygons" か整数インデックス
                        try:
                            items = p.menuItems()
                            if "polygons" in items:
                                p.set(items.index("polygons"))
                            elif "polygon" in items:
                                p.set(items.index("polygon"))
                            else:
                                p.set(1)  # 多くの場合 0=all, 1=polygons
                        except Exception:
                            p.set(1)
                        break
            else:
                # FBX (デフォルト)
                loader_node = geo.createNode("file", node_name="file1")
                loader_node.parm("file").set(main_path)

            # ---- 以降は共通 ----
            transform_node = geo.createNode("xform", node_name="transform1")
            transform_node.setInput(0, loader_node)
            if self.fix_axis_check.isChecked():
                transform_node.parmTuple("r").set((-90, 0, 0))

            material_node = geo.createNode("material", node_name="material1")
            material_node.setInput(0, transform_node)
            shader = self._build_material(self.current_id, tex_paths)
            if shader is not None:
                mp = material_node.parm("shop_materialpath1")
                if mp is None:
                    mp = material_node.parm("shop_materialpath")
                if mp is not None:
                    mp.set(shader.path())

            null_node = geo.createNode("null", node_name="null1")
            null_node.setInput(0, material_node)
            null_node.setDisplayFlag(True)
            null_node.setRenderFlag(True)

            geo.layoutChildren()
            geo.setSelected(True, clear_all_selected=True)
        except Exception as e:
            hou.ui.displayMessage(
                "Downloaded the model, but could not fully build the geo "
                "network automatically.\nYou can wire it up manually.\n(%s)" % e,
                severity=hou.severityType.Warning,
            )


# ----------------------------------------------------------------------
# Shelf tool entry point
# ----------------------------------------------------------------------
def show():
    """シェルフツールから呼ばれるエントリポイント。"""
    dlg = PolyHavenDownloader(hou.qt.mainWindow())
    hou.session.polyhaven_downloader_dialog = dlg  # keep a reference alive
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


if __name__ == "__main__":
    show()