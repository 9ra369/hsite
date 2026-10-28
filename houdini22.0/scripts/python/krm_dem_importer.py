# -*- coding: utf-8 -*-
"""
krm_dem_importer.py
===================
地図GUIで範囲を選び、標高タイル(DEM)を取得して Houdini の HeightField として読み込むツール。

配置:
    <hsite>/houdini22.0/scripts/python/krm_dem_importer.py

シェルフ (krm_Shelf) に登録するスクリプト:
    import importlib, krm_dem_importer
    importlib.reload(krm_dem_importer)
    krm_dem_importer.show()

地図の操作:
    左ドラッグ ......................... 地図の移動
    ホイール / ダブルクリック .......... ズーム
    右ドラッグ / Shift + 左ドラッグ .... 取得範囲の選択

生成されるネットワーク (/obj/<名前>):
    dem_base (HeightField SOP) → dem_load (Python SOP: .npy を height に流し込む) → OUT_DEM
    DEM は $HIP/geo/dem/*.npy (+ 同名 .json のメタデータ) に保存されます。
    未保存シーンの場合は $HOUDINI_TEMP_DIR/krm_dem に保存されます。

座標系:
    標高タイルは Web メルカトル (EPSG:3857)。範囲中心の緯度でピクセル間隔をメートルに換算し、
    1 unit = 1 m（単位スケール 1.0 の場合）・北 = -Z・東 = +X で配置します。
    数 km〜数十 km 程度の範囲を想定しています（南北に広い範囲では縮尺誤差が増えます）。

データ出典（表示義務があります）:
    国土地理院 標高タイル / 地理院タイル, Terrain Tiles (Mapzen / AWS Open Data), © OpenStreetMap contributors
    地名検索: 国土地理院 地名検索API（日本語）, Nominatim / OpenStreetMap（全世界）
"""
from __future__ import annotations

import collections
import concurrent.futures
import datetime
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:  # Houdini 20.5 以前
    from PySide2 import QtCore, QtGui, QtWidgets

try:
    import hou
except ImportError:  # Houdini 外で地図GUIだけ試す場合
    hou = None


# =============================================================================
# 設定
# =============================================================================
TOOL_NAME = "KRM DEM Importer"
TILE = 256
EARTH_R = 6378137.0
MAX_LAT = 85.05112878
USER_AGENT = "krm_dem_importer/1.0 (Houdini tool)"
MAX_TILES = 4000            # 一度に取得するDEMタイル数の上限
WARN_PIXELS = 16_000_000    # これを超える出力は確認ダイアログを出す

GSI = "https://cyberjapandata.gsi.go.jp/xyz"

DEM_SOURCES = collections.OrderedDict([
    ("gsi_dem10b", dict(
        label="国土地理院 DEM10B（日本 / 約10m）",
        urls=[GSI + "/dem_png/{z}/{x}/{y}.png"],
        min_zoom=1, max_zoom=14, default_zoom=14, encoding="gsi",
        attribution="国土地理院 標高タイル（DEM10B）")),
    ("gsi_dem5", dict(
        label="国土地理院 DEM5A→5B→5C（日本 / 約5m・整備範囲のみ）",
        urls=[GSI + "/dem5a_png/{z}/{x}/{y}.png",
              GSI + "/dem5b_png/{z}/{x}/{y}.png",
              GSI + "/dem5c_png/{z}/{x}/{y}.png"],
        min_zoom=15, max_zoom=15, default_zoom=15, encoding="gsi",
        attribution="国土地理院 標高タイル（DEM5A/5B/5C）")),
    ("terrarium", dict(
        label="Terrain Tiles / Terrarium（全世界）",
        urls=["https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"],
        min_zoom=1, max_zoom=15, default_zoom=13, encoding="terrarium",
        attribution="Terrain Tiles (Mapzen / AWS Open Data)")),
])

