#!/usr/bin/env python3
"""
Deadlock Wiki Mirror
====================
Downloads deadlock.wiki for offline reading, styled like the live site.
Same design as the Dustloop mirror (Aliantom/Dustloop-offline-pull).

deadlock.wiki is behind a Cloudflare bot check that blocks GitHub's servers,
so this runs on your own machine (home PC / home server) and, with --publish,
uploads the result to this repo's "mirror-data" release. A codespace at work
then downloads it and serves it (see .devcontainer/ and deadlock_mirror/server.py).

How it works:

  1. Ask the wiki's API for every page in the mirrored namespaces (articles,
     Update: patch notes, categories), with each page's latest revision id,
     plus the list of redirects. Translated subpages ("Abrams/de") are skipped.
  2. Download each page that is new or has changed since the last run, plus
     every stylesheet, image, font and video it needs.
  3. Rewrite links as each page is saved, so it renders correctly even if
     the run is cut off partway through.
  4. Save progress after every page, so a stopped run resumes next time.

Usage:
    python deadlock_mirror.py --check            # can this machine reach the wiki?
    python deadlock_mirror.py                    # download / update
    python deadlock_mirror.py --publish          # ...then upload to the GitHub release
    python deadlock_mirror.py --only Abrams Haze # just these pages
    python deadlock_mirror.py --refetch-all      # ignore saved revision ids

Output (default ~/deadlock_mirror, override with DEADLOCK_OUTPUT):
    site/w/...            mirrored pages (.html)
    site/images/...       images, same paths as on the wiki
    site/_assets/css/     stylesheets
    site/_media/yt/       YouTube videos embedded in pages (optional)
    redirects.json        redirect title -> target title (used by the server)
    state.json            progress, so runs can resume
    status.md             human-readable summary of the last run
    mirror.log            run history
"""

import argparse
import hashlib
import html
import http.client
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Override with DEADLOCK_BASE for local testing against a fake wiki.
BASE = os.environ.get("DEADLOCK_BASE", "https://deadlock.wiki").rstrip("/")
HOSTS = {"deadlock.wiki", "www.deadlock.wiki", urllib.parse.urlsplit(BASE).netloc}
# Web fonts the wiki's stylesheets load from elsewhere; saved under /_ext/.
EXTERNAL_FONT_HOSTS = {"fonts.googleapis.com", "fonts.gstatic.com"}
# Bump when stylesheet rewriting changes, so saved stylesheets are redone.
CSS_VERSION = 2
API = f"{BASE}/api.php"

# Namespaces to mirror: 0 = articles, 3000 = Update (patch notes),
# 14 = Category. Add 4 (Deadlock:) or 12 (Help:) if you want those too.
NAMESPACES = [0, 3000, 14]
MAIN_TITLE = "Main Page"
# Fetched first, in this order.
PRIORITY_TITLES = [MAIN_TITLE]
# Skip translated copies of pages ("Abrams/de", "Haze/zh-hans").
SKIP_TRANSLATIONS = True

# Paths on the wiki that are files/scripts, never article titles.
NON_PAGE_ROOTS = {"images", "resources", "skins", "extensions", "load.php",
                  "api.php", "index.php", "rest.php", "_assets", "_media",
                  "favicon.ico", "robots.txt"}

OUTPUT_DIR = Path(os.environ.get("DEADLOCK_OUTPUT", Path.home() / "deadlock_mirror"))
SITE_DIR = OUTPUT_DIR / "site"
STATE_FILE = OUTPUT_DIR / "state.json"
STATUS_FILE = OUTPUT_DIR / "status.md"
REDIRECTS_FILE = OUTPUT_DIR / "redirects.json"
LOG_FILE = OUTPUT_DIR / "mirror.log"

# Politeness: about one request per second. Don't lower this, the wiki is
# community-run.
WAIT_SECONDS = float(os.environ.get("DEADLOCK_WAIT", "1.0"))
USER_AGENT = ("DeadlockOfflineMirror/1.0 (personal offline reader; "
              "+https://github.com/Aliantom/Deadlock-wiki-offline-pull)")

# Where --publish uploads the mirror.
PUBLISH_REPO = os.environ.get("MIRROR_REPO", "Aliantom/Deadlock-wiki-offline-pull")
RELEASE_TAG = "mirror-data"
PART_PREFIX = "mirror-deadlock.tar.part"
PART_SIZE = 1900 * 1024 * 1024   # release assets are capped at 2 GB each

CSS_MAX_AGE = timedelta(days=7)
MISSING_RETRY_AFTER = timedelta(days=7)

YOUTUBE_MAX_HEIGHT = int(os.environ.get("DEADLOCK_YT_HEIGHT", "360"))
YOUTUBE_DIR_NAME = "_media/yt"
YOUTUBE_RETRY_AFTER = timedelta(days=2)
# Keep in sync with server.py.
YOUTUBE_ID_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/|v/)|youtu\.be/|ytimg\.com/vi/)"
    r"([A-Za-z0-9_-]{11})")

# Fallback namespace names, used if the API can't be asked.
DEFAULT_NAMESPACES = {
    "media": -2, "special": -1, "talk": 1, "user": 2, "user talk": 3,
    "deadlock": 4, "project": 4, "deadlock talk": 5, "file": 6, "image": 6,
    "file talk": 7, "mediawiki": 8, "mediawiki talk": 9, "template": 10,
    "template talk": 11, "help": 12, "help talk": 13, "category": 14,
    "category talk": 15, "module": 828, "module talk": 829, "update": 3000,
    "update talk": 3001, "data": 3002, "data talk": 3003, "bucket": 9592,
    "bucket talk": 9593,
}


