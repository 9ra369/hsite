"""``<asset>.json``（旧 ``<asset>.bridge.json``）の読み込み。

エクスポータ側のスキーマがまだ固まっていない段階なので、ここは意図的に寛容にしてある。

- チャンネル名の揺れ（``albedo`` / ``basecolor``、``metalness`` / ``metallic``）を正規化する
- 未知のキーは無視する（落とさない）
- 欠けているキーは既定値で埋め、``issues`` に記録する

「読めなかった」ことを黙って握りつぶさず、必ず ``Manifest.issues`` に残すのが方針。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SUPPORTED_MAJOR = 2

# 正規化後のチャンネル名。Houdini 側はこれだけを見る。
CANONICAL = ("basecolor", "roughness", "metallic", "specular", "normal")

_CHANNEL_ALIASES = {
    "basecolor": "basecolor",
    "base_color": "basecolor",
    "albedo": "basecolor",
    "diffuse": "basecolor",
    "color": "basecolor",
    "roughness": "roughness",
    "rough": "roughness",
    "metallic": "metallic",
    "metalness": "metallic",
    "metal": "metallic",
    "specular": "specular",
    "spec": "specular",
    "normal": "normal",
    "norm": "normal",
}


class ManifestError(Exception):
    """読み込み自体が続行不能な場合。"""


# マニフェストのファイル名規則は現在 "<asset>.json"（blender_export_revision.md
# 以降。旧規則の "<asset>.bridge.json" も後方互換として読める）。
_STEM_SUFFIXES = (".bridge", ".manifest")


def _manifest_stem(path: Path) -> str:
    """``<asset>.json``（または旧 ``<asset>.bridge.json``）から
    ``<asset>`` 部分だけを取り出す。

    ``Path.stem`` は最後の拡張子（``.json``）しか外さないので、そのまま
    ``.usdc`` を付けると ``<asset>.bridge.usdc`` のような誤ったファイル名に
    なってしまう。``.bridge`` / ``.manifest`` のような中間サフィックスも
    明示的に落とす。
    """
    stem = path.stem  # "<asset>.bridge.json" -> "<asset>.bridge"
    for suffix in _STEM_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem


@dataclass
class Channel:
    kind: str                      # "texture" | "constant"
    path: str = ""                 # asset dir からの相対 POSIX パス
    colorspace: str = "raw"        # "srgb" | "raw"
    value: Any = None              # constant のとき float または [r, g, b]
    component: str = ""            # グレースケール取り出し元 "r"/"g"/"b"
    space: str = "tangent"         # normal のみ
    encoding: str = "opengl"       # normal のみ
    strength: float = 1.0          # normal のみ

    @property
    def is_texture(self) -> bool:
        return self.kind == "texture" and bool(self.path)

    @property
    def is_constant(self) -> bool:
        return self.kind == "constant" and self.value is not None


@dataclass
class Material:
    ident: str
    source_name: str               # Blender の原文の名前
    safe_name: str                 # Houdini のノード名に使う
    channels: Dict[str, Channel] = field(default_factory=dict)

    def channel(self, name: str) -> Optional[Channel]:
        return self.channels.get(name)


@dataclass
class ObjectEntry:
    ident: str
    source_name: str


@dataclass
class Manifest:
    path: Path                     # マニフェストファイル自身の絶対パス
    root: Path                     # アセットディレクトリ（マニフェストのある場所）
    schema_version: str
    asset_name: str
    asset_id: str
    geometry_path: Path            # 絶対パスに解決済み
    geometry_format: str
    material_id_attribute: str
    objects: List[ObjectEntry]
    materials: List[Material]
    assignments: List[Dict[str, str]]
    exporter_warnings: List[str]
    issues: List[str]              # 読み込み時にこちらで気づいた問題

    def texture_path(self, channel: Channel) -> Path:
        return (self.root / channel.path).resolve()

    def materials_for_object(self, object_id: str) -> List[Material]:
        wanted = {a.get("material_id") for a in self.assignments
                  if a.get("object_id") == object_id}
        return [m for m in self.materials if m.ident in wanted]


# ------------------------------------------------------------------ parsing

def _as_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_channel(name: str, raw: dict, material_name: str, issues: List[str]) -> Optional[Channel]:
    kind = raw.get("type")
    if kind == "texture":
        path = raw.get("path") or ""
        if not path:
            issues.append(f"{material_name}.{name}: type=texture ですが path が空です")
            return None
        return Channel(
            kind="texture",
            path=path,
            colorspace=(raw.get("colorspace") or "raw").lower(),
            component=(raw.get("channel") or "").lower(),
            space=(raw.get("space") or "tangent").lower(),
            encoding=(raw.get("encoding") or "opengl").lower(),
            strength=_as_float(raw.get("strength"), 1.0),
        )

    if kind == "constant":
        value = raw.get("value")
        # エクスポータは Normal ソケットの既定値 (0,0,0) をそのまま定数として書く。
        # ノーマルの定数には意味がないので、無いものとして扱う。
        if name == "normal":
            return None
        if value is None:
            issues.append(f"{material_name}.{name}: type=constant ですが value がありません")
            return None
        return Channel(kind="constant", value=value)

    issues.append(f"{material_name}.{name}: 未知の type '{kind}' を無視しました")
    return None


def _parse_material(raw: dict, index: int, issues: List[str]) -> Material:
    source_name = raw.get("source_name") or raw.get("name") or f"material_{index}"
    safe = raw.get("safe_name") or ""
    if not safe:
        from .naming import sanitize
        safe = sanitize(source_name)
        issues.append(f"{source_name}: safe_name が無いのでこちらで生成しました ({safe})")

    channels: Dict[str, Channel] = {}
    for key, value in (raw.get("channels") or {}).items():
        canonical = _CHANNEL_ALIASES.get(str(key).lower())
        if canonical is None:
            issues.append(f"{source_name}: 未知のチャンネル '{key}' を無視しました")
            continue
        if not isinstance(value, dict):
            issues.append(f"{source_name}.{key}: 形式が不正です")
            continue
        parsed = _parse_channel(canonical, value, source_name, issues)
        if parsed is not None:
            channels[canonical] = parsed

    return Material(
        ident=raw.get("id") or f"mat_{index:04d}",
        source_name=source_name,
        safe_name=safe,
        channels=channels,
    )


def load(manifest_path) -> Manifest:
    path = Path(manifest_path).expanduser()
    if not path.is_file():
        raise ManifestError(f"マニフェストが見つかりません: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"マニフェストを読めません: {exc}") from exc

    issues: List[str] = []

    version = str(data.get("schema_version") or "0")
    try:
        major = int(version.split(".")[0])
    except ValueError:
        major = -1
    if major != SUPPORTED_MAJOR:
        raise ManifestError(
            f"スキーマのメジャーバージョンが合いません "
            f"(マニフェスト {version} / このプラグインは {SUPPORTED_MAJOR}.x 対応)"
        )

    root = path.parent
    manifest_stem = _manifest_stem(path)
    asset = data.get("asset") or {}
    asset_name = asset.get("name") or manifest_stem
    asset_id = asset.get("id") or asset_name

    geometry = data.get("geometry") or {}
    # ジオメトリの解決は、マニフェスト自身のファイル名を正とする
    # （asset.name は Blender 側で古いまま＝バージョンサフィックス無しの
    # ことがあるため使わない）。
    #
    # blender_export_revision.md 以降の構成: publish/<version>/ 直下に
    # マニフェスト、その "geo" サブフォルダに usdc がある
    #   publish/v001/bldg_test.json
    #   publish/v001/geo/bldg_test.usdc
    # 旧構成（マニフェストと同階層に usdc）も後方互換で試す。
    # それでも無ければ geometry.path（マニフェスト内の申告）を試す。
    candidates = [
        (root / "geo" / f"{manifest_stem}.usdc"),  # 新構成
        (root / f"{manifest_stem}.usdc"),          # 旧構成
    ]
    geo_abs = None
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            geo_abs = resolved
            break

    if geo_abs is None:
        fallback_rel = geometry.get("path")
        if fallback_rel:
            fallback_abs = (root / fallback_rel).resolve()
            if fallback_abs.is_file():
                issues.append(
                    f"{candidates[0].name} が見つからないため、"
                    f"manifest の geometry.path ({fallback_rel}) を使いました"
                )
                geo_abs = fallback_abs

    if geo_abs is None:
        geo_abs = candidates[0].resolve()
        issues.append(f"ジオメトリファイルが見つかりません: {geo_abs}")

    materials = [_parse_material(m, i, issues)
                 for i, m in enumerate(data.get("materials") or [])]
    if not materials:
        issues.append("マニフェストにマテリアルが 1 つもありません")

    objects = [ObjectEntry(ident=o.get("id") or f"obj_{i:04d}",
                           source_name=o.get("source_name") or f"object_{i}")
               for i, o in enumerate(data.get("objects") or [])]

    return Manifest(
        path=path.resolve(),
        root=root,
        schema_version=version,
        asset_name=asset_name,
        asset_id=asset_id,
        geometry_path=geo_abs,
        geometry_format=(geometry.get("format") or "usd").lower(),
        material_id_attribute=geometry.get("material_id_attribute") or "",
        objects=objects,
        materials=materials,
        assignments=list(data.get("assignments") or []),
        exporter_warnings=list(data.get("warnings") or []),
        issues=issues,
    )
