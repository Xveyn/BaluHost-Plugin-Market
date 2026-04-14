"""Build the marketplace index and `.bhplugin` artifacts.

Walks every directory under ``plugins/``, validates its ``plugin.json``,
zips the plugin source into ``dist/<name>-<version>.bhplugin``, computes a
SHA-256 checksum, and writes ``dist/index.json`` in the format the
BaluHost backend expects.

Runs in two modes:

- ``--check-only``: validate every plugin, exit non-zero on error. No
  artifacts written. Used by the PR validation workflow.
- (default): validate + build. Writes ``dist/index.json`` and
  ``dist/<name>-<version>.bhplugin`` for every plugin. Used by the
  publish workflow.

The validation rules mirror ``backend/app/plugins/sdk/validator.py`` and
``backend/app/plugins/resolver.py`` in the BaluHost repo. The duplication
is deliberate: this script must run in CI without pulling the full
BaluHost backend. When ``baluhost-sdk`` is published to PyPI this file
can shrink to a thin wrapper around the real validator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from packaging.requirements import InvalidRequirement, Requirement


SUPPORTED_MANIFEST_VERSIONS = {1}

REQUIRED_MANIFEST_FIELDS = (
    "manifest_version",
    "name",
    "version",
    "display_name",
    "description",
    "author",
)

C_EXTENSION_BLACKLIST = frozenset(
    {
        "numpy",
        "pandas",
        "scipy",
        "pillow",
        "lxml",
        "psycopg2",
        "mysqlclient",
        "grpcio",
        "cryptography",
        "bcrypt",
        "cffi",
        "orjson",
        "ujson",
    }
)


@dataclass
class PluginIssue:
    level: str  # "error" | "warning"
    plugin: str
    code: str
    message: str


@dataclass
class BuildContext:
    repo_root: Path
    plugins_dir: Path
    dist_dir: Path
    download_base_url: str
    check_only: bool = False
    issues: List[PluginIssue] = field(default_factory=list)
    index_entries: list = field(default_factory=list)

    def err(self, plugin: str, code: str, message: str) -> None:
        self.issues.append(PluginIssue("error", plugin, code, message))

    def warn(self, plugin: str, code: str, message: str) -> None:
        self.issues.append(PluginIssue("warning", plugin, code, message))

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)


def validate_manifest(ctx: BuildContext, plugin_dir: Path) -> Optional[dict]:
    name = plugin_dir.name
    manifest_path = plugin_dir / "plugin.json"

    if not manifest_path.exists():
        ctx.err(name, "manifest_missing", f"{plugin_dir}/plugin.json not found")
        return None

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        ctx.err(name, "manifest_invalid_json", f"plugin.json is not valid JSON: {exc}")
        return None

    if not isinstance(manifest, dict):
        ctx.err(name, "manifest_invalid", "plugin.json must be a JSON object")
        return None

    for field_name in REQUIRED_MANIFEST_FIELDS:
        if field_name not in manifest:
            ctx.err(name, "manifest_field_missing", f"required field '{field_name}' missing")

    if ctx.has_errors and any(
        i.plugin == name and i.code == "manifest_field_missing" for i in ctx.issues
    ):
        return None

    mv = manifest.get("manifest_version")
    if mv not in SUPPORTED_MANIFEST_VERSIONS:
        ctx.err(
            name,
            "manifest_version_unsupported",
            f"manifest_version {mv!r} not in {sorted(SUPPORTED_MANIFEST_VERSIONS)}",
        )
        return None

    if manifest["name"] != name:
        ctx.err(
            name,
            "manifest_name_mismatch",
            f"manifest name '{manifest['name']}' does not match folder name '{name}'",
        )

    if not manifest.get("min_baluhost_version"):
        ctx.err(
            name,
            "min_baluhost_version_missing",
            "min_baluhost_version must be set so the marketplace can gate installs",
        )

    for raw in manifest.get("python_requirements", []):
        try:
            req = Requirement(raw)
        except InvalidRequirement as exc:
            ctx.err(
                name,
                "requirement_invalid",
                f"'{raw}' is not a valid PEP 508 requirement: {exc}",
            )
            continue

        if req.name.lower() in C_EXTENSION_BLACKLIST:
            ctx.warn(
                name,
                "requirement_c_extension",
                f"'{req.name}' ships C extensions and will not install into an "
                "isolated plugin environment. If BaluHost Core already provides "
                "this package, it will be reclassified as shared at install time; "
                "otherwise choose a pure-Python alternative.",
            )

    entrypoint = manifest.get("entrypoint", "__init__.py")
    if not (plugin_dir / entrypoint).exists():
        ctx.err(
            name,
            "entrypoint_missing",
            f"entrypoint '{entrypoint}' does not exist in {plugin_dir}",
        )

    ui = manifest.get("ui")
    if isinstance(ui, dict):
        bundle = ui.get("bundle")
        if bundle and not (plugin_dir / bundle).exists():
            ctx.err(name, "ui_bundle_missing", f"ui.bundle '{bundle}' does not exist")

    return manifest


def _iter_plugin_files(plugin_dir: Path) -> Iterable[Path]:
    for path in sorted(plugin_dir.rglob("*")):
        if path.is_dir():
            continue
        # site-packages/ is generated at install time, never shipped in the .bhplugin.
        if "site-packages" in path.relative_to(plugin_dir).parts:
            continue
        # Ignore editor/OS cruft.
        if "__pycache__" in path.parts:
            continue
        yield path


def build_bhplugin(plugin_dir: Path, out_path: Path) -> tuple[str, int]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in _iter_plugin_files(plugin_dir):
            arcname = str(Path(plugin_dir.name) / file_path.relative_to(plugin_dir))
            zf.write(file_path, arcname)

    data = out_path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def build_plugin_entry(
    ctx: BuildContext, plugin_dir: Path, manifest: dict
) -> Optional[dict]:
    name = manifest["name"]
    version = manifest["version"]

    if ctx.check_only:
        version_entry = {
            "version": version,
            "min_baluhost_version": manifest.get("min_baluhost_version"),
            "max_baluhost_version": manifest.get("max_baluhost_version"),
            "python_requirements": manifest.get("python_requirements", []),
            "required_permissions": manifest.get("required_permissions", []),
            "download_url": None,
            "checksum_sha256": None,
            "size_bytes": None,
            "released_at": None,
        }
    else:
        bhplugin_name = f"{name}-{version}.bhplugin"
        out_path = ctx.dist_dir / bhplugin_name
        checksum, size = build_bhplugin(plugin_dir, out_path)
        version_entry = {
            "version": version,
            "min_baluhost_version": manifest.get("min_baluhost_version"),
            "max_baluhost_version": manifest.get("max_baluhost_version"),
            "python_requirements": manifest.get("python_requirements", []),
            "required_permissions": manifest.get("required_permissions", []),
            "download_url": f"{ctx.download_base_url.rstrip('/')}/{bhplugin_name}",
            "checksum_sha256": checksum,
            "size_bytes": size,
            "released_at": datetime.now(timezone.utc).isoformat(),
        }

    return {
        "name": name,
        "latest_version": version,
        "versions": [version_entry],
        "display_name": manifest.get("display_name", name),
        "description": manifest.get("description", ""),
        "author": manifest.get("author", ""),
        "homepage": manifest.get("homepage"),
        "category": manifest.get("category", "general"),
    }


def build_index(ctx: BuildContext) -> dict:
    return {
        "index_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plugins": ctx.index_entries,
    }


def run(ctx: BuildContext) -> int:
    if not ctx.plugins_dir.exists():
        print(f"error: plugins directory not found: {ctx.plugins_dir}", file=sys.stderr)
        return 1

    plugin_dirs = sorted(
        p for p in ctx.plugins_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    )

    for plugin_dir in plugin_dirs:
        manifest = validate_manifest(ctx, plugin_dir)
        if manifest is None:
            continue
        entry = build_plugin_entry(ctx, plugin_dir, manifest)
        if entry is not None:
            ctx.index_entries.append(entry)

    for issue in ctx.issues:
        prefix = "ERROR" if issue.level == "error" else "warning"
        print(f"{prefix} [{issue.plugin}] {issue.code}: {issue.message}", file=sys.stderr)

    if ctx.has_errors:
        print(
            f"\n{sum(1 for i in ctx.issues if i.level == 'error')} error(s) found.",
            file=sys.stderr,
        )
        return 1

    if ctx.check_only:
        print(f"ok: {len(plugin_dirs)} plugin(s) validated.")
        return 0

    ctx.dist_dir.mkdir(parents=True, exist_ok=True)
    index = build_index(ctx)
    index_path = ctx.dist_dir / "index.json"
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    print(
        f"ok: wrote {index_path} and {len(plugin_dirs)} .bhplugin archive(s) "
        f"to {ctx.dist_dir}"
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plugins-dir",
        type=Path,
        default=Path("plugins"),
        help="Directory containing plugin folders (default: plugins)",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=Path("dist"),
        help="Directory to write index.json and .bhplugin files (default: dist)",
    )
    parser.add_argument(
        "--download-base-url",
        default="https://xveyn.github.io/BaluHost-Plugin-Market",
        help="Base URL where the dist directory will be published",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate manifests only, do not write artifacts",
    )
    args = parser.parse_args(argv)

    ctx = BuildContext(
        repo_root=Path.cwd(),
        plugins_dir=args.plugins_dir,
        dist_dir=args.dist_dir,
        download_base_url=args.download_base_url,
        check_only=args.check_only,
    )
    return run(ctx)


if __name__ == "__main__":
    raise SystemExit(main())
