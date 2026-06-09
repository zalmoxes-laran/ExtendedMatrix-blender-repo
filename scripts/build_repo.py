#!/usr/bin/env python3
"""Generate the Blender extension repository served at blender.extendedmatrix.org.

For each configured tool we read its GitHub Releases and emit one Blender
``index.json`` per channel, plus a tiny root page. No archive is ever
downloaded: GitHub exposes a ``digest`` (``sha256:...``) and ``size`` for every
release asset, which map directly onto the ``archive_hash`` / ``archive_size``
fields Blender expects. So the whole repository is built from release metadata
alone, even though some zips are ~200 MB.

Why we rewrite ``platforms`` and ``blender_version_*`` per entry
----------------------------------------------------------------
Each tool ships 8 zips per release (4 platforms x 2 Python ABIs), but they all
embed the *same* manifest (``platforms = [all four]``, ``blender_version_min =
4.4.0``, no max). If we listed them as-is, Blender could not tell them apart.
So for every asset we derive the real platform + Blender range from the
filename (``<id>-v<version>-<platform>-blender5X.zip``) and narrow the entry
accordingly. Blender then downloads exactly the right zip for the user's OS and
Blender version, and auto-updates within the channel.

Channels (all auto-discovered from the releases that exist)
-----------------------------------------------------------
* stable (latest)    -> <out>/index.json            newest stable of each tool
* dev (prerelease)   -> <out>/dev/index.json         newest prerelease of each tool
* pinned line        -> <out>/<slug>/<maj>.<min>/index.json
                        newest patch of a stable MAJOR.MINOR line, per tool.
                        ONLY created when a tool has 2+ stable lines (otherwise
                        it would just duplicate the stable channel).

The human-facing install UI lives on the main site (extendedmatrix.org/download);
the subdomain root (index.html) just redirects there.

Usage::

    python3 scripts/build_repo.py --config repo_config.json --out dist
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# GitHub access (via the gh CLI; works locally and in Actions with GH_TOKEN)
# --------------------------------------------------------------------------- #
def gh_api(path: str):
    """Return parsed JSON from ``gh api <path>``."""
    proc = subprocess.run(
        ["gh", "api", path, "-H", "Accept: application/vnd.github+json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"`gh api {path}` failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def list_releases(repo: str):
    """All releases for a repo (newest first). 100 is plenty for these tools."""
    return gh_api(f"repos/{repo}/releases?per_page=100")


# --------------------------------------------------------------------------- #
# Version handling
# --------------------------------------------------------------------------- #
_VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.]?(.*))?$")


def parse_version(tag: str):
    """``(major, minor, patch, pre)`` or ``None`` if the tag is not parseable."""
    m = _VER_RE.match(tag.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")


def _pre_key(pre: str):
    """Prerelease ordering: a stable suffix ('') outranks any prerelease."""
    if not pre:
        return (1, 0)
    nums = re.findall(r"\d+", pre)
    return (0, int(nums[-1]) if nums else 0)


def version_key(tag: str):
    pv = parse_version(tag)
    if pv is None:
        return (-1, -1, -1, (0, 0))
    major, minor, patch, pre = pv
    return (major, minor, patch, _pre_key(pre))


def newest(releases: list):
    return max(releases, key=lambda r: version_key(r["tag_name"])) if releases else None


# --------------------------------------------------------------------------- #
# Index.json entry construction
# --------------------------------------------------------------------------- #
def make_asset_re(cfg) -> re.Pattern:
    ids = "|".join(re.escape(t["id"]) for t in cfg["tools"])
    plats = "|".join(re.escape(p) for p in cfg["platform_map"])
    btags = "|".join(re.escape(b) for b in cfg["blender_tag_ranges"])
    return re.compile(
        rf"^(?P<id>{ids})-v(?P<ver>.+?)-(?P<plat>{plats})-(?P<btag>{btags})\.zip$"
    )


def build_entry(tool: dict, asset: dict, plat_token: str, btag: str,
                version: str, cfg: dict) -> dict:
    """Assemble one Blender repository entry for a single platform zip."""
    rng = cfg["blender_tag_ranges"][btag]
    entry = {
        "schema_version": tool.get("schema_version", "1.0.0"),
        "id": tool["id"],
        "name": tool["name"],
        "tagline": tool["tagline"],
        "version": version,
        "type": tool.get("type", "add-on"),
        "maintainer": tool["maintainer"],
        "license": tool["license"],
        "blender_version_min": rng["min"],
        "platforms": [cfg["platform_map"][plat_token]],
        "tags": tool.get("tags", []),
        "permissions": tool.get("permissions", {}),
        # GitHub gives us everything we need without downloading the archive:
        "archive_url": asset["browser_download_url"],
        "archive_size": asset["size"],
        "archive_hash": asset["digest"],  # already in "sha256:<hex>" form
    }
    if rng.get("max"):
        entry["blender_version_max"] = rng["max"]
    if tool.get("website"):
        entry["website"] = tool["website"]
    return entry


def entries_for_release(tool: dict, release: dict, cfg: dict,
                        asset_re: re.Pattern) -> list:
    """Every per-platform entry contributed by one release of one tool."""
    out = []
    for asset in release.get("assets", []):
        m = asset_re.match(asset["name"])
        if not m or m.group("id") != tool["id"]:
            continue
        if not asset.get("digest"):
            print(f"  ! {asset['name']}: no sha256 digest from GitHub, skipping",
                  file=sys.stderr)
            continue
        out.append(build_entry(
            tool, asset, m.group("plat"), m.group("btag"), m.group("ver"), cfg))
    return out


def has_assets(tool: dict, release: dict, asset_re: re.Pattern) -> bool:
    return any(
        asset_re.match(a["name"]) and asset_re.match(a["name"]).group("id") == tool["id"]
        and a.get("digest")
        for a in release.get("assets", [])
    )


# --------------------------------------------------------------------------- #
# Channel discovery
# --------------------------------------------------------------------------- #
def discover_channels(cfg: dict, asset_re: re.Pattern) -> list:
    """Return a list of channel dicts: {title, desc, path, entries, contents}."""
    # Fetch + classify releases once per tool.
    per_tool = {}
    for tool in cfg["tools"]:
        rels = list_releases(tool["repo"])
        usable = [r for r in rels if has_assets(tool, r, asset_re)]
        per_tool[tool["id"]] = {
            "tool": tool,
            "stable": [r for r in usable if not r.get("prerelease")],
            "dev": [r for r in usable if r.get("prerelease")],
        }

    channels: list = []

    def channel(title, desc, path):
        return {"title": title, "desc": desc, "path": path,
                "entries": [], "contents": []}

    # ---- Stable (latest): newest stable of every tool -------------------- #
    stable = channel(
        "Stable", "Latest stable release of each tool. Recommended for most users.",
        "index.json")
    for st in per_tool.values():
        pick = newest(st["stable"])
        if pick:
            stable["entries"] += entries_for_release(st["tool"], pick, cfg, asset_re)
            stable["contents"].append((st["tool"]["name"], pick["tag_name"].lstrip("v")))
    channels.append(stable)

    # ---- Dev: newest prerelease of every tool ---------------------------- #
    dev = channel(
        "Dev", "Latest prerelease of each tool. Bleeding edge, for testers.",
        "dev/index.json")
    for st in per_tool.values():
        pick = newest(st["dev"])
        if pick:
            dev["entries"] += entries_for_release(st["tool"], pick, cfg, asset_re)
            dev["contents"].append((st["tool"]["name"], pick["tag_name"].lstrip("v")))
    channels.append(dev)

    # ---- Pinned MAJOR.MINOR stable lines, per tool ----------------------- #
    # Only meaningful once a tool has 2+ stable lines (so users can stay on an
    # older line after a newer one ships). With a single line it would just
    # duplicate "Stable", so we skip it entirely.
    for st in per_tool.values():
        tool = st["tool"]
        lines: dict = {}
        for r in st["stable"]:
            pv = parse_version(r["tag_name"])
            if pv is None:
                continue
            lines.setdefault(f"{pv[0]}.{pv[1]}", []).append(r)
        if len(lines) < 2:
            continue
        for line in sorted(lines, key=lambda k: tuple(map(int, k.split(".")))):
            pick = newest(lines[line])
            ch = channel(
                f"{tool['name']} — line {line}",
                f"Only the {line}.x patches of {tool['name']} (stays pinned to this minor).",
                f"{tool['slug']}/{line}/index.json")
            ch["entries"] = entries_for_release(tool, pick, cfg, asset_re)
            ch["contents"] = [(tool["name"], pick["tag_name"].lstrip("v"))]
            channels.append(ch)

    return channels


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_index(out: Path, rel_path: str, entries: list) -> None:
    target = out / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = {"version": "v1", "blocklist": [], "data": entries}
    target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def write_landing(out: Path, cfg: dict, channels: list) -> None:
    """Root index.html. The human-facing install UI now lives on the main site
    (extendedmatrix.org/download), so the subdomain root just redirects there.
    The index.json channels — what Blender actually consumes — are written
    separately by write_index() and are unaffected."""
    target = "https://extendedmatrix.org/download"
    site = cfg["site_url"].rstrip("/")
    page = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<meta http-equiv="refresh" content="0; url={target}">\n'
        f'<link rel="canonical" href="{target}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>Extended Matrix — Blender Extensions</title>\n'
        '<style>body{margin:0;min-height:100vh;display:flex;align-items:center;'
        'justify-content:center;text-align:center;padding:24px;background:#fff;color:#1f2328;'
        'font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}'
        'a{color:#1f6feb}code{background:#eff2f5;padding:1px 6px;border-radius:5px;font-size:13px}'
        '.m{color:#59636e;font-size:13px;margin-top:18px}</style>\n'
        '</head>\n<body>\n<div>\n'
        '<p>Extended Matrix — Blender extensions repository.</p>\n'
        f'<p>Redirecting to <a href="{target}">extendedmatrix.org/download</a> …</p>\n'
        '<p class="m">Repository URLs for Blender '
        '(Get Extensions → Repositories → Add Remote Repository):<br>\n'
        f'<code>{site}/index.json</code> (stable) · '
        f'<code>{site}/dev/index.json</code> (dev)</p>\n'
        '</div>\n'
        f'<script>location.replace({target!r})</script>\n'
        '</body>\n</html>\n'
    )
    (out / "index.html").write_text(page, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="repo_config.json")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--static", default="public",
                    help="dir whose contents are copied verbatim into the output")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # Copy static files (CNAME, etc.) verbatim if present.
    static = Path(args.static)
    if static.is_dir():
        for item in static.iterdir():
            dest = out / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

    # Disable Jekyll processing on GitHub Pages.
    (out / ".nojekyll").write_text("", encoding="utf-8")

    asset_re = make_asset_re(cfg)
    channels = discover_channels(cfg, asset_re)

    print(f"Channels ({len(channels)}):")
    for ch in channels:
        write_index(out, ch["path"], ch["entries"])
        contents = ", ".join(f"{n} {v}" for n, v in ch["contents"]) or "(empty)"
        print(f"  {ch['path']:<28} {len(ch['entries']):>2} entries  [{contents}]")

    write_landing(out, cfg, channels)
    print(f"\nWrote repository to {out}/  (root: redirect to /download)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
