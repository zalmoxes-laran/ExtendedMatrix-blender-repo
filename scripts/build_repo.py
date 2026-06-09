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
                        This is what lets e.g. EM 1.5 and 1.6 coexist: a user
                        who adds /em/1.5/ only ever gets 1.5.x patches.

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
from urllib.parse import urlsplit


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
        entries, contents = [], []
        return {"title": title, "desc": desc, "path": path,
                "entries": entries, "contents": contents}

    # ---- Stable (latest): newest stable of every tool -------------------- #
    stable = channel(
        "Stable", "Ultima versione stabile di ogni tool. Per la maggior parte degli utenti.",
        "index.json")
    for st in per_tool.values():
        pick = newest(st["stable"])
        if pick:
            stable["entries"] += entries_for_release(st["tool"], pick, cfg, asset_re)
            stable["contents"].append((st["tool"]["name"], pick["tag_name"].lstrip("v")))
    channels.append(stable)

    # ---- Dev: newest prerelease of every tool ---------------------------- #
    dev = channel(
        "Dev", "Ultima prerelease di ogni tool. Bleeding edge, per tester.",
        "dev/index.json")
    for st in per_tool.values():
        pick = newest(st["dev"])
        if pick:
            dev["entries"] += entries_for_release(st["tool"], pick, cfg, asset_re)
            dev["contents"].append((st["tool"]["name"], pick["tag_name"].lstrip("v")))
    channels.append(dev)

    # ---- Pinned MAJOR.MINOR stable lines, per tool ----------------------- #
    for st in per_tool.values():
        tool = st["tool"]
        lines: dict = {}
        for r in st["stable"]:
            pv = parse_version(r["tag_name"])
            if pv is None:
                continue
            lines.setdefault(f"{pv[0]}.{pv[1]}", []).append(r)
        for line in sorted(lines, key=lambda k: tuple(map(int, k.split(".")))):
            pick = newest(lines[line])
            ch = channel(
                f"{tool['name']} — linea {line}",
                f"Solo le patch della linea {line}.x di {tool['name']} (resta fissata su questa minor).",
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
    site = cfg["site_url"].rstrip("/")

    def url(path: str) -> str:
        return f"{site}/{path}"

    def card(ch: dict) -> str:
        u = url(ch["path"])
        contents = "".join(
            f"<li>{html.escape(n)} <code>{html.escape(v)}</code></li>"
            for n, v in ch["contents"]
        ) or "<li><em>nessuna release disponibile per ora</em></li>"
        return f"""
      <section class="card">
        <h3>{html.escape(ch['title'])}</h3>
        <p class="desc">{html.escape(ch['desc'])}</p>
        <div class="url">
          <input readonly value="{html.escape(u)}" onclick="this.select()">
          <button onclick="navigator.clipboard.writeText('{html.escape(u)}')">Copia</button>
        </div>
        <p class="dd">oppure <a href="{html.escape(u)}">trascina questo link in Blender</a> (due volte: la prima aggiunge il repository, la seconda installa).</p>
        <ul class="contents">{contents}</ul>
      </section>"""

    stable_dev = [c for c in channels if c["path"] in ("index.json", "dev/index.json")]
    pinned = [c for c in channels if c not in stable_dev]
    pinned_html = ("<h2>Linee di versione fissate</h2><div class=\"grid\">"
                   + "".join(card(c) for c in pinned) + "</div>") if pinned else ""

    page = f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Extended Matrix — Blender Extensions</title>
<style>
  :root {{ --bg:#0f1419; --fg:#e6e6e6; --muted:#9aa4ad; --accent:#e8772e; --card:#1a212b; --line:#2a3340; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:16px/1.55 -apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--fg); }}
  header {{ padding:48px 24px 24px; border-bottom:1px solid var(--line); }}
  .wrap {{ max-width:920px; margin:0 auto; }}
  h1 {{ margin:0 0 6px; font-size:28px; }}
  h2 {{ margin:36px 0 14px; font-size:20px; }}
  .lead {{ color:var(--muted); margin:0; }}
  .steps {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 22px; margin:24px 0; }}
  .steps ol {{ margin:0; padding-left:20px; }}
  .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px; }}
  .card h3 {{ margin:0 0 6px; }}
  .desc {{ color:var(--muted); font-size:14px; margin:0 0 12px; min-height:38px; }}
  .url {{ display:flex; gap:8px; }}
  .url input {{ flex:1; min-width:0; background:#0b0f14; color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:8px; font-family:ui-monospace,monospace; font-size:12px; }}
  .url button {{ background:var(--accent); color:#1a1208; border:0; border-radius:6px; padding:8px 12px; font-weight:600; cursor:pointer; }}
  .dd {{ font-size:12.5px; color:var(--muted); margin:10px 0 8px; }}
  .contents {{ margin:8px 0 0; padding-left:18px; font-size:13px; color:var(--muted); }}
  code {{ background:#0b0f14; padding:1px 6px; border-radius:4px; font-size:12px; }}
  a {{ color:var(--accent); }}
  footer {{ color:var(--muted); font-size:13px; padding:32px 24px; border-top:1px solid var(--line); margin-top:40px; }}
</style>
</head>
<body>
<header><div class="wrap">
  <h1>Extended Matrix — Blender Extensions</h1>
  <p class="lead">Repository di estensioni per Blender 4.4+. Aggiungi un canale qui sotto e Blender terrà i tool aggiornati da solo.</p>
</div></header>
<main class="wrap" style="padding:0 24px;">
  <div class="steps">
    <strong>Come si installa (una volta per canale)</strong>
    <ol>
      <li>Copia l'URL del canale che vuoi.</li>
      <li>Blender → <em>Edit → Preferences → Get Extensions → Repositories → ＋ → Add Remote Repository</em>, incolla l'URL.</li>
      <li>Attiva <em>Check for Updates on Start</em> per gli aggiornamenti automatici.</li>
      <li>Trovi i tool nella lista estensioni: installa ed abilita.</li>
    </ol>
  </div>
  <h2>Canali principali</h2>
  <div class="grid">{''.join(card(c) for c in stable_dev)}</div>
  {pinned_html}
</main>
<footer><div class="wrap">
  Generato da <code>scripts/build_repo.py</code> dalle GitHub Releases dei tool ·
  <a href="https://www.extendedmatrix.org">extendedmatrix.org</a>
</div></footer>
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
