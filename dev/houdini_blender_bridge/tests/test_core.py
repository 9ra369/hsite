"""hou に依存しない層のテスト。Houdini 無しで走る。

    cd houdini_blender_bridge
    PYTHONPATH=python pytest tests -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from blender_bridge.core import library                 # noqa: E402
from blender_bridge.core import manifest as mf          # noqa: E402
from blender_bridge.core import matching                # noqa: E402
from blender_bridge.core.naming import sanitize, unique  # noqa: E402


# ------------------------------------------------------------------ naming

@pytest.mark.parametrize("raw, expected", [
    ("M_Rock_01", "M_Rock_01"),
    ("Rock Base.001", "Rock_Base_001"),
    ("  ", "material"),
    ("01_wall", "_01_wall"),
    ("岩", "material"),
    ("a---b", "a_b"),
])
def test_sanitize(raw, expected):
    assert sanitize(raw) == expected


def test_unique():
    taken = set()
    assert unique("wall", taken) == "wall"
    assert unique("wall", taken) == "wall_1"
    assert unique("wall", taken) == "wall_2"


# ---------------------------------------------------------------- matching

class FakeMaterial:
    def __init__(self, source_name, safe_name=None):
        self.source_name = source_name
        self.safe_name = safe_name or sanitize(source_name)


def test_exact_match_wins():
    materials = [FakeMaterial("M_Wall"), FakeMaterial("m_wall")]
    index = matching.build_index(materials)
    found, how = index.lookup("M_Wall")
    assert how == matching.EXACT
    assert found.source_name == "M_Wall"


def test_case_insensitive_match():
    index = matching.build_index([FakeMaterial("M_Wall")])
    found, how = index.lookup("m_WALL")
    assert how == matching.CASE_INSENSITIVE
    assert found.source_name == "M_Wall"


def test_ambiguous_is_refused():
    """大文字小文字だけが違う 2 つがある場合、推測で片方を選んではいけない。"""
    index = matching.build_index([FakeMaterial("Rock"), FakeMaterial("rock")])
    found, how = index.lookup("ROCK")
    assert found is None
    assert how == matching.AMBIGUOUS


def test_exact_mode_ignores_case():
    index = matching.build_index([FakeMaterial("M_Wall")], case_insensitive=False)
    found, how = index.lookup("m_wall")
    assert found is None
    assert how == matching.UNMATCHED


def test_lookup_uses_last_token_only():
    index = matching.build_index([FakeMaterial("M_Wall")])
    found, _ = index.lookup_usd_path("/root/_materials/M_Wall")
    assert found is not None
    # スコープが変わっても引ける
    found, _ = index.lookup_usd_path("/anything/else/M_Wall")
    assert found is not None


def test_ambiguous_names_listing():
    materials = [FakeMaterial("Rock"), FakeMaterial("rock"), FakeMaterial("Moss")]
    assert matching.ambiguous_names(materials) == ["Rock, rock"]


# ---------------------------------------------------------------- manifest

def _write(tmp_path, payload, geometry=True):
    asset = tmp_path / "building_a"
    asset.mkdir(exist_ok=True)
    if geometry:
        (asset / "building_a.usdc").write_bytes(b"stub")
    path = asset / "building_a.bridge.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


BASE = {
    "schema_version": "2.0.0",
    "asset": {"id": "building_a", "name": "building_a"},
    "geometry": {"format": "usd", "path": "building_a.usdc",
                 "material_id_attribute": "bridge_material_id"},
    "objects": [{"id": "obj_0000", "source_name": "Wall"}],
    "materials": [{
        "id": "mat_0000",
        "source_name": "M_Wall",
        "safe_name": "M_Wall",
        "channels": {
            "basecolor": {"type": "texture", "path": "tex/M_Wall/basecolor.png",
                          "colorspace": "srgb"},
            "metallic": {"type": "constant", "value": 0.0},
            "normal": {"type": "constant", "value": [0.0, 0.0, 0.0]},
        },
    }],
    "assignments": [{"object_id": "obj_0000", "material_id": "mat_0000"}],
    "warnings": ["M_Wall: basecolor inferred from filename"],
}


def test_load_basic(tmp_path):
    data = mf.load(_write(tmp_path, BASE))
    assert data.asset_name == "building_a"
    assert len(data.materials) == 1
    material = data.materials[0]
    assert material.channel("basecolor").is_texture
    assert material.channel("metallic").is_constant


def test_constant_normal_is_dropped(tmp_path):
    """エクスポータは Normal ソケットの既定値 (0,0,0) を定数として書く。
    ノーマルの定数には意味がないので落とす。"""
    data = mf.load(_write(tmp_path, BASE))
    assert data.materials[0].channel("normal") is None


def test_channel_aliases_are_normalised(tmp_path):
    payload = json.loads(json.dumps(BASE))
    payload["materials"][0]["channels"] = {
        "albedo": {"type": "texture", "path": "tex/a.png", "colorspace": "srgb"},
        "metalness": {"type": "constant", "value": 1.0},
    }
    data = mf.load(_write(tmp_path, payload))
    material = data.materials[0]
    assert material.channel("basecolor") is not None
    assert material.channel("metallic") is not None


def test_unknown_channel_is_recorded_not_dropped_silently(tmp_path):
    payload = json.loads(json.dumps(BASE))
    payload["materials"][0]["channels"]["sheen"] = {"type": "constant", "value": 1.0}
    data = mf.load(_write(tmp_path, payload))
    assert any("sheen" in issue for issue in data.issues)


def test_missing_geometry_is_an_issue_not_an_error(tmp_path):
    data = mf.load(_write(tmp_path, BASE, geometry=False))
    assert any("ジオメトリ" in issue for issue in data.issues)


def test_geometry_path_derived_from_manifest_filename(tmp_path):
    """``bldg_test_v002.bridge.json`` は同階層の ``bldg_test_v002.usdc`` を見る。

    manifest 内の geometry.path が古い（バージョンサフィックス無し）名前を
    指していても、マニフェスト自身のファイル名から正しく解決できること。
    """
    asset = tmp_path / "bldg_test_v002"
    asset.mkdir()
    (asset / "bldg_test_v002.usdc").write_bytes(b"stub")
    payload = json.loads(json.dumps(BASE))
    payload["asset"] = {"id": "bldg_test", "name": "bldg_test"}
    payload["geometry"]["path"] = "bldg_test.usdc"  # 古い名前のまま
    path = asset / "bldg_test_v002.bridge.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    data = mf.load(path)
    assert data.geometry_path.name == "bldg_test_v002.usdc"
    assert not data.issues  # フォールバックが要らないので issue も出ない


def test_geometry_path_falls_back_to_manifest_field_when_filename_missing(tmp_path):
    """ファイル名から解決したパスが無ければ geometry.path を試す（記録付き）。"""
    asset = tmp_path / "odd_name"
    asset.mkdir()
    (asset / "actual_geo.usdc").write_bytes(b"stub")
    payload = json.loads(json.dumps(BASE))
    payload["geometry"]["path"] = "actual_geo.usdc"
    path = asset / "odd_name.bridge.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    data = mf.load(path)
    assert data.geometry_path.name == "actual_geo.usdc"
    assert any("geometry.path" in issue for issue in data.issues)


def test_schema_major_mismatch_raises(tmp_path):
    payload = json.loads(json.dumps(BASE))
    payload["schema_version"] = "3.0.0"
    with pytest.raises(mf.ManifestError):
        mf.load(_write(tmp_path, payload))


def test_missing_file_raises(tmp_path):
    with pytest.raises(mf.ManifestError):
        mf.load(tmp_path / "nope.bridge.json")


def test_geometry_resolved_from_geo_subfolder(tmp_path):
    """blender_export_revision.md の新構成:
    publish/<version>/<asset>.json + publish/<version>/geo/<asset>.usdc"""
    version_dir = tmp_path / "building" / "publish" / "v001"
    version_dir.mkdir(parents=True)
    (version_dir / "geo").mkdir()
    (version_dir / "geo" / "building.usdc").write_bytes(b"stub")
    payload = json.loads(json.dumps(BASE))
    payload["asset"] = {"id": "building", "name": "building"}
    payload["geometry"]["path"] = "building.usdc"  # 見つからないはずの旧フィールド
    path = version_dir / "building.json"  # .bridge が付かない新しいファイル名
    path.write_text(json.dumps(payload), encoding="utf-8")

    data = mf.load(path)
    assert data.geometry_path == (version_dir / "geo" / "building.usdc").resolve()
    assert not data.issues


def test_geometry_resolution_prefers_geo_subfolder_over_colocated(tmp_path):
    """geo/ サブフォルダと同階層の両方に usdc があれば、geo/ を優先する。"""
    version_dir = tmp_path / "building" / "publish" / "v001"
    version_dir.mkdir(parents=True)
    (version_dir / "geo").mkdir()
    (version_dir / "geo" / "building.usdc").write_bytes(b"new")
    (version_dir / "building.usdc").write_bytes(b"old")
    path = version_dir / "building.json"
    payload = json.loads(json.dumps(BASE))
    payload["asset"] = {"id": "building", "name": "building"}
    path.write_text(json.dumps(payload), encoding="utf-8")

    data = mf.load(path)
    assert data.geometry_path == (version_dir / "geo" / "building.usdc").resolve()


# ------------------------------------------------------------------ library

def _make_publish_versions(root, asset, versions):
    for version in versions:
        (root / asset / "publish" / version).mkdir(parents=True, exist_ok=True)


def test_list_assets_requires_publish_folder(tmp_path):
    (tmp_path / "building" / "publish").mkdir(parents=True)
    (tmp_path / "not_an_asset").mkdir()
    assert library.list_assets(tmp_path) == ["building"]


def test_list_assets_missing_root_is_empty(tmp_path):
    assert library.list_assets(tmp_path / "nope") == []


def test_list_versions_sorted_newest_first(tmp_path):
    _make_publish_versions(tmp_path, "building", ["v001", "v003", "v002"])
    assert library.list_versions(tmp_path, "building") == ["v003", "v002", "v001"]


def test_list_versions_ignores_non_version_folders(tmp_path):
    _make_publish_versions(tmp_path, "building", ["v001"])
    (tmp_path / "building" / "publish" / "_wip").mkdir()
    assert library.list_versions(tmp_path, "building") == ["v001"]


def test_find_manifest_prefers_plain_json(tmp_path):
    version_dir = tmp_path / "building" / "publish" / "v001"
    version_dir.mkdir(parents=True)
    (version_dir / "building.bridge.json").write_text("{}", encoding="utf-8")
    (version_dir / "building.json").write_text("{}", encoding="utf-8")
    found = library.find_manifest(tmp_path, "building", "v001")
    assert found.name == "building.json"


def test_find_manifest_falls_back_to_lone_json(tmp_path):
    version_dir = tmp_path / "building" / "publish" / "v001"
    version_dir.mkdir(parents=True)
    (version_dir / "unexpected_name.json").write_text("{}", encoding="utf-8")
    found = library.find_manifest(tmp_path, "building", "v001")
    assert found.name == "unexpected_name.json"


def test_find_manifest_none_when_missing(tmp_path):
    assert library.find_manifest(tmp_path, "building", "v001") is None
