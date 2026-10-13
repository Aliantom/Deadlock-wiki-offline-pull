# Deadlock Wiki — Offline Mirror

A copy of [deadlock.wiki](https://deadlock.wiki/) you can browse where the
wiki is blocked, styled like the real site and at the same URLs
(`/Abrams`, `/Items`, `/Update:...`). Built the same way as
[Dustloop-offline-pull](https://github.com/Aliantom/Dustloop-offline-pull).

Three parts:

1. **`deadlock_mirror.py`** downloads the wiki (pages, stylesheets, images,
   fonts, videos) and rewrites links so everything loads from the copy. Runs
   only re-download pages whose revision changed.
2. **The `mirror-data` release** on this repo stores the copy as split tar
   files (`--publish` uploads them).
3. **A codespace** pulls the newest copy on start and serves it on port 8000
   with `deadlock_mirror/server.py`.

## Where the downloading runs

deadlock.wiki is behind a Cloudflare bot check that blocks GitHub's servers
(every request from GitHub Actions gets a "Just a moment..." page), so unlike
Dustloop, GitHub can't download it. The downloader runs on any machine that
*can* reach the wiki, normally your home PC or home server, and uploads the
result. Nothing changes on the work side.

Test any machine in a few seconds:

```bash
python deadlock_mirror.py --check
```

The `Mirror deadlock.wiki` workflow runs this check on GitHub daily. If
GitHub is ever let through (say the wiki's admins allow this mirror), that
workflow takes over the downloading and uploading by itself.

## Setting up the home machine (once)

Needs **Python 3.8+**, the **GitHub CLI** ([cli.github.com](https://cli.github.com)),
and optionally **yt-dlp** for embedded YouTube videos (`pip install yt-dlp`).

```bash
git clone https://github.com/Aliantom/Deadlock-wiki-offline-pull
cd Deadlock-wiki-offline-pull
gh auth login                          # once, so it can upload to the release
python deadlock_mirror.py --check      # should say OK
python deadlock_mirror.py --publish    # first run: a few hours, then uploads
```

The first run takes a while on purpose: it waits about a second between
requests to be polite to the wiki. It saves progress after every page, so
stopping it (Ctrl+C, reboot) is fine; the next run continues. After that,
daily runs only fetch what changed and take minutes.

### Run it daily

**Windows (Task Scheduler):** Create Basic Task → Daily → Start a program:
- Program: `python` (or the full path to `python.exe`)
- Arguments: `deadlock_mirror.py --publish`
- Start in: the folder you cloned into

**Linux / macOS (cron):** `crontab -e`, then:

```
0 4 * * * cd /path/to/Deadlock-wiki-offline-pull && python3 deadlock_mirror.py --publish >> ~/deadlock_mirror/cron.log 2>&1
```

It uploads only when something new was downloaded.

## Reading it at work

Open a codespace on this repo. The server starts on port 8000 by itself and
the newest copy is pulled in the background (log: `/tmp/deadlock-refresh.log`).

- Home: `/` (the wiki's Main Page). Last run report: `/_status`.
- The search box works offline (titles and page text).
- `bash .devcontainer/refresh.sh --force` pulls the newest copy now.
- If the page shows a 502: `bash .devcontainer/start.sh`.

What works without the live site: styling, images, tabs, collapsible
sections, sortable tables, redirects, `File:` links (open the image), and
downloaded videos. The wiki's own scripts are removed (they call the live
site); the server adds a small script for tabs, collapsibles and sorting.

## What's mirrored

Set at the top of `deadlock_mirror.py`:

| Setting | Default | |
|---|---|---|
| `NAMESPACES` | `[0, 3000, 14]` | Articles, `Update:` patch notes, categories |
| `SKIP_TRANSLATIONS` | `True` | Skip translated subpages like `Abrams/de` |
| `PRIORITY_TITLES` | Main Page | Fetched first |
| `DEADLOCK_OUTPUT` (env) | `~/deadlock_mirror` | Where the copy goes |
| `DEADLOCK_WAIT` (env) | `1` | Seconds between requests. **Don't lower this.** |

Other options: `--only Abrams Haze`, `--refetch-all`, `--no-videos`,
`--budget-minutes N`, `--publish-only` (upload what's on disk).

Every run audits each page: a page counts as fully rendered only when every
stylesheet, image, font and video it references is on disk. Pages that fail
are re-queued. `status.md` (and `/_status`) shows the result.

## Etiquette

The wiki is community-run. This is for personal offline reading: please
don't redistribute the copy or run it more than once a day, and keep the
one-second wait. Downloading from YouTube is against its terms of service;
skip it with `--no-videos` if you'd rather.
