#!/usr/bin/env python3
"""Generate the Blender extension repository served at blender.extendedmatrix.org.

For each configured tool we read its GitHub Releases and emit one Blender
``index.json`` per channel, plus a static landing page. No archive is ever
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

Usage::

    python3 scripts/build_repo.py --config repo_config.json --out dist
"""
from __future__ import annotations

import argparse
import html
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


# Material "open_with" glyph — the canonical 4-arrow "move/drag" handle.
_MOVE_ICON = (
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
    '<path d="M10 9h4V6h3l-5-5-5 5h3v3zm-1 1H6V7l-5 5 5 5v-3h3v-4zm14 2l-5-5v3'
    'h-3v4h3v3l5-5zm-9 3h-4v3H7l5 5 5-5h-3v-3z"/></svg>'
)


def write_landing(out: Path, cfg: dict, channels: list) -> None:
    site = cfg["site_url"].rstrip("/")

    def url(path: str) -> str:
        return f"{site}/{path}"

    def block(ch: dict) -> str:
        u = url(ch["path"])
        includes = " · ".join(
            f"{html.escape(n)} <code>{html.escape(v)}</code>" for n, v in ch["contents"]
        ) or '<span class="muted">no release yet</span>'
        return f"""
        <article class="channel">
          <h3>{html.escape(ch['title'])}</h3>
          <p class="desc">{html.escape(ch['desc'])}</p>
          <a class="dnd" href="{html.escape(u)}" title="Drag this into the Blender window">{_MOVE_ICON}<span>Drag &amp; Drop into Blender</span></a>
          <p class="or">…or copy the repository URL</p>
          <div class="url">
            <input readonly value="{html.escape(u)}" onclick="this.select()">
            <button type="button" onclick="navigator.clipboard.writeText('{html.escape(u)}')">Copy</button>
          </div>
          <p class="includes">Includes: {includes}</p>
        </article>"""

    main_ch = [c for c in channels if c["path"] in ("index.json", "dev/index.json")]
    pinned = [c for c in channels if c not in main_ch]
    pinned_section = ""
    if pinned:
        pinned_section = f"""
  <div class="dots">• • •</div>
  <section>
    <h2>Pin to a version line <span class="muted" style="font-weight:400">— advanced</span></h2>
    <p class="sub">Optional. Add one of these only to <strong>stay on a specific version line</strong> — e.g. keep a tool on <code>1.5.x</code> after <code>1.6</code> is released. For normal use, add <em>Stable</em> above instead.</p>
    <div class="channels">{''.join(block(c) for c in pinned)}</div>
  </section>"""

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Extended Matrix — Blender Extensions</title>
<style>
  :root {{ --fg:#1f2328; --muted:#59636e; --faint:#d0d7de; --accent:#1f6feb; --accent-bg:#ddebff; --code:#eff2f5; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#fff; color:var(--fg);
         font:17px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         -webkit-font-smoothing:antialiased; }}
  main {{ max-width:760px; margin:0 auto; padding:72px 24px 96px; }}
  h1 {{ font-size:34px; font-weight:700; letter-spacing:-.021em; margin:0 0 14px; }}
  .lead {{ font-size:19px; color:var(--muted); margin:0; max-width:60ch; }}
  h2 {{ font-size:21px; font-weight:600; margin:0 0 10px; letter-spacing:-.01em; }}
  .sub {{ color:var(--muted); margin:0 0 8px; max-width:64ch; }}
  .dots {{ text-align:center; color:var(--faint); letter-spacing:.7em; margin:52px 0; font-size:13px; }}
  ol {{ color:#3b424b; padding-left:22px; margin:14px 0 0; }}
  ol li {{ margin:7px 0; }}
  .note {{ color:var(--muted); font-size:14.5px; margin:16px 0 0; }}
  .channels {{ display:grid; gap:30px; grid-template-columns:1fr 1fr; margin-top:26px; }}
  .channel h3 {{ font-size:18px; margin:0 0 4px; }}
  .desc {{ color:var(--muted); font-size:15px; margin:0 0 18px; min-height:44px; }}
  .dnd {{ display:inline-flex; align-items:center; gap:9px; text-decoration:none;
          color:var(--accent); fill:var(--accent); font-weight:600; font-size:15px;
          border:2px dashed #9cc4ff; border-radius:11px; padding:13px 18px; background:#f4f9ff;
          transition:background .12s,border-color .12s; }}
  .dnd:hover {{ background:var(--accent-bg); border-color:var(--accent); }}
  .or {{ color:var(--muted); font-size:13px; margin:16px 0 7px; }}
  .url {{ display:flex; gap:8px; }}
  .url input {{ flex:1; min-width:0; font:12.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
                padding:10px; border:1px solid var(--faint); border-radius:8px; background:#f6f8fa; color:var(--fg); }}
  .url button {{ border:1px solid var(--faint); background:#f6f8fa; border-radius:8px; padding:10px 15px;
                 cursor:pointer; font-weight:600; font-size:13px; color:var(--fg); }}
  .url button:hover {{ background:#eceff2; }}
  .includes {{ color:var(--muted); font-size:13px; margin:16px 0 0; }}
  .muted {{ color:var(--muted); }}
  code {{ background:var(--code); padding:1px 6px; border-radius:5px; font-size:12.5px; }}
  a {{ color:var(--accent); }}
  footer {{ color:#8c959f; font-size:13px; margin-top:72px; text-align:center; }}
  @media (max-width:600px) {{ .channels {{ grid-template-columns:1fr; }} main {{ padding:48px 20px 72px; }} }}
</style>
</head>
<body>
<main>
  <header>
    <h1>Extended Matrix — Blender Extensions</h1>
    <p class="lead">One-click install and automatic updates for the Extended Matrix Blender tools. Add a channel in Blender 4.4+ and it stays up to date.</p>
  </header>

  <div class="dots">• • •</div>

  <section>
    <h2>How it works</h2>
    <p class="sub">Each channel is a <strong>single repository</strong> you add to Blender <strong>once</strong>. It bundles <strong>both</strong> tools — <strong>EM Tools</strong> and <strong>3D Survey Collection</strong> — and Blender keeps them updated for you.</p>
    <ol>
      <li>Drag the <em>Drag &amp; Drop into Blender</em> handle below into the Blender window — or copy the channel URL.</li>
      <li>If you copied it: <em>Edit ▸ Preferences ▸ Get Extensions ▸ Repositories ▸ + ▸ Add Remote Repository</em>, then paste.</li>
      <li>Keep <em>Check for Updates on Start</em> enabled for automatic updates.</li>
      <li>Find the tools in the extensions list, install and enable.</li>
    </ol>
    <p class="note">Using drag &amp; drop you’ll do it <strong>twice</strong>: the first adds the repository, the second installs.</p>
  </section>

  <div class="dots">• • •</div>

  <section>
    <h2>Choose a channel</h2>
    <p class="sub">Add just <strong>one</strong>: <strong>Stable</strong> for everyday use, or <strong>Dev</strong> to try prereleases.</p>
    <div class="channels">{''.join(block(c) for c in main_ch)}</div>
  </section>
{pinned_section}
  <footer>Generated from the tools’ GitHub Releases · <a href="https://www.extendedmatrix.org">extendedmatrix.org</a></footer>
</main>
</body>
</html>
"""
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
    print(f"\nWrote repository to {out}/  (landing: {out}/index.html)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