class Blocked(Exception):
    """The wiki answered with a Cloudflare challenge instead of content."""


# ---------------------------------------------------------------------------
# File names (keep fs_name in sync with server.py)
# ---------------------------------------------------------------------------

_FS_UNSAFE = re.compile(r'[%:?*"<>|\\]')


def fs_name(path: str) -> str:
    """Map a decoded URL path to a file path that is valid on Windows, macOS
    and Linux: characters Windows forbids become %XX. The server applies the
    same mapping to incoming URLs."""
    return _FS_UNSAFE.sub(lambda m: "%%%02X" % ord(m.group(0)), path)


def title_key(title: str) -> str:
    return title.replace(" ", "_")


_TITLE_SAFE = "/:,()!*~-_.'"


def title_to_url(title: str) -> str:
    return f"{BASE}/{urllib.parse.quote(title_key(title), safe=_TITLE_SAFE)}"


def title_to_path(title: str) -> Path:
    return SITE_DIR / "w" / (fs_name(title_key(title)) + ".html")


def local_file(site_path: str) -> Path:
    return SITE_DIR / fs_name(urllib.parse.unquote(site_path.lstrip("/")))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def looks_blocked(status: Optional[int], body: bytes) -> bool:
    return status in (403, 503) and (b"Just a moment" in body or b"challenge-platform" in body
                                     or b"cf-chl" in body)


class Fetcher:
    """Polite HTTP client: one request at a time, with a pause between."""

    def __init__(self, wait: float):
        self.wait = wait
        self.last = 0.0
        self.requests = 0
        self.blocked_in_a_row = 0

    def get(self, url: str) -> Tuple[Optional[int], bytes]:
        url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")
        for attempt in range(3):
            pause = self.wait * random.uniform(0.5, 1.5) - (time.time() - self.last)
            if pause > 0:
                time.sleep(pause)
            self.last = time.time()
            self.requests += 1
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self.blocked_in_a_row = 0
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read()
                except Exception:
                    body = b""
                if looks_blocked(exc.code, body):
                    self.blocked_in_a_row += 1
                    if self.blocked_in_a_row >= 3:
                        raise Blocked(f"Cloudflare challenge on {url}")
                    return exc.code, b""
                if exc.code in (404, 410, 403):
                    return exc.code, b""
                if exc.code == 429 or exc.code >= 500:
                    time.sleep(10 * (attempt + 1))
                    continue
                return exc.code, b""
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                logging.warning("Network error on %s (%s), retrying", url, exc)
                time.sleep(5 * (attempt + 1))
            except ValueError as exc:
                logging.warning("Bad URL %s (%s); skipping", url, exc)
                return None, b""
        return None, b""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def now() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    state.setdefault("pages", {})      # title -> {"revid": int, "fetched": iso}
    state.setdefault("missing", {})    # url -> iso of last 404
    state.setdefault("css", {})        # load.php query -> iso fetched
    state.setdefault("yt_failed", {})  # youtube id -> {"when": iso, "why": str}
    state.setdefault("gone", {})       # local asset path -> iso of last 404
    if state.get("css_version") != CSS_VERSION:
        state["css"] = {}                # re-download and re-rewrite stylesheets
        state["css_version"] = CSS_VERSION
    return state


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def recently_missing(state: dict, url: str) -> bool:
    when = state["missing"].get(url)
    return bool(when) and now() - datetime.fromisoformat(when) < MISSING_RETRY_AFTER


# ---------------------------------------------------------------------------
# Titles and scope
# ---------------------------------------------------------------------------

class Scope:
    """Knows the wiki's namespace names and language codes, to decide which
    titles belong in the mirror."""

    def __init__(self):
        self.ns_names: Dict[str, int] = dict(DEFAULT_NAMESPACES)
        self.languages: Set[str] = set()
        self.redirects: Dict[str, str] = {}

    def namespace(self, title: str) -> int:
        if ":" in title:
            ns = self.ns_names.get(title.split(":", 1)[0].replace("_", " ").strip().lower())
            if ns is not None:
                return ns
        return 0

    def is_translation(self, title: str) -> bool:
        return SKIP_TRANSLATIONS and "/" in title and \
            title.rsplit("/", 1)[1].lower() in self.languages

    def wanted(self, title: str) -> bool:
        return (self.namespace(title) in NAMESPACES and not self.is_translation(title)
                and title not in self.redirects)


def api_query(fetcher: Fetcher, params: dict) -> Optional[dict]:
    status, body = fetcher.get(f"{API}?{urllib.parse.urlencode(params)}")
    if status != 200:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def api_all(fetcher: Fetcher, params: dict):
    """Yield every result page of an API query, following 'continue'."""
    params = {"action": "query", "format": "json", **params}
    while True:
        data = api_query(fetcher, params)
        if data is None:
            raise IOError("API request failed")
        yield data
        if "continue" not in data:
            return
        params = {**params, **data["continue"]}


