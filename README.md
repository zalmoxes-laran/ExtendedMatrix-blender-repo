# ExtendedMatrix-blender-repo

Self-hosted **Blender extension repository** for the Extended Matrix tools,
served at **https://blender.extendedmatrix.org**.

Add one of its channel URLs in Blender (*Preferences → Get Extensions →
Repositories → Add Remote Repository*) and Blender installs **EM Tools** and
**3D Survey Collection** and keeps them **updated automatically** — the same
mechanism the official extensions platform uses, but self-hosted.

## How it works

`scripts/build_repo.py` reads the **GitHub Releases** of the tool repos and
emits one Blender `index.json` per channel + a landing page into `dist/`.
A GitHub Action publishes `dist/` to GitHub Pages.

* **No archives are downloaded.** GitHub exposes a sha256 `digest` and `size`
  for every release asset; these become `archive_hash` / `archive_size`.
  `archive_url` points straight at the release asset, so the Pages site stays
  tiny while the (≈200 MB) zips are served by GitHub Releases.
* **Per-entry narrowing.** Every tool ships 8 zips per release
  (`<id>-v<version>-<platform>-blender5X.zip`: 4 platforms × Python 3.11/3.13),
  but they embed the same manifest. The generator derives the real `platforms`
  and `blender_version_min/max` from each filename so Blender downloads exactly
  the right zip per OS / Blender version. The mapping lives in
  [`repo_config.json`](repo_config.json) (`blender_tag_ranges`, `platform_map`).

## Channels

| URL | Contents | Updates |
|-----|----------|---------|
| `…/index.json` | **Stable** — newest stable of every tool | jumps lines (1.5 → 1.6) |
| `…/dev/index.json` | **Dev** — newest prerelease of every tool | bleeding edge |
| `…/<slug>/<major>.<minor>/index.json` | **Pinned line** — newest patch of one stable minor line, per tool | stays on that line |

`<slug>` is `em` (EM Tools) or `dsc` (3D Survey Collection). Pinned lines are
per-tool because the two tools version independently.

> **Coexisting versions.** To stay on, say, EM 1.5 while 1.6 is out, add **only**
> `…/em/1.5/index.json` (not the combined `…/index.json`). A user who adds both
> the pinned line *and* the stable channel will be offered the higher version.

Channels are **auto-discovered** from whatever releases exist — tag a new
stable `1.6.0` and `…/em/1.6/` appears on the next build with no code change.

## Maintaining

* **Add a tool / change metadata:** edit [`repo_config.json`](repo_config.json).
* **Blender's version→Python boundary moved?** edit `blender_tag_ranges` there.
* **Rebuild locally:** `python3 scripts/build_repo.py --out dist` (needs the
  `gh` CLI authenticated; reads public releases). Open `dist/index.html`.

## One-time setup (go-live)

1. **Pages:** Settings → Pages → Source = **GitHub Actions**.
2. **DNS:** add a `CNAME` record `blender` → `zalmoxes-laran.github.io` at the
   `extendedmatrix.org` DNS provider (same pattern as `dev.extendedmatrix.org`).
   GitHub Pages → custom domain `blender.extendedmatrix.org` (the `public/CNAME`
   file already pins it); enable *Enforce HTTPS* once the cert is issued.
3. The repo rebuilds **nightly**, on **push**, and on manual **Run workflow**.

### Optional: instant rebuilds on a new tool release

Add a step to each tool's `release.yml` so a new release pings this repo:

```yaml
      - name: Notify extension repository
        run: gh api repos/zalmoxes-laran/ExtendedMatrix-blender-repo/dispatches \
               -f event_type=tool-released
        env:
          GH_TOKEN: ${{ secrets.REPO_DISPATCH_TOKEN }}   # PAT with repo scope
```

Without it, a new release is still picked up by the nightly build.