BASEMAPS = collections.OrderedDict([
    ("地理院 淡色地図", dict(url=GSI + "/pale/{z}/{x}/{y}.png", attribution="地理院タイル")),
    ("地理院 標準地図", dict(url=GSI + "/std/{z}/{x}/{y}.png", attribution="地理院タイル")),
    ("地理院 写真", dict(url=GSI + "/seamlessphoto/{z}/{x}/{y}.jpg", attribution="地理院タイル（全国最新写真）")),
    ("OpenStreetMap", dict(url="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                           attribution="© OpenStreetMap contributors")),
])

GSI_GEOCODER = "https://msearch.gsi.go.jp/address-search/AddressSearch?q="
NOMINATIM = "https://nominatim.openstreetmap.org/search"

# Nominatim / OSM の利用ポリシーでは、連絡先を含む User-Agent が求められます。
# 自分のサイトURLやメールアドレスを入れてください（空でも動きますが、ブロックされる可能性があります）
CONTACT = ""
if CONTACT:
    USER_AGENT = "krm_dem_importer/1.1 (+{})".format(CONTACT)

# 日本のおおよその範囲（地理院データの提供範囲チェック用）
JAPAN_BBOX = (122.0, 20.0, 154.0, 46.0)


def _in_japan(lon, lat):
    w, s, e, n = JAPAN_BBOX
    return w <= lon <= e and s <= lat <= n


def _has_japanese(text):
    return re.search(r"[぀-ヿ㐀-鿿]", text) is not None


_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]


def geocode(text):
    """地名 → 候補リスト [{title, lon, lat, bbox(w,s,e,n) or None}]

    日本語を含む場合は地理院の地名検索を優先し、見つからなければ Nominatim（全世界）を使う。
    """
    results = []
    if _has_japanese(text):
        data = fetch_url(GSI_GEOCODER + urllib.parse.quote(text), timeout=8, use_cache=False)
        for r in (json.loads(data.decode("utf-8")) if data else [])[:8]:
            lon, lat = r["geometry"]["coordinates"][:2]
            results.append(dict(title=r.get("properties", {}).get("title", text),
                                lon=float(lon), lat=float(lat), bbox=None))
        if results:
            return results

    # Nominatim の利用ポリシー: 1リクエスト/秒まで
    with _nominatim_lock:
        wait = 1.0 - (time.time() - _nominatim_last[0])
        if wait > 0:
            time.sleep(wait)
        _nominatim_last[0] = time.time()
    query = urllib.parse.urlencode({"q": text, "format": "jsonv2", "limit": 8,
                                    "accept-language": "ja,en"})
    data = fetch_url(NOMINATIM + "?" + query, timeout=10, use_cache=False)
    for r in (json.loads(data.decode("utf-8")) if data else []):
        bbox = None
        if r.get("boundingbox"):
            s, n, w, e = (float(v) for v in r["boundingbox"])
            if e > w and n > s:
                bbox = (w, s, e, n)
        results.append(dict(title=r.get("display_name", text),
                            lon=float(r["lon"]), lat=float(r["lat"]), bbox=bbox))
    return results


# =============================================================================
# タイル座標計算（Web メルカトル）
# =============================================================================
def lonlat_to_world(lon, lat, z):
    """経緯度 → ズーム z でのワールドピクセル座標"""
    n = TILE * (1 << z)
    lat = max(min(lat, MAX_LAT), -MAX_LAT)
    s = math.sin(math.radians(lat))
    x = (lon + 180.0) / 360.0 * n
    y = (0.5 - math.log((1.0 + s) / (1.0 - s)) / (4.0 * math.pi)) * n
    return x, y


def world_to_lonlat(x, y, z):
    """ズーム z でのワールドピクセル座標 → 経緯度"""
    n = TILE * (1 << z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


class DemGrid(object):
    """選択範囲をズーム z のピクセルグリッドにスナップしたもの"""

    def __init__(self, bbox, z):
        west, south, east, north = bbox
        self.z = z
        fx0, fy0 = lonlat_to_world(west, north, z)
        fx1, fy1 = lonlat_to_world(east, south, z)
        self.ix0 = int(math.floor(fx0))
        self.iy0 = int(math.floor(fy0))
        self.ix1 = max(self.ix0 + 1, int(math.ceil(fx1)))
        self.iy1 = max(self.iy0 + 1, int(math.ceil(fy1)))
        self.width = self.ix1 - self.ix0
        self.height = self.iy1 - self.iy0
        self.tx0, self.tx1 = self.ix0 // TILE, (self.ix1 - 1) // TILE
        self.ty0, self.ty1 = self.iy0 // TILE, (self.iy1 - 1) // TILE

        w, n = world_to_lonlat(self.ix0, self.iy0, z)
        e, s = world_to_lonlat(self.ix1, self.iy1, z)
        self.bbox = (w, s, e, n)  # 実際に出力される範囲
        self.center = ((w + e) * 0.5, (s + n) * 0.5)
        # メルカトルは等角なので X/Y 同じ間隔。中心緯度で換算
        self.pixel_size_m = (2.0 * math.pi * EARTH_R * math.cos(math.radians(self.center[1]))
                             / (TILE * (1 << z)))

    @property
    def tile_count(self):
        return (self.tx1 - self.tx0 + 1) * (self.ty1 - self.ty0 + 1)

    def tiles(self):
        return [(tx, ty)
                for ty in range(self.ty0, self.ty1 + 1)
                for tx in range(self.tx0, self.tx1 + 1)]

    def assemble(self, tile_arrays):
        """{(tx, ty): ndarray(256, 256)} → (height, width) の配列。行0 = 北端"""
        out = np.full((self.height, self.width), np.nan, np.float32)
        for (tx, ty), arr in tile_arrays.items():
            if arr is None:
                continue
            ox, oy = tx * TILE, ty * TILE
            x0, x1 = max(ox, self.ix0), min(ox + TILE, self.ix1)
            y0, y1 = max(oy, self.iy0), min(oy + TILE, self.iy1)
            if x0 >= x1 or y0 >= y1:
                continue
            out[y0 - self.iy0:y1 - self.iy0, x0 - self.ix0:x1 - self.ix0] = \
                arr[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
        return out


# =============================================================================
# 取得・デコード
# =============================================================================
def _cache_root():
    base = os.environ.get("HOUDINI_TEMP_DIR") or tempfile.gettempdir()
    root = os.path.join(base, "krm_dem_cache")
    os.makedirs(root, exist_ok=True)
    return root


def fetch_url(url, timeout=20.0, use_cache=True):
    """URL を取得する（ディスクキャッシュ付き）。

    存在しないタイル (404) は b"" を返す。通信エラーは例外。
    """
    path = None
    if use_cache:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        path = os.path.join(_cache_root(), key[:2], key)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read()

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = res.read()
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            data = b""          # 範囲外・海域など。キャッシュしてよい
        elif ex.code == 403 and "elevation-tiles-prod" in url:
            return b""          # S3 は存在しないキーで 403 を返す（キャッシュはしない）
        else:
            raise

    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "{}.{}.tmp".format(path, threading.get_ident())
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    return data


def _png_to_rgb(data):
    """PNG/JPEG バイト列 → (r, g, b) の int64 配列。QImage はワーカースレッドでも使える"""
    img = QtGui.QImage.fromData(data)
    if img.isNull():
        return None
    img = img.convertToFormat(QtGui.QImage.Format.Format_RGB32)
    w, h = img.width(), img.height()
    try:
        px = np.frombuffer(img.constBits(), dtype=np.uint32, count=w * h).reshape(h, w).copy()
    except Exception:  # バインディングによって constBits が扱えない場合の保険（遅い）
        px = np.array([[img.pixel(x, y) for x in range(w)] for y in range(h)], dtype=np.uint32)
    px = px.astype(np.int64)
    return (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF


def decode_dem_png(data, encoding):
    """標高PNGタイル → float32 標高[m]（欠損は NaN）"""
    rgb = _png_to_rgb(data)
    if rgb is None:
        return None
    r, g, b = rgb
    if encoding == "gsi":
        # 地理院: x = 2^16R + 2^8G + B, x<2^23: h=0.01x / x=2^23: 無効 / x>2^23: h=0.01(x-2^24)
        x = (r << 16) | (g << 8) | b
        h = np.where(x < (1 << 23), x, x - (1 << 24)).astype(np.float64) * 0.01
        h[x == (1 << 23)] = np.nan
    elif encoding == "terrarium":
        h = r * 256.0 + g + b / 256.0 - 32768.0
    else:
        raise ValueError("unknown encoding: " + encoding)
    return h.astype(np.float32)


def fetch_dem_tile(source, z, x, y):
    """DEMタイルを1枚取得。複数URLがある場合は欠損ピクセルを後続ソースで補完する"""
    out = None
    for tmpl in source["urls"]:
        data = fetch_url(tmpl.format(z=z, x=x, y=y))
        if not data:
            continue
        h = decode_dem_png(data, source["encoding"])
        if h is None or h.shape != (TILE, TILE):
            continue
        if out is None:
            out = h
        else:
            m = np.isnan(out)
            out[m] = h[m]
        if not np.isnan(out).any():
            break
    if out is None:
        out = np.full((TILE, TILE), np.nan, np.float32)
    return out


# =============================================================================
# Houdini 側
# =============================================================================
PYSOP_CODE = r'''# krm_dem_importer: .npy の標高を入力 HeightField の height レイヤーへ書き込む
import json
import os
import numpy as np

node = hou.pwd()
geo = node.geometry()

path = node.evalParm("dem_file")
if not os.path.isfile(path):
    raise hou.NodeError("DEMファイルが見つかりません: " + path)
data = np.load(path).astype(np.float32)   # (rows, cols) 行0=北端, 列0=西端

meta = {}
meta_path = os.path.splitext(path)[0] + ".json"
if os.path.isfile(meta_path):
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

offset = 0.0
if node.evalParm("zero_base"):
    offset = float(data.min())
    data = data - offset
scale = node.evalParm("unit_scale")
data = data * (scale * node.evalParm("height_scale"))

vol = None
if geo.findPrimAttrib("name") is not None:
    for prim in geo.prims():
        if isinstance(prim, hou.Volume) and prim.attribValue("name") == "height":
            vol = prim
            break
if vol is None:
    raise hou.NodeError("入力に height ボリュームがありません（HeightField を接続してください）")

rx, ry, rz = vol.resolution()
if rz != 1:
    raise hou.NodeError("2D の HeightField ボリュームを想定しています: res=%s" % (vol.resolution(),))

# ボリュームのインデックス方向をワールド軸に対応させる（北=-Z, 東=+X）
p00 = vol.indexToPos((0, 0, 0))
di = vol.indexToPos((1, 0, 0)) - p00
dj = vol.indexToPos((0, 1, 0)) - p00
grid = data
if abs(di[0]) >= abs(di[2]):      # i→X, j→Z
    if di[0] < 0:
        grid = grid[:, ::-1]
    if dj[2] < 0:
        grid = grid[::-1, :]
    target = grid
else:                              # i→Z, j→X
    if di[2] < 0:
        grid = grid[::-1, :]
    if dj[0] < 0:
        grid = grid[:, ::-1]
    target = grid.T

if target.shape != (ry, rx):
    yi = np.round(np.linspace(0, target.shape[0] - 1, ry)).astype(np.int64)
    xi = np.round(np.linspace(0, target.shape[1] - 1, rx)).astype(np.int64)
    target = target[yi][:, xi]
    node.addWarning("解像度が一致しないためリサンプルしました: DEM %dx%d → HeightField %dx%d"
                    % (data.shape[1], data.shape[0], rx, ry))

vol.setAllVoxelsFromString(np.ascontiguousarray(target, dtype=np.float32).tobytes())

def _detail(name, value):
    if geo.findGlobalAttrib(name) is None:
        geo.addAttrib(hou.attribType.Global, name, value)
    geo.setGlobalAttribValue(name, value)

if meta:
    _detail("dem_bbox_wsen", tuple(float(v) for v in meta["bbox_wsen"]))
    _detail("dem_center_lonlat", tuple(float(v) for v in meta["center_lonlat"]))
    _detail("dem_pixel_size_m", float(meta["pixel_size_m"]))
    _detail("dem_zoom", int(meta["zoom"]))
    _detail("dem_source", str(meta["source_label"]))
    _detail("dem_crs", str(meta["crs"]))
_detail("dem_height_offset", offset)
_detail("dem_unit_scale", float(scale))
'''


def _safe_name(text, default="dem"):
    name = re.sub(r"[^0-9A-Za-z_]", "_", text.strip()) or default
    if name[0].isdigit():
        name = "_" + name
    return name


def _output_dir():
    """$HIP/geo/dem（未保存シーンは一時フォルダ）。(実パス, パラメータ用パス) を返す"""
    is_new = True
    try:
        is_new = hou.hipFile.isNewFile()
    except Exception:
        pass
    if not is_new:
        real = os.path.join(hou.text.expandString("$HIP"), "geo", "dem")
        os.makedirs(real, exist_ok=True)
        return real, "$HIP/geo/dem"
    base = os.environ.get("HOUDINI_TEMP_DIR") or tempfile.gettempdir()
    real = os.path.join(base, "krm_dem").replace("\\", "/")
    os.makedirs(real, exist_ok=True)
    return real, real


def save_dem(dem, meta, name):
    real_dir, parm_dir = _output_dir()
    base = "{}_z{}".format(name, meta["zoom"])
    stem, i = base, 2
    while os.path.exists(os.path.join(real_dir, stem + ".npy")):
        stem = "{}_{}".format(base, i)
        i += 1
    np.save(os.path.join(real_dir, stem + ".npy"), dem.astype(np.float32))
    with open(os.path.join(real_dir, stem + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return "{}/{}.npy".format(parm_dir, stem)


def _first_parm(node, names):
    for n in names:
        p = node.parm(n)
        if p is not None:
            return p
    return None


def build_houdini_network(npy_parm_path, meta, name, unit_scale, zero_base):
    obj = hou.node("/obj")
    node_name, i = name, 2
    while obj.node(node_name) is not None:
        node_name = "{}_{}".format(name, i)
        i += 1

    geo = obj.createNode("geo", node_name)
    for child in geo.children():
        child.destroy()

    hf = geo.createNode("heightfield", "dem_base")
    py = geo.createNode("python", "dem_load")
    py.setInput(0, hf)

    # --- Python SOP のパラメータ ---
    ptg = py.parmTemplateGroup()
    folder = hou.FolderParmTemplate("krm_dem", "DEM", folder_type=hou.folderType.Simple)
    folder.addParmTemplate(hou.StringParmTemplate(
        "dem_file", "DEM File (.npy)", 1, default_value=(npy_parm_path,),
        string_type=hou.stringParmType.FileReference))
    folder.addParmTemplate(hou.FloatParmTemplate(
        "unit_scale", "Unit Scale (1 = 1m)", 1, default_value=(unit_scale,), min=0.0001, max=1.0))
    folder.addParmTemplate(hou.ToggleParmTemplate(
        "zero_base", "Lowest Point = 0", default_value=zero_base))
    folder.addParmTemplate(hou.FloatParmTemplate(
        "height_scale", "Height Exaggeration", 1, default_value=(1.0,), min=0.0, max=10.0))
    ptg.insertBefore(ptg.entries()[0], folder)
    py.setParmTemplateGroup(ptg)
    py.parm("python").set(PYSOP_CODE)

    # --- HeightField のサイズを DEM に合わせる（unit_scale に追従） ---
    dx = meta["pixel_size_m"]
    w, h = meta["shape"][1], meta["shape"][0]
    ref = 'ch("../{}/unit_scale")'.format(py.name())
    p_sx = _first_parm(hf, ["sizex", "size1"])
    p_sy = _first_parm(hf, ["sizey", "size2"])
    p_gs = _first_parm(hf, ["gridspacing", "divsize"])
    if p_sx and p_sy and p_gs:
        p_sx.setExpression("{} * {!r}".format(ref, w * dx))
        p_sy.setExpression("{} * {!r}".format(ref, h * dx))
        p_gs.setExpression("{} * {!r}".format(ref, dx))
    else:
        print("[{}] HeightField のサイズパラメータが見つかりません。dem_base を手動で調整してください".format(TOOL_NAME))

    out = geo.createNode("null", "OUT_DEM")
    out.setInput(0, py)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)
    geo.layoutChildren()
    geo.moveToGoodPosition()
    return geo


# =============================================================================
# 地図ウィジェット
# =============================================================================
def _event_pos(event):
    return event.position() if hasattr(event, "position") else QtCore.QPointF(event.pos())


class TileMapWidget(QtWidgets.QWidget):
    """XYZタイルを表示し、矩形範囲を選択できる軽量スリッピーマップ"""

    selectionChanged = QtCore.Signal(object)   # (west, south, east, north) or None
    cursorMoved = QtCore.Signal(float, float)  # lon, lat
    _tileReady = QtCore.Signal(str, object)    # key, bytes | None(通信エラー)

    MIN_Z, MAX_Z = 2, 18
    CACHE_MAX = 600
    SEL_COLOR = QtGui.QColor(255, 150, 40)

    def __init__(self, parent=None):
        super(TileMapWidget, self).__init__(parent)
        self.setMinimumSize(480, 400)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

        self._basemap = next(iter(BASEMAPS))
        self._z = 5
        self._cx, self._cy = lonlat_to_world(137.0, 37.0, self._z)
        self._tiles = collections.OrderedDict()  # key -> QPixmap | None(タイル無し)
        self._pending = set()
        self._wanted = set()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        self._alive = True
        self._sel = None
        self._drag = None
        self._tileReady.connect(self._on_tile_ready)

    # ---- 公開API ----------------------------------------------------------
    def zoom(self):
        return self._z

    def set_basemap(self, name):
        self._basemap = name
        self.update()

    def selection(self):
        return self._sel

    def set_selection(self, bbox):
        self._sel = bbox
        self.update()

    def center_lonlat(self):
        return world_to_lonlat(self._cx, self._cy, self._z)

    def center_on(self, lon, lat, z=None):
        if z is not None:
            self._z = int(max(self.MIN_Z, min(self.MAX_Z, z)))
        self._cx, self._cy = lonlat_to_world(lon, lat, self._z)
        self._clamp()
        self.update()

    def fit_bbox(self, bbox, margin=0.8):
        if not bbox:
            return
        w, s, e, n = bbox
        z = self.MIN_Z
        for cand in range(self.MAX_Z, self.MIN_Z - 1, -1):
            x0, y0 = lonlat_to_world(w, n, cand)
            x1, y1 = lonlat_to_world(e, s, cand)
            if (x1 - x0) <= self.width() * margin and (y1 - y0) <= self.height() * margin:
                z = cand
                break
        self.center_on((w + e) * 0.5, (s + n) * 0.5, z)

    def shutdown(self):
        self._alive = False
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---- タイル読み込み ---------------------------------------------------
    def _key(self, z, x, y):
        return "{}/{}/{}/{}".format(self._basemap, z, x, y)

    def _request_tile(self, z, x, y):
        key = self._key(z, x, y)
        if key in self._tiles:
            self._tiles.move_to_end(key)
            return self._tiles[key]
        if key not in self._pending:
            self._pending.add(key)
            url = BASEMAPS[self._basemap]["url"].format(z=z, x=x, y=y)
            self._pool.submit(self._load_tile, key, url)
        return None

    def _load_tile(self, key, url):
        data = None
        if key in self._wanted:  # 画面外に流れたタイルは取得しない
            try:
                data = fetch_url(url, timeout=15)
            except Exception:
                data = None
        if self._alive:
            try:
                self._tileReady.emit(key, data)
            except RuntimeError:
                pass

    def _on_tile_ready(self, key, data):
        self._pending.discard(key)
        if data is None:      # 通信エラー or 不要になった → キャッシュせず次の描画で再要求
            return
        pm = QtGui.QPixmap()
        if not data or not pm.loadFromData(data):
            pm = None
        self._tiles[key] = pm
        while len(self._tiles) > self.CACHE_MAX:
            self._tiles.popitem(last=False)
        self.update()

    # ---- 座標 -------------------------------------------------------------
    def _clamp(self):
        n = TILE * (1 << self._z)
        self._cx %= n
        self._cy = max(0.0, min(float(n), self._cy))

    def _origin(self):
        return self._cx - self.width() * 0.5, self._cy - self.height() * 0.5

    def _screen_to_lonlat(self, pt):
        x0, y0 = self._origin()
        n = TILE * (1 << self._z)
        x = (x0 + pt.x()) % n
        y = max(0.0, min(float(n), y0 + pt.y()))
        return world_to_lonlat(x, y, self._z)

    def _lonlat_to_screen(self, lon, lat):
        x, y = lonlat_to_world(lon, lat, self._z)
        x0, y0 = self._origin()
        return QtCore.QPointF(x - x0, y - y0)

    # ---- 描画 -------------------------------------------------------------
    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        p.fillRect(self.rect(), QtGui.QColor(38, 38, 38))

        z, n = self._z, 1 << self._z
        x0, y0 = self._origin()
        tx0 = int(math.floor(x0 / TILE))
        tx1 = int(math.floor((x0 + self.width()) / TILE))
        ty0 = max(0, int(math.floor(y0 / TILE)))
        ty1 = min(n - 1, int(math.floor((y0 + self.height()) / TILE)))

        wanted = set()
        self._wanted = wanted
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                wx = tx % n
                wanted.add(self._key(z, wx, ty))
                target = QtCore.QRectF(tx * TILE - x0, ty * TILE - y0, TILE, TILE)
                pm = self._request_tile(z, wx, ty)
                if pm is not None:
                    p.drawPixmap(target, pm, QtCore.QRectF(pm.rect()))
                else:
                    self._draw_parent(p, target, z, wx, ty)

        if self._sel:
            w, s, e, nn = self._sel
            r = QtCore.QRectF(self._lonlat_to_screen(w, nn), self._lonlat_to_screen(e, s)).normalized()
            p.setPen(QtGui.QPen(self.SEL_COLOR, 2))
            fill = QtGui.QColor(self.SEL_COLOR)
            fill.setAlpha(45)
            p.setBrush(fill)
            p.drawRect(r)

        self._draw_overlay(p)
        p.end()

    def _draw_parent(self, p, target, z, x, y):
        """未読み込みのタイルは、キャッシュ済みの親タイルを拡大して仮表示"""
        for dz in range(1, 7):
            if z - dz < 0:
                break
            pm = self._tiles.get(self._key(z - dz, x >> dz, y >> dz))
            if pm:
                size = pm.width() / float(1 << dz)
                sx = (x - ((x >> dz) << dz)) * size
                sy = (y - ((y >> dz) << dz)) * size
                p.drawPixmap(target, pm, QtCore.QRectF(sx, sy, size, size))
                return

    def _draw_overlay(self, p):
        font = p.font()
        font.setPointSizeF(max(7.5, font.pointSizeF() * 0.9))
        p.setFont(font)
        fm = QtGui.QFontMetrics(font)
        p.setPen(QtCore.Qt.PenStyle.NoPen)

        def label(text, anchor):
            rect = fm.boundingRect(text).adjusted(-6, -3, 6, 3)
            if anchor == "br":
                rect.moveBottomRight(QtCore.QPoint(self.width() - 4, self.height() - 4))
            else:
                rect.moveTopLeft(QtCore.QPoint(4, 4))
            p.setBrush(QtGui.QColor(0, 0, 0, 150))
            p.drawRoundedRect(rect, 3, 3)
            p.setPen(QtGui.QColor(230, 230, 230))
            p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, text)
            p.setPen(QtCore.Qt.PenStyle.NoPen)

        label(BASEMAPS[self._basemap]["attribution"], "br")
        label("z{}   左ドラッグ: 移動 / 右ドラッグ・Shift+ドラッグ: 範囲選択".format(self._z), "tl")

    # ---- 入力 -------------------------------------------------------------
    def mousePressEvent(self, event):
        pos = _event_pos(event)
        btn = event.button()
        shift = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
        if btn == QtCore.Qt.MouseButton.RightButton or (btn == QtCore.Qt.MouseButton.LeftButton and shift):
            lon, lat = self._screen_to_lonlat(pos)
            self._drag = ("select", lon, lat)
            self._sel = None
            self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        elif btn == QtCore.Qt.MouseButton.LeftButton:
            self._drag = ("pan", pos, self._cx, self._cy)
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        pos = _event_pos(event)
        lon, lat = self._screen_to_lonlat(pos)
        self.cursorMoved.emit(lon, lat)
        if not self._drag:
            return
        if self._drag[0] == "pan":
            _, p0, cx0, cy0 = self._drag
            self._cx = cx0 - (pos.x() - p0.x())
            self._cy = cy0 - (pos.y() - p0.y())
            self._clamp()
        else:
            _, lon0, lat0 = self._drag
            self._sel = (min(lon0, lon), min(lat0, lat), max(lon0, lon), max(lat0, lat))
            self.selectionChanged.emit(self._sel)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._drag and self._drag[0] == "select":
            if self._sel and ((self._sel[2] - self._sel[0]) < 1e-7 or (self._sel[3] - self._sel[1]) < 1e-7):
                self._sel = None
            self.selectionChanged.emit(self._sel)
            self.update()
        self._drag = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.zoom_at(_event_pos(event), 1)

    def wheelEvent(self, event):
        d = event.angleDelta().y()
        if d:
            self.zoom_at(_event_pos(event), 1 if d > 0 else -1)

    def zoom_at(self, pos, dz):
        nz = max(self.MIN_Z, min(self.MAX_Z, self._z + dz))
        if nz == self._z:
            return
        f = 2.0 ** (nz - self._z)
        mx, my = pos.x() - self.width() * 0.5, pos.y() - self.height() * 0.5
        self._cx = (self._cx + mx) * f - mx
        self._cy = (self._cy + my) * f - my
        self._z = nz
        self._clamp()
        self.update()


# =============================================================================
# メインダイアログ
# =============================================================================
class DemImporterDialog(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super(DemImporterDialog, self).__init__(parent)
        self.setWindowTitle(TOOL_NAME)
        self.resize(1240, 780)
        self._settings = QtCore.QSettings("krm", "krm_dem_importer")

        self.map = TileMapWidget(self)
        self._build_ui()
        self._restore_settings()
        self._on_source_changed()
        self._update_info()

    # ---- UI --------------------------------------------------------------
    def _build_ui(self):
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        left = QtWidgets.QVBoxLayout()
        left.addWidget(self.map, 1)
        self.cursor_label = QtWidgets.QLabel("—")
        self.cursor_label.setStyleSheet("color: #9a9a9a;")
        left.addWidget(self.cursor_label)
        root.addLayout(left, 1)

        side = QtWidgets.QWidget()
        side.setFixedWidth(330)
        lay = QtWidgets.QVBoxLayout(side)
        lay.setContentsMargins(0, 0, 0, 0)
        root.addWidget(side)

        # 移動
        g = QtWidgets.QGroupBox("場所へ移動")
        gl = QtWidgets.QHBoxLayout(g)
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("富士山 / Yosemite El Capitan / 35.36, 138.72")
        btn = QtWidgets.QPushButton("移動")
        gl.addWidget(self.search_edit, 1)
        gl.addWidget(btn)
        self.search_edit.returnPressed.connect(self._on_search)
        btn.clicked.connect(self._on_search)
        lay.addWidget(g)

        # 背景地図
        g = QtWidgets.QGroupBox("背景地図")
        gl = QtWidgets.QVBoxLayout(g)
        self.basemap_combo = QtWidgets.QComboBox()
        self.basemap_combo.addItems(list(BASEMAPS.keys()))
        self.basemap_combo.currentTextChanged.connect(self.map.set_basemap)
        gl.addWidget(self.basemap_combo)
        lay.addWidget(g)

        # 範囲
        g = QtWidgets.QGroupBox("取得範囲（度）")
        grid = QtWidgets.QGridLayout(g)
        self.spins = {}
        for row, col, key, text, lim in ((0, 0, "n", "北", 85), (0, 2, "e", "東", 180),
                                         (1, 0, "s", "南", 85), (1, 2, "w", "西", 180)):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setDecimals(6)
            sb.setRange(-lim, lim)
            sb.setKeyboardTracking(False)
            sb.valueChanged.connect(self._on_spin_changed)
            self.spins[key] = sb
            grid.addWidget(QtWidgets.QLabel(text), row, col, QtCore.Qt.AlignmentFlag.AlignRight)
            grid.addWidget(sb, row, col + 1)
        bl = QtWidgets.QHBoxLayout()
        b_fit = QtWidgets.QPushButton("範囲を表示")
        b_clr = QtWidgets.QPushButton("クリア")
        b_fit.clicked.connect(lambda: self.map.fit_bbox(self.map.selection()))
        b_clr.clicked.connect(self._clear_selection)
        bl.addWidget(b_fit)
        bl.addWidget(b_clr)
        grid.addLayout(bl, 3, 0, 1, 4)
        lay.addWidget(g)

        # DEM
        g = QtWidgets.QGroupBox("DEM")
        fl = QtWidgets.QFormLayout(g)
        self.source_combo = QtWidgets.QComboBox()
        for key, src in DEM_SOURCES.items():
            self.source_combo.addItem(src["label"], key)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        self.zoom_spin = QtWidgets.QSpinBox()
        self.zoom_spin.valueChanged.connect(self._update_info)
        self.zoom_spin.setToolTip("小さくすると粗く・広範囲向けになります（1段階でピクセル間隔が2倍）")
        self.info_label = QtWidgets.QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        fl.addRow("ソース", self.source_combo)
        fl.addRow("解像度 (zoom)", self.zoom_spin)
        fl.addRow(self.info_label)
        lay.addWidget(g)

        # 出力
        g = QtWidgets.QGroupBox("Houdini 出力")
        fl = QtWidgets.QFormLayout(g)
        self.name_edit = QtWidgets.QLineEdit("dem")
        self.scale_spin = QtWidgets.QDoubleSpinBox()
        self.scale_spin.setDecimals(4)
        self.scale_spin.setRange(0.0001, 1000.0)
        self.scale_spin.setValue(1.0)
        self.scale_spin.setToolTip("1.0 = 1 unit が 1m。広域なら 0.01 などに")
        self.zero_check = QtWidgets.QCheckBox("最低点を高さ 0 にする")
        self.nodata_combo = QtWidgets.QComboBox()
        self.nodata_combo.addItems(["最低標高で埋める", "0 m で埋める"])
        fl.addRow("ノード名", self.name_edit)
        fl.addRow("単位スケール", self.scale_spin)
        fl.addRow("", self.zero_check)
        fl.addRow("欠損値（海域など）", self.nodata_combo)
        lay.addWidget(g)

        lay.addStretch(1)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        lay.addWidget(self.status_label)
        self.import_btn = QtWidgets.QPushButton("Houdini に読み込み")
        self.import_btn.setMinimumHeight(34)
        self.import_btn.setStyleSheet(
            "QPushButton { background: #c8782a; color: white; font-weight: bold; border-radius: 3px; }"
            "QPushButton:disabled { background: #555; color: #999; }")
        self.import_btn.clicked.connect(self._on_import)
        lay.addWidget(self.import_btn)

        self.map.selectionChanged.connect(self._on_map_selection)
        self.map.cursorMoved.connect(
            lambda lon, lat: self.cursor_label.setText("緯度 {:.6f}   経度 {:.6f}".format(lat, lon)))

    # ---- 設定の保存/復元 --------------------------------------------------
    def _restore_settings(self):
        s = self._settings
        try:
            lon = s.value("lon", 137.0, type=float)
            lat = s.value("lat", 37.0, type=float)
            z = s.value("z", 5, type=int)
            self.map.center_on(lon, lat, z)
            bm = s.value("basemap", "", type=str)
            if bm in BASEMAPS:
                self.basemap_combo.setCurrentText(bm)
            idx = self.source_combo.findData(s.value("source", "gsi_dem10b", type=str))
            if idx >= 0:
                self.source_combo.setCurrentIndex(idx)
            self.scale_spin.setValue(s.value("unit_scale", 1.0, type=float))
            self.zero_check.setChecked(s.value("zero_base", False, type=bool))
        except Exception:
            pass

    def _save_settings(self):
        s = self._settings
        lon, lat = self.map.center_lonlat()
        s.setValue("lon", lon)
        s.setValue("lat", lat)
        s.setValue("z", self.map.zoom())
        s.setValue("basemap", self.basemap_combo.currentText())
        s.setValue("source", self.source_combo.currentData())
        s.setValue("unit_scale", self.scale_spin.value())
        s.setValue("zero_base", self.zero_check.isChecked())

    def closeEvent(self, event):
        self._save_settings()
        self.map.shutdown()
        super(DemImporterDialog, self).closeEvent(event)

    def reject(self):  # Esc でも設定保存
        self._save_settings()
        self.map.shutdown()
        super(DemImporterDialog, self).reject()

    # ---- 範囲 ------------------------------------------------------------
    def _on_map_selection(self, bbox):
        for sb in self.spins.values():
            sb.blockSignals(True)
        if bbox:
            w, s, e, n = bbox
            self.spins["w"].setValue(w)
            self.spins["s"].setValue(s)
            self.spins["e"].setValue(e)
            self.spins["n"].setValue(n)
        for sb in self.spins.values():
            sb.blockSignals(False)
        self._update_info()

    def _on_spin_changed(self, *_):
        w, e = sorted((self.spins["w"].value(), self.spins["e"].value()))
        s, n = sorted((self.spins["s"].value(), self.spins["n"].value()))
        self.map.set_selection((w, s, e, n) if (e > w and n > s) else None)
        self._update_info()

    def _clear_selection(self):
        self.map.set_selection(None)
        self._update_info()

    # ---- DEM 情報 ----------------------------------------------------------
    def _current_source(self):
        key = self.source_combo.currentData()
        return key, DEM_SOURCES[key]

    def _on_source_changed(self, *_):
        _, src = self._current_source()
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setRange(src["min_zoom"], src["max_zoom"])
        self.zoom_spin.setValue(src["default_zoom"])
        self.zoom_spin.blockSignals(False)
        self._update_info()

    def _update_info(self, *_):
        bbox = self.map.selection()
        if not bbox:
            self.info_label.setText("<span style='color:#9a9a9a'>地図上で右ドラッグ（または Shift+ドラッグ）して範囲を選択してください。</span>")
            self.import_btn.setEnabled(False)
            return
        grid = DemGrid(bbox, self.zoom_spin.value())
        px = grid.width * grid.height
        km_w = grid.width * grid.pixel_size_m / 1000.0
        km_h = grid.height * grid.pixel_size_m / 1000.0
        warn = ""
        if grid.tile_count > MAX_TILES:
            warn = "<br><span style='color:#ff6b6b'>タイル数が上限 {} を超えています。範囲を狭めるか zoom を下げてください。</span>".format(MAX_TILES)
        elif px > WARN_PIXELS:
            warn = "<br><span style='color:#f0b040'>出力が大きいため、読み込みに時間とメモリを使います。</span>"
        if self._current_source()[0].startswith("gsi") and not _in_japan(*grid.center):
            warn += "<br><span style='color:#f0b040'>国外の範囲です。地理院DEMにはデータがありません → Terrarium を選んでください。</span>"
        self.info_label.setText(
            "出力 <b>{} × {}</b> px<br>"
            "ピクセル間隔 約 <b>{:.2f} m</b><br>"
            "範囲 約 {:.2f} × {:.2f} km<br>"
            "タイル数 {}{}".format(grid.width, grid.height, grid.pixel_size_m, km_w, km_h,
                                   grid.tile_count, warn))
        self.import_btn.setEnabled(grid.tile_count <= MAX_TILES)

    # ---- 検索 ------------------------------------------------------------
    def _on_search(self):
        text = self.search_edit.text().strip()
        if not text:
            return
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$", text)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            self.map.center_on(lon, lat, max(self.map.zoom(), 13))
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            results = geocode(text)
        except Exception as ex:
            results = None
            self._status("検索に失敗しました: {}".format(ex), error=True)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if results is None:
            return
        if not results:
            self._status("「{}」は見つかりませんでした。英語名でも試してください。".format(text), error=True)
            return
        if len(results) == 1:
            self._go_to(results[0])
            return

        # 候補が複数 → 検索欄の下にメニューで出す
        menu = QtWidgets.QMenu(self)
        for r in results:
            title = r["title"] if len(r["title"]) <= 90 else r["title"][:87] + "…"
            act = menu.addAction(title)
            act.setData(r)
        act = menu.exec(self.search_edit.mapToGlobal(QtCore.QPoint(0, self.search_edit.height())))
        if act is not None:
            self._go_to(act.data())

    def _go_to(self, r):
        if r.get("bbox"):
            self.map.fit_bbox(r["bbox"], margin=0.9)
        else:
            self.map.center_on(r["lon"], r["lat"], 14)

        notes = []
        if not _in_japan(r["lon"], r["lat"]):
            # 海外: 地理院データは無いので切り替える
            src_key, _ = self._current_source()
            if src_key.startswith("gsi"):
                self.source_combo.setCurrentIndex(self.source_combo.findData("terrarium"))
                notes.append("DEMソースを Terrarium に切り替えました")
            if self.basemap_combo.currentText().startswith("地理院"):
                self.basemap_combo.setCurrentText("OpenStreetMap")
                notes.append("背景を OSM に切り替えました")
        self._status(r["title"] + ("\n" + " / ".join(notes) if notes else ""))

    def _status(self, text, error=False):
        self.status_label.setStyleSheet("color: {};".format("#ff6b6b" if error else "#9a9a9a"))
        self.status_label.setText(text)

    # ---- 読み込み ----------------------------------------------------------
    def _on_import(self):
        if hou is None:
            QtWidgets.QMessageBox.information(self, TOOL_NAME, "Houdini 外では読み込みできません。")
            return
        bbox = self.map.selection()
        if not bbox:
            return
        src_key, src = self._current_source()
        z = self.zoom_spin.value()
        grid = DemGrid(bbox, z)
        if grid.tile_count > MAX_TILES:
            return
        if grid.width * grid.height > WARN_PIXELS:
            ans = QtWidgets.QMessageBox.question(
                self, TOOL_NAME,
                "出力が {} × {} px と大きくなります。続けますか？".format(grid.width, grid.height))
            if ans != QtWidgets.QMessageBox.StandardButton.Yes:
                return

        tiles, errors = self._download_tiles(src, z, grid)
        if tiles is None:
            self._status("キャンセルしました。")
            return
        if errors:
            (tx, ty), ex = errors[0]
            QtWidgets.QMessageBox.warning(
                self, TOOL_NAME,
                "{} / {} 枚のタイル取得に失敗しました。\n例: z{}/{}/{} — {}\n\n"
                "取得済みタイルはキャッシュされるので、再実行すると続きから取得します。".format(
                    len(errors), grid.tile_count, z, tx, ty, ex))
            return

        dem = grid.assemble(tiles)
        valid = np.isfinite(dem)
        if not valid.any():
            QtWidgets.QMessageBox.warning(self, TOOL_NAME,
                                          "この範囲には標高データがありません（海域 または 提供範囲外）。")
            return
        nodata = int((~valid).sum())
        fill = float(dem[valid].min()) if self.nodata_combo.currentIndex() == 0 else 0.0
        dem = np.where(valid, dem, fill).astype(np.float32)

        name = _safe_name(self.name_edit.text())
        meta = dict(
            tool="krm_dem_importer",
            created=datetime.datetime.now().isoformat(timespec="seconds"),
            source=src_key,
            source_label=src["label"],
            attribution=src["attribution"],
            zoom=z,
            crs="EPSG:3857 (Web Mercator) / pixel size at center latitude",
            bbox_wsen=list(grid.bbox),
            requested_bbox_wsen=list(bbox),
            center_lonlat=list(grid.center),
            shape=[grid.height, grid.width],
            pixel_size_m=grid.pixel_size_m,
            height_min=float(dem.min()),
            height_max=float(dem.max()),
            nodata_pixels=nodata,
            nodata_fill=fill,
        )
        try:
            with hou.undos.group("DEM Import"):
                npy_path = save_dem(dem, meta, name)
                node = build_houdini_network(npy_path, meta, name,
                                             self.scale_spin.value(), self.zero_check.isChecked())
            node.setSelected(True, clear_all_selected=True)
        except Exception as ex:
            traceback.print_exc()
            QtWidgets.QMessageBox.critical(self, TOOL_NAME, "Houdini への読み込みに失敗しました:\n{}".format(ex))
            return

        msg = "{} を作成しました（{} × {} px, {:.0f}〜{:.0f} m）".format(
            node.path(), grid.width, grid.height, meta["height_min"], meta["height_max"])
        if nodata:
            msg += "\n欠損 {} px を {:.1f} m で埋めました".format(nodata, fill)
        self._status(msg)

    def _download_tiles(self, src, z, grid):
        coords = grid.tiles()
        progress = QtWidgets.QProgressDialog("標高タイルを取得中…", "キャンセル", 0, len(coords), self)
        progress.setWindowTitle(TOOL_NAME)
        progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        futures = {pool.submit(fetch_dem_tile, src, z, tx, ty): (tx, ty) for tx, ty in coords}
        results, errors = {}, []
        try:
            pending = set(futures)
            while pending:
                done, pending = concurrent.futures.wait(pending, timeout=0.05)
                for f in done:
                    try:
                        results[futures[f]] = f.result()
                    except Exception as ex:
                        errors.append((futures[f], ex))
                progress.setValue(len(coords) - len(pending))
                QtWidgets.QApplication.processEvents()
                if progress.wasCanceled():
                    return None, []
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            progress.close()
        return results, errors


# =============================================================================
# エントリポイント
# =============================================================================
_dialog = None


def show():
    global _dialog
    if _dialog is not None:
        try:
            _dialog.close()
        except Exception:
            pass
    parent = hou.qt.mainWindow() if hou is not None else None
    _dialog = DemImporterDialog(parent)
    _dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()
    return _dialog


if __name__ == "__main__":  # Houdini 外で地図GUIだけ確認する
    import sys
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    show()
    sys.exit(app.exec())