def load_site_info(fetcher: Fetcher, scope: Scope) -> bool:
    data = api_query(fetcher, {"action": "query", "format": "json", "meta": "siteinfo",
                               "siprop": "namespaces|namespacealiases|languages"})
    if not data:
        return False
    q = data.get("query", {})
    for ns in q.get("namespaces", {}).values():
        for key in ("*", "name", "canonical"):
            if ns.get(key):
                scope.ns_names[ns[key].lower()] = ns["id"]
    for alias in q.get("namespacealiases", []):
        name = alias.get("*") or alias.get("alias")
        if name:
            scope.ns_names[name.lower()] = alias["id"]
    scope.languages = {l["code"].lower() for l in q.get("languages", []) if l.get("code")}
    scope.languages.discard("")
    return True


def discover_pages(fetcher: Fetcher, scope: Scope) -> Dict[str, Optional[int]]:
    """Return {title: latest revision id} for every page to mirror, and fill
    scope.redirects. Returns {} if the API can't be reached; pages are then
    discovered by following links."""
    found: Dict[str, Optional[int]] = {}
    try:
        for ns in NAMESPACES:
            for data in api_all(fetcher, {
                    "generator": "allpages", "gapnamespace": str(ns), "gaplimit": "500",
                    "gapfilterredir": "nonredirects", "prop": "info"}):
                for page in data.get("query", {}).get("pages", {}).values():
                    if not scope.is_translation(page["title"]):
                        found[page["title"]] = page.get("lastrevid")
            for data in api_all(fetcher, {
                    "generator": "allpages", "gapnamespace": str(ns), "gaplimit": "500",
                    "gapfilterredir": "redirects", "redirects": "1"}):
                for r in data.get("query", {}).get("redirects", []):
                    target = r["to"] + (f"#{r['tofragment']}" if r.get("tofragment") else "")
                    scope.redirects[r["from"]] = target
    except IOError:
        logging.warning("Page-list API unreachable; discovering pages from links only.")
        return {}
    logging.info("Discovered %d pages and %d redirects", len(found), len(scope.redirects))
    return found


def save_redirects(scope: Scope) -> None:
    REDIRECTS_FILE.write_text(json.dumps(
        {title_key(k): title_key(v) for k, v in sorted(scope.redirects.items())},
        indent=0, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Rewriting pages and stylesheets
# ---------------------------------------------------------------------------

SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
NOSCRIPT_RE = re.compile(r"</?noscript\b[^>]*>", re.I)
SRCSET_RE = re.compile(r"\s(?:data-)?srcset\s*=\s*(\"[^\"]*\"|'[^']*')", re.I)
# Tag matchers skip over quoted attribute values, which can contain a raw ">".
_ATTRS = r"""(?:[^>"']|"[^"]*"|'[^']*')*"""
LINK_TAG_RE = re.compile(r"<link\b" + _ATTRS + r">", re.I)
IMG_TAG_RE = re.compile(r"<img\b" + _ATTRS + r">", re.I)
MEDIA_TAG_RE = re.compile(r"<(?:video|source|audio|track)\b" + _ATTRS + r">", re.I)
ATTR_RE = re.compile(r"(\s(src|href|data-src|poster)\s*=\s*)(\"[^\"]*\"|'[^']*')", re.I)
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.I)
CSS_IMPORT_RE = re.compile(r"@import\s+(['\"])([^'\"]+)\1", re.I)
PAGE_LINK_RE = re.compile(r"href=\"/([^\"#?]+)", re.I)


def link_title(href_path: str) -> Optional[str]:
    """'/Abrams' -> 'Abrams'; asset and script paths -> None."""
    path = urllib.parse.unquote(href_path)
    if not path or path.split("/", 1)[0] in NON_PAGE_ROOTS:
        return None
    return path.replace("_", " ")


def to_site_path(url: str, base_url: str) -> Optional[str]:
    """Resolve a URL as it appears in a page; return its path (with query) if
    it lives on the wiki, otherwise None."""
    url = html.unescape(url.strip())
    if not url or url.startswith(("data:", "javascript:", "mailto:", "#")):
        return None
    parts = urllib.parse.urlsplit(urllib.parse.urljoin(base_url, url))
    if parts.netloc not in HOSTS:
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


def is_stylesheet_url(path: str) -> bool:
    return path.split("?", 1)[0].endswith("/load.php") and "only=styles" in path


def css_local_path(path: str) -> str:
    query = path.split("?", 1)[1] if "?" in path else ""
    return f"/_assets/css/{hashlib.sha1(query.encode('utf-8')).hexdigest()[:16]}.css"


def asset_local_path(path: str) -> str:
    """Images, fonts, svgs: same path as on the site, query string dropped."""
    return path.split("?", 1)[0]


class Mirror:
    def __init__(self, fetcher: Fetcher, state: dict, scope: Scope):
        self.fetcher = fetcher
        self.state = state
        self.scope = scope
        self.done_assets: Set[str] = set()
        self.stats = {"pages": 0, "assets": 0, "css": 0, "failed": 0}
        self.failed_pages: List[str] = []

    # -- assets ------------------------------------------------------------

    def download(self, site_path: str, dest: Path) -> bool:
        url = BASE + site_path
        if recently_missing(self.state, url):
            return False
        status, body = self.fetcher.get(url)
        if status != 200:
            if status in (404, 410):
                self.state["missing"][url] = now().isoformat()
                self.state["gone"][asset_local_path(site_path)] = now().isoformat()
            self.stats["failed"] += 1
            logging.info("  %s -> %s", site_path, status)
            return False
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(body)
            tmp.replace(dest)
        except OSError as exc:  # e.g. a file name too long for the disk
            logging.warning("  could not save %s (%s)", site_path, exc)
            self.stats["failed"] += 1
            return False
        return True

    def ensure_asset(self, site_path: str) -> str:
        """Make sure an image/font/video is on disk; return the path to link to."""
        local = asset_local_path(site_path)
        if local in self.done_assets:
            return local
        self.done_assets.add(local)
        dest = local_file(local)
        if not dest.exists() and self.download(site_path, dest):
            self.stats["assets"] += 1
        return local

    def ensure_stylesheet(self, site_path: str) -> str:
        """Download a load.php stylesheet, rewrite the urls inside it, and
        return the local path to link to."""
        local = css_local_path(site_path)
        if local in self.done_assets:
            return local
        self.done_assets.add(local)
        dest = local_file(local)
        query = site_path.split("?", 1)[-1]
        fetched = self.state["css"].get(query)
        if dest.exists() and fetched and now() - datetime.fromisoformat(fetched) < CSS_MAX_AGE:
            return local
        url = BASE + site_path
        status, body = self.fetcher.get(url)
        if status != 200:
            self.stats["failed"] += 1
            logging.info("  stylesheet %s -> %s", site_path[:120], status)
            return local if dest.exists() else site_path
        css = self.rewrite_css(body.decode("utf-8", errors="replace"), url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(css, encoding="utf-8")
        self.state["css"][query] = now().isoformat()
        self.stats["css"] += 1
        return local

    def ensure_external(self, url: str) -> Optional[str]:
        """Web fonts from Google Fonts: save them under /_ext/ so the font
        looks right even where Google is unreachable."""
        parts = urllib.parse.urlsplit(url)
        if parts.netloc == "fonts.googleapis.com":
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
            local = f"/_ext/fonts/{digest}.css"
            if local in self.done_assets:
                return local
            self.done_assets.add(local)
            dest = local_file(local)
            if dest.exists():
                return local
            status, body = self.fetcher.get(url)
            if status != 200:
                self.stats["failed"] += 1
                return None
            css = CSS_URL_RE.sub(
                lambda m: "url(%s)" % (self.ensure_external(
                    urllib.parse.urljoin(url, html.unescape(m.group(2)))) or m.group(2)),
                body.decode("utf-8", errors="replace"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(css, encoding="utf-8")
            self.stats["css"] += 1
            return local
        if parts.netloc == "fonts.gstatic.com":
            local = f"/_ext/{parts.netloc}{parts.path}"
            if local in self.done_assets:
                return local
            self.done_assets.add(local)
            dest = local_file(local)
            if not dest.exists():
                status, body = self.fetcher.get(url)
                if status != 200:
                    self.stats["failed"] += 1
                    return None
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
                self.stats["assets"] += 1
            return local
        return None

    def rewrite_css(self, css: str, base_url: str) -> str:
        def fix_url(m):
            quote, target = m.group(1), m.group(2)
            path = to_site_path(target, base_url)
            if path is None:
                full = urllib.parse.urljoin(base_url, html.unescape(target.strip()))
                if urllib.parse.urlsplit(full).netloc in EXTERNAL_FONT_HOSTS:
                    local = self.ensure_external(full)
                    if local:
                        return f"url({quote}{local}{quote})"
                return m.group(0)
            if is_stylesheet_url(path):
                return f"url({quote}{self.ensure_stylesheet(path)}{quote})"
            return f"url({quote}{self.ensure_asset(path)}{quote})"

        def fix_import(m):
            path = to_site_path(m.group(2), base_url)
            if path is None:
                full = urllib.parse.urljoin(base_url, html.unescape(m.group(2).strip()))
                local = (self.ensure_external(full)
                         if urllib.parse.urlsplit(full).netloc in EXTERNAL_FONT_HOSTS else None)
                return f'@import "{local}"' if local else m.group(0)
            return f'@import "{self.ensure_stylesheet(path)}"'

        css = CSS_IMPORT_RE.sub(fix_import, css)
        return CSS_URL_RE.sub(fix_url, css)

    # -- pages -------------------------------------------------------------

    def rewrite_page(self, page: str, page_url: str) -> str:
        page = SCRIPT_RE.sub("", page)
        page = NOSCRIPT_RE.sub("", page)
        page = SRCSET_RE.sub("", page)

        def fix_attr(m, tag_kind):
            prefix, name, quoted = m.group(1), m.group(2).lower(), m.group(3)
            q = quoted[0]
            path = to_site_path(quoted[1:-1], page_url)
            if path is None:
                return m.group(0)
            if tag_kind == "stylesheet":
                if name != "href":
                    return m.group(0)
                new = self.ensure_stylesheet(path) if is_stylesheet_url(path) else self.ensure_asset(path)
            elif tag_kind == "icon":
                if name != "href":
                    return m.group(0)
                new = self.ensure_asset(path)
            elif tag_kind == "img":
                new = self.ensure_asset(path)
                if name == "data-src":
                    # Lazy-loaded image: promote to a real src.
                    return f' src={q}{html.escape(new, quote=True)}{q}'
            else:
                new = path
            return f"{prefix}{q}{html.escape(new, quote=True)}{q}"

        def fix_link_tag(m):
            tag = m.group(0)
            rel = re.search(r"\srel\s*=\s*[\"']([^\"']*)", tag, re.I)
            rel = rel.group(1).lower() if rel else ""
            if "stylesheet" in rel:
                return ATTR_RE.sub(lambda a: fix_attr(a, "stylesheet"), tag)
            if "icon" in rel:
                return ATTR_RE.sub(lambda a: fix_attr(a, "icon"), tag)
            if any(r in rel for r in ("preload", "modulepreload", "prefetch", "preconnect",
                                      "dns-prefetch", "edituri", "search", "alternate")):
                # Only useful on the live site (or would make requests to it).
                return ""
            return tag

        def fix_img_tag(m):
            tag = m.group(0)
            if re.search(r"\sdata-src\s*=", tag, re.I):
                tag = re.sub(r"\ssrc\s*=\s*(\"[^\"]*\"|'[^']*')", "", tag, count=1, flags=re.I)
            return ATTR_RE.sub(lambda a: fix_attr(a, "img"), tag)

        page = LINK_TAG_RE.sub(fix_link_tag, page)
        page = IMG_TAG_RE.sub(fix_img_tag, page)
        page = MEDIA_TAG_RE.sub(fix_img_tag, page)
        # Inline styles and <style> blocks.
        page = CSS_URL_RE.sub(lambda m: self.rewrite_css(m.group(0), page_url), page)

        # Absolute wiki links in <a href> etc. -> keep them on the mirror.
        def fix_other(m):
            prefix, quoted = m.group(1), m.group(3)
            q = quoted[0]
            parts = urllib.parse.urlsplit(html.unescape(quoted[1:-1]))
            if parts.scheme in ("http", "https") and parts.netloc in HOSTS:
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                path += f"#{parts.fragment}" if parts.fragment else ""
                return f"{prefix}{q}{html.escape(path, quote=True)}{q}"
            return m.group(0)

        return ATTR_RE.sub(fix_other, page)

    def fetch_page(self, title: str) -> Tuple[bool, List[str]]:
        url = title_to_url(title)
        status, body = self.fetcher.get(url)
        if status != 200:
            logging.warning("Page %s -> %s", title, status)
            self.stats["failed"] += 1
            self.failed_pages.append(f"{title} ({status})")
            return False, []
        page = body.decode("utf-8", errors="replace")
        linked = sorted({t for t in (link_title(m) for m in PAGE_LINK_RE.findall(page)) if t})
        before = dict(self.stats)
        page = self.rewrite_page(page, url)
        dest = title_to_path(title)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_text(page, encoding="utf-8")
        tmp.replace(dest)
        self.stats["pages"] += 1
        logging.info("Saved %s (+%d images/fonts, +%d stylesheets)", title,
                     self.stats["assets"] - before["assets"], self.stats["css"] - before["css"])
        return True, [t for t in linked if self.scope.wanted(t)]


# ---------------------------------------------------------------------------
# Audit, planning, videos
# ---------------------------------------------------------------------------

AUDIT_REF_RE = re.compile(r"\s(?:src|poster)\s*=\s*\"([^\"]+)\"", re.I)
AUDIT_CSS_RE = re.compile(r"<link\b[^>]*stylesheet[^>]*href=\"([^\"]+)\"", re.I)
IFRAME_RE = re.compile(r"<iframe\b[^>]*>", re.I)
VIDEO_TAG_RE = re.compile(r"<video\b", re.I)
EMBED_HOST_RE = re.compile(r"(?:data-)?src=\"(?:https?:)?//([^/\"]+)", re.I)


def page_titles_on_disk() -> List[str]:
    w = SITE_DIR / "w"
    if not w.is_dir():
        return []
    return sorted(urllib.parse.unquote(f.relative_to(w).as_posix()[:-5]).replace("_", " ")
                  for f in w.rglob("*.html"))


def audit(state: dict) -> dict:
    """A page counts as fully rendered only when every stylesheet, image,
    font and video it references is on disk (files the wiki itself says
    don't exist are not counted against it)."""
    css_cache: Dict[str, List[str]] = {}
    result = {"pages": 0, "good": 0, "old": [], "broken": {}, "missing_files": 0,
              "embeds": {}, "video_tags": 0, "samples": []}

    def exists(ref: str) -> bool:
        local = urllib.parse.unquote(html.unescape(ref)).split("?", 1)[0]
        return local in state["gone"] or local_file(local).exists()

    for title in page_titles_on_disk():
        result["pages"] += 1
        text = title_to_path(title).read_text(encoding="utf-8", errors="replace")
        if "<script" in text or title not in state["pages"]:
            result["old"].append(title)
            continue
        refs = [r for r in AUDIT_REF_RE.findall(text) if r.startswith("/")]
        for css in AUDIT_CSS_RE.findall(text):
            refs.append(css)
            if css not in css_cache:
                f = local_file(css)
                body = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
                css_cache[css] = [u for _, u in CSS_URL_RE.findall(body) if u.startswith("/")]
            refs += css_cache[css]
        refs += [u for _, u in CSS_URL_RE.findall(text) if u.startswith("/")]
        missing = sorted({r for r in refs if not exists(r)})
        if missing:
            result["broken"][title] = missing
            result["missing_files"] += len(missing)
            if len(result["samples"]) < 3:
                ref = missing[0]
                at = text.find(ref)
                start = max(0, text.rfind("<", 0, max(at, 0)) - 200)
                snippet = text[start:at + len(ref) + 150] if at >= 0 else ref
                result["samples"].append(f"{title}: " + " ".join(snippet.split()))
        else:
            result["good"] += 1
        for tag in IFRAME_RE.findall(text):
            for host in EMBED_HOST_RE.findall(tag):
                result["embeds"][host] = result["embeds"].get(host, 0) + 1
        result["video_tags"] += len(VIDEO_TAG_RE.findall(text))
    return result


def plan(state: dict, discovered: Dict[str, Optional[int]], scope: Scope,
         refetch_all: bool, broken: Set[str]) -> List[str]:
    """Order: priority pages, then pages never downloaded, then pages with
    missing files, then changed pages."""
    known = set(discovered) | set(PRIORITY_TITLES)
    if not discovered:
        known |= set(state["pages"]) | set(page_titles_on_disk())

    def needs_fetch(title: str) -> bool:
        if refetch_all or not title_to_path(title).exists() or title in broken:
            return True
        saved = state["pages"].get(title)
        if not saved:
            return True
        latest = discovered.get(title)
        return latest is not None and latest != saved.get("revid")

    todo = [t for t in PRIORITY_TITLES if needs_fetch(t)]
    rest = sorted(t for t in known if t not in PRIORITY_TITLES and needs_fetch(t))
    missing_first = [t for t in rest if not title_to_path(t).exists()]
    repair = [t for t in rest if title_to_path(t).exists() and
              (t in broken or t not in state["pages"])]
    changed = [t for t in rest if t not in missing_first and t not in repair]
    return todo + missing_first + repair + changed


def prune_deleted(discovered: Dict[str, Optional[int]], state: dict) -> int:
    """Remove pages that were deleted or turned into redirects on the wiki."""
    if not discovered:
        return 0
    removed = 0
    for title in page_titles_on_disk():
        # Only pages the API listed before; pages found by following links
        # (revid None) aren't in the API listing to begin with.
        saved = state["pages"].get(title) or {}
        if title not in discovered and title not in PRIORITY_TITLES and saved.get("revid"):
            title_to_path(title).unlink(missing_ok=True)
            state["pages"].pop(title, None)
            removed += 1
    if removed:
        logging.info("Removed %d pages no longer on the wiki", removed)
    return removed


def youtube_ids() -> Dict[str, List[str]]:
    ids: Dict[str, List[str]] = {}
    for title in page_titles_on_disk():
        text = title_to_path(title).read_text(encoding="utf-8", errors="replace")
        for vid in dict.fromkeys(YOUTUBE_ID_RE.findall(text)):
            ids.setdefault(vid, []).append(title)
    return ids


def youtube_file(vid: str) -> Path:
    return SITE_DIR / YOUTUBE_DIR_NAME / f"{vid}.mp4"


def download_youtube(state: dict, deadline: Optional[datetime]) -> dict:
    """Download YouTube videos embedded in pages with yt-dlp."""
    ids = youtube_ids()
    result = {"total": len(ids), "downloaded": 0, "failed_now": 0, "remaining": 0,
              "skipped": None}
    todo = []
    for vid in ids:
        if youtube_file(vid).exists():
            continue
        failed = state["yt_failed"].get(vid)
        if failed and now() - datetime.fromisoformat(failed["when"]) < YOUTUBE_RETRY_AFTER:
            continue
        todo.append(vid)
    result["remaining"] = len(todo)
    if not todo:
        return result
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        result["skipped"] = "yt-dlp not installed (pip install yt-dlp)"
        return result
    (SITE_DIR / YOUTUBE_DIR_NAME).mkdir(parents=True, exist_ok=True)
    h = YOUTUBE_MAX_HEIGHT
    fmt = (f"b[ext=mp4][height<={h}]/bv*[ext=mp4][height<={h}]+ba[ext=m4a]/"
           f"b[height<={h}]/bv*[height<={h}]+ba/b")
    streak = 0
    for vid in todo:
        if deadline and now() > deadline:
            break
        out = youtube_file(vid)
        cmd = [ytdlp, "-q", "--no-warnings", "--no-playlist", "-f", fmt,
               "--merge-output-format", "mp4", "--max-filesize", "80M",
               "--sleep-requests", "1", "-o", str(out.with_suffix(".%(ext)s")),
               f"https://www.youtube.com/watch?v={vid}"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            ok = proc.returncode == 0 and out.exists()
            why = (proc.stderr.strip().splitlines() or ["unknown error"])[-1][:200]
        except subprocess.TimeoutExpired:
            ok, why = False, "timed out"
        if ok:
            result["downloaded"] += 1
            state["yt_failed"].pop(vid, None)
            streak = 0
            logging.info("Video %s saved (%s)", vid, ids[vid][0])
        else:
            result["failed_now"] += 1
            state["yt_failed"][vid] = {"when": now().isoformat(), "why": why}
            streak += 1
            logging.warning("Video %s failed: %s", vid, why)
            if streak >= 8:
                logging.warning("8 videos failed in a row; stopping videos for this run.")
                break
        save_state(state)
        time.sleep(2)
    result["remaining"] = sum(1 for v in ids if not youtube_file(v).exists()
                              and v not in state["yt_failed"])
    return result


def write_status(state: dict, discovered: Dict[str, Optional[int]], remaining: List[str],
                 stats: dict, started: datetime, fetcher: Fetcher, failed_pages: List[str],
                 check: dict, videos: Optional[dict], note: str = "") -> None:
    total = len(set(discovered) | set(page_titles_on_disk()))
    embeds = ", ".join(f"{h} x{n}" for h, n in
                       sorted(check["embeds"].items(), key=lambda x: -x[1])[:6])
    lines = [
        "# Deadlock Wiki mirror status",
        "",
        f"- Run finished: {now().strftime('%Y-%m-%d %H:%M UTC')} "
        f"(took {str(now() - started).split('.')[0]}, {fetcher.requests} requests)",
        f"- Pages fully rendered (every file present): **{check['good']} / {total}**",
        f"- Pages with missing files: {len(check['broken'])} ({check['missing_files']} files)"
        + (" (re-queued)" if check["broken"] else ""),
        f"- Videos: {check['video_tags']} <video> tags; embeds: {embeds or 'none'}",
    ]
    if videos:
        have = sum(1 for v in youtube_ids() if youtube_file(v).exists())
        lines.append(f"- YouTube videos: **{have} / {videos['total']}** downloaded; "
                     f"this run +{videos['downloaded']}, {videos['failed_now']} failed"
                     + (f" ({videos['skipped']})" if videos.get("skipped") else ""))
    lines += [
        f"- This run: {stats['pages']} pages, {stats['assets']} images/fonts/videos, "
        f"{stats['css']} stylesheets, {stats['failed']} failed requests",
        f"- Still to do: {len(remaining)} pages"
        + (" (next run continues from here)" if remaining else " — mirror is complete"),
    ]
    if note:
        lines += ["", note]
    if failed_pages:
        lines += ["", f"Pages that failed ({len(failed_pages)}):", ""] + \
                 [f"- {t}" for t in failed_pages[:15]]
    for sample in check["samples"]:
        lines += ["", "Markup around a missing file: " + sample[:900]]
    if check["broken"]:
        lines += ["", "Pages with missing files:", ""] + [
            f"- {t}: {', '.join(m[:3])}" for t, m in list(check["broken"].items())[:10]]
    if remaining:
        lines += ["", "Next up:", ""] + [f"- {t}" for t in remaining[:15]]
    text = "\n".join(lines) + "\n"
    STATUS_FILE.write_text(text, encoding="utf-8")
    print(text)


# ---------------------------------------------------------------------------
# Publishing to the GitHub release
# ---------------------------------------------------------------------------

class SplitWriter:
    """File-like object that writes a stream into numbered part files."""

    def __init__(self, directory: Path, prefix: str, size: int):
        self.directory, self.prefix, self.size = directory, prefix, size
        self.index, self.written, self.fh = -1, 0, None
        self.paths: List[Path] = []
        self._next()

    def _next(self):
        if self.fh:
            self.fh.close()
        self.index += 1
        path = self.directory / f"{self.prefix}{self.index:02d}"
        self.paths.append(path)
        self.fh = open(path, "wb")
        self.written = 0

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        while view:
            room = self.size - self.written
            if room == 0:
                self._next()
                room = self.size
            chunk = view[:room]
            self.fh.write(chunk)
            self.written += len(chunk)
            view = view[len(chunk):]
        return len(data)

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def publish() -> bool:
    """Pack the mirror into tar parts and upload them to the release."""
    if not shutil.which("gh"):
        logging.error("Publishing needs the GitHub CLI: https://cli.github.com then `gh auth login`")
        return False
    out_dir = OUTPUT_DIR.parent / (OUTPUT_DIR.name + "_upload")
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    logging.info("Packing %s ...", OUTPUT_DIR)
    writer = SplitWriter(out_dir, PART_PREFIX, PART_SIZE)

    def skip(info: tarfile.TarInfo):
        name = info.name
        if name.endswith((".part", ".tmp")) or name.endswith("/mirror.log"):
            return None
        return info

    with tarfile.open(fileobj=writer, mode="w|") as tar:
        tar.add(str(OUTPUT_DIR), arcname="deadlock_mirror", filter=skip)
    writer.close()
    parts = writer.paths
    logging.info("Packed into %d part(s): %s", len(parts),
                 ", ".join(f"{p.name} ({p.stat().st_size // 1048576} MB)" for p in parts))
    try:
        if gh("release", "view", RELEASE_TAG, "-R", PUBLISH_REPO, check=False).returncode != 0:
            gh("release", "create", RELEASE_TAG, "-R", PUBLISH_REPO, "--title", "Mirror data",
               "--notes", "Deadlock wiki mirror, uploaded by deadlock_mirror.py --publish. "
                          "Download with .devcontainer/refresh.sh.")
        gh("release", "upload", RELEASE_TAG, *map(str, parts), "--clobber", "-R", PUBLISH_REPO)
        names = {p.name for p in parts}
        listed = gh("release", "view", RELEASE_TAG, "-R", PUBLISH_REPO, "--json", "assets",
                    "-q", ".assets[].name").stdout.split()
        for old in listed:
            if old.startswith(PART_PREFIX) and old not in names:
                gh("release", "delete-asset", RELEASE_TAG, old, "-y", "-R", PUBLISH_REPO)
    except subprocess.CalledProcessError as exc:
        logging.error("Upload failed: %s", (exc.stderr or exc.stdout or "").strip()[-500:])
        return False
    shutil.rmtree(out_dir, ignore_errors=True)
    logging.info("Published to https://github.com/%s/releases/tag/%s", PUBLISH_REPO, RELEASE_TAG)
    return True


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def check_access() -> int:
    """Quick test: can this machine reach the wiki past Cloudflare?"""
    fetcher = Fetcher(0.5)
    ok = True
    for label, url in (("API", f"{API}?action=query&meta=siteinfo&format=json"),
                       ("Page", title_to_url(MAIN_TITLE))):
        try:
            status, body = fetcher.get(url)
        except Blocked:
            status, body = 403, b"Just a moment"
        if status == 200:
            print(f"OK       {label}: {url} ({len(body)} bytes)")
        else:
            ok = False
            print(f"BLOCKED  {label}: {url} -> HTTP {status}"
                  + (" (Cloudflare bot check)" if status in (403, 503) else ""))
    print("\nThis machine can mirror the wiki." if ok else
          "\nThis machine can't reach the wiki; the mirror has to run somewhere else.")
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror deadlock.wiki for offline reading.")
    parser.add_argument("--check", action="store_true",
                        help="Only test whether this machine can reach the wiki.")
    parser.add_argument("--budget-minutes", type=float, default=None,
                        help="Stop starting new pages after this many minutes.")
    parser.add_argument("--only", nargs="+", metavar="TITLE", help="Only fetch these titles.")
    parser.add_argument("--no-videos", action="store_true",
                        help="Skip downloading embedded YouTube videos.")
    parser.add_argument("--refetch-all", action="store_true",
                        help="Re-download every page even if unchanged.")
    parser.add_argument("--publish", action="store_true",
                        help="Upload the mirror to the GitHub release afterwards.")
    parser.add_argument("--publish-only", action="store_true",
                        help="Upload what's on disk without downloading anything.")
    args = parser.parse_args()

    # Windows consoles default to a legacy code page; don't let a page title
    # with an unusual character crash the run when it's printed.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.check:
        return check_access()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    if args.publish_only:
        return 0 if publish() else 1

    logging.info("=" * 60)
    logging.info("Deadlock wiki mirror run starting -> %s", OUTPUT_DIR)
    started = now()
    deadline = started + timedelta(minutes=args.budget_minutes) if args.budget_minutes else None
    fetcher = Fetcher(WAIT_SECONDS)
    state = load_state()
    scope = Scope()
    mirror = Mirror(fetcher, state, scope)
    discovered: Dict[str, Optional[int]] = {}
    queue: List[str] = []
    i = 0
    note = ""
    try:
        load_site_info(fetcher, scope)
        if args.only:
            queue = list(args.only)
        else:
            discovered = discover_pages(fetcher, scope)
            if scope.redirects:
                save_redirects(scope)
            prune_deleted(discovered, state)
            queue = plan(state, discovered, scope, args.refetch_all, set(audit(state)["broken"]))
        logging.info("%d pages to fetch this run", len(queue))
        seen = set(queue)
        while i < len(queue):
            if deadline and now() > deadline:
                logging.info("Time budget reached; stopping here. Progress is saved.")
                break
            title = queue[i]
            i += 1
            try:
                ok, linked = mirror.fetch_page(title)
            except Blocked:
                raise
            except Exception as exc:
                logging.error("Error on %s:\n%s", title, traceback.format_exc())
                mirror.failed_pages.append(f"{title} (error: {type(exc).__name__}: {exc})"[:200])
                ok, linked = False, []
            if ok:
                state["pages"][title] = {"revid": discovered.get(title),
                                         "fetched": now().isoformat()}
                save_state(state)
            if not args.only:
                # Pick up pages the API didn't list (or all of them, if it failed).
                for t in linked:
                    if t not in seen and not title_to_path(t).exists():
                        seen.add(t)
                        queue.append(t)
    except Blocked as exc:
        logging.error("Stopped: %s. This machine is being blocked by the wiki's "
                      "Cloudflare check; see the readme.", exc)
        note = ("**Stopped early: the wiki's Cloudflare bot check blocked this machine.** "
                "Progress so far is saved.")

    save_state(state)
    videos = None
    if not args.only and not args.no_videos and not note:
        if i < len(queue):
            videos = {"total": len(youtube_ids()), "downloaded": 0, "failed_now": 0,
                      "remaining": 0, "skipped": "waiting for pages to finish"}
        else:
            try:
                videos = download_youtube(state, deadline)
            except Exception as exc:
                logging.error("Video phase failed:\n%s", traceback.format_exc())
                videos = {"total": 0, "downloaded": 0, "failed_now": 0, "remaining": 0,
                          "skipped": f"error: {type(exc).__name__}: {exc}"[:150]}
    save_state(state)
    write_status(state, discovered, queue[i:], mirror.stats, started, fetcher,
                 mirror.failed_pages, audit(state), videos, note)
    if args.publish and mirror.stats["pages"] + (videos or {}).get("downloaded", 0) > 0:
        if not publish():
            return 1
    elif args.publish:
        logging.info("Nothing new downloaded; skipping upload.")
    return 2 if note else 0


if __name__ == "__main__":
    sys.exit(main())
