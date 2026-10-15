#!/usr/bin/env python3
"""Serves the Deadlock wiki mirror at the same URLs as the live wiki.

    cd deadlock_mirror && python3 server.py 8000

/Abrams           -> site/w/Abrams.html (page)
/images/...       -> site/images/... (files, same paths as the wiki)
/Some_redirect    -> 302 to its target (from redirects.json)
/File:Name.png    -> 302 to the image itself
/index.php?search=... or Special:Search -> offline search
/_status          -> the mirror's last run report
"""
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")
PAGES = os.path.join(SITE, "w")
MAIN_PAGE = "Main_Page"
LIVE = "https://deadlock.wiki"

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")

_FS_UNSAFE = re.compile(r'[%:?*"<>|\\]')


def fs_name(path):
    """Keep in sync with deadlock_mirror.py: Windows-unsafe characters -> %XX."""
    return _FS_UNSAFE.sub(lambda m: "%%%02X" % ord(m.group(0)), path)


def url_for(title):
    return "/" + quote(title.replace(" ", "_"), safe="/:,()!*~-_.'#")


# ---------------------------------------------------------------------------
# What gets added to every page
# ---------------------------------------------------------------------------

INJECT_CSS = """<style id="mirror-css">
/* --- Tabs (TabberNeue). Without JavaScript every panel is shown, stacked,
   each under its own title. mirror.js turns them back into real tabs. --- */
.tabber:not(.mirror-tabs-on), .tabber:not(.mirror-tabs-on) .tabber__section {
  display: block !important; overflow: visible !important; height: auto !important;
  max-height: none !important; transform: none !important; position: static !important; }
.tabber:not(.mirror-tabs-on) > .tabber__section > .tabber__panel,
.tabber:not(.mirror-tabs-on) [role="tabpanel"] {
  display: block !important; visibility: visible !important; width: auto !important;
  max-width: 100% !important; height: auto !important; transform: none !important;
  opacity: 1 !important; position: static !important; flex-shrink: 0 !important;
  margin-bottom: 1em; }
.tabber:not(.mirror-tabs-on) > .tabber__section > .tabber__panel[data-mw-tabber-title]::before {
  content: attr(data-mw-tabber-title); display: block; font-weight: bold;
  border-bottom: 1px solid #a2a9b1; margin: .6em 0 .3em; }
.tabber .tabber__header, .tabber .tabber__tabs { display: none !important; }
.tabber.mirror-tabs-on > .tabber__section { display: block !important; overflow: visible !important;
  height: auto !important; transform: none !important; }
.tabber.mirror-tabs-on > .tabber__section > .tabber__panel { display: none !important; }
.tabber.mirror-tabs-on > .tabber__section > .tabber__panel.mirror-active {
  display: block !important; visibility: visible !important; height: auto !important;
  width: auto !important; opacity: 1 !important; transform: none !important; position: static !important; }
.mirror-tabbar { display: flex; flex-wrap: wrap; gap: 2px; border-bottom: 1px solid #a2a9b1;
  margin-bottom: .5em; }
.mirror-tabbar button { font: inherit; background: none; border: 0; border-bottom: 2px solid transparent;
  padding: .35em .8em; cursor: pointer; color: inherit; opacity: .75; }
.mirror-tabbar button[aria-selected="true"] { border-bottom-color: currentColor; opacity: 1;
  font-weight: bold; }
/* --- Collapsible sections and sortable tables (mirror.js) --- */
.mirror-toggle { float: right; font-size: .85em; font-weight: normal; cursor: pointer;
  margin-left: .5em; user-select: none; }
.mirror-hidden { display: none !important; }
table.sortable th.mirror-sort { cursor: pointer; }
/* Things that only work on the live site */
.embedvideo-privacyNotice, .embedvideo-consent { display: none !important; }
.mirror-video { margin: .5em 0; max-width: 100%; }
.mirror-video video { width: 100%; max-width: 720px; display: block; background: #000; }
.mirror-banner { font: 12px/1.4 system-ui, sans-serif; background: #222; color: #ddd;
  padding: 3px 10px; text-align: center; }
.mirror-banner a { color: #9cf; }
</style>
"""

MIRROR_JS = r"""<script id="mirror-js">
(function () {
  "use strict";
  // ---- Tabs -------------------------------------------------------------
  function panelsOf(tabber) {
    var sec = null;
    for (var i = 0; i < tabber.children.length; i++)
      if (tabber.children[i].classList.contains("tabber__section")) sec = tabber.children[i];
    if (!sec) return [];
    return Array.prototype.filter.call(sec.children, function (c) {
      return c.classList.contains("tabber__panel") || c.getAttribute("role") === "tabpanel";
    });
  }
  function titleOf(tabber, panel, n) {
    var t = panel.getAttribute("data-mw-tabber-title") || panel.getAttribute("data-title");
    if (!t && panel.id) {
      var a = tabber.querySelector('[aria-controls="' + panel.id + '"], a[href="#' + panel.id + '"]');
      if (a) t = a.textContent.trim();
    }
    if (!t && panel.getAttribute("aria-labelledby")) {
      var l = document.getElementById(panel.getAttribute("aria-labelledby"));
      if (l) t = l.textContent.trim();
    }
    return t || ("Tab " + (n + 1));
  }
  function setupTabber(tabber) {
    var panels = panelsOf(tabber);
    if (panels.length < 1) return;
    var bar = document.createElement("div");
    bar.className = "mirror-tabbar";
    bar.setAttribute("role", "tablist");
    var buttons = panels.map(function (p, n) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = titleOf(tabber, p, n);
      b.setAttribute("role", "tab");
      b.addEventListener("click", function () { show(n); });
      bar.appendChild(b);
      return b;
    });
    function show(n) {
      panels.forEach(function (p, k) { p.classList.toggle("mirror-active", k === n); });
      buttons.forEach(function (b, k) { b.setAttribute("aria-selected", k === n ? "true" : "false"); });
    }
    var start = 0;
    panels.forEach(function (p, k) {
      if (location.hash && p.id && location.hash.slice(1) === p.id) start = k;
    });
    tabber.insertBefore(bar, tabber.firstChild);
    tabber.classList.add("mirror-tabs-on");
    show(start);
  }
  // ---- Collapsibles (mw-collapsible) ------------------------------------
  function setupCollapsible(el) {
    var rows, content, toggle = document.createElement("span");
    toggle.className = "mirror-toggle";
    if (el.tagName === "TABLE") {
      rows = Array.prototype.slice.call(el.rows, 1);
      var head = el.rows[0] && el.rows[0].cells[el.rows[0].cells.length - 1];
      if (!head) return;
      head.insertBefore(toggle, head.firstChild);
      content = rows;
    } else {
      var inner = el.querySelector(":scope > .mw-collapsible-content");
      content = inner ? [inner] : Array.prototype.slice.call(el.children);
      el.insertBefore(toggle, el.firstChild);
    }
    var collapsed = el.classList.contains("mw-collapsed");
    function apply() {
      content.forEach(function (c) { if (c !== toggle) c.classList.toggle("mirror-hidden", collapsed); });
      toggle.textContent = collapsed ? "[show]" : "[hide]";
    }
    toggle.addEventListener("click", function () { collapsed = !collapsed; apply(); });
    apply();
  }
  // ---- Sortable tables --------------------------------------------------
  function cellValue(cell) {
    var v = cell.getAttribute("data-sort-value");
    if (v === null) v = cell.textContent.trim();
    var num = parseFloat(v.replace(/[,%+]/g, ""));
    return isNaN(num) || !/^[\s+\-]?[\d.,]+%?\s*$/.test(v) ? v.toLowerCase() : num;
  }
  function setupSortable(table) {
    var head = table.tHead ? table.tHead.rows[0] : table.rows[0];
    if (!head) return;
    Array.prototype.forEach.call(head.cells, function (th, col) {
      if (th.classList.contains("unsortable")) return;
      th.classList.add("mirror-sort");
      var asc = true;
      th.addEventListener("click", function (e) {
        if (e.target.closest("a")) return;
        var body = table.tBodies[0];
        var rows = Array.prototype.filter.call(body.rows, function (r) {
          return r !== head && r.cells.length === head.cells.length;
        });
        rows.sort(function (a, b) {
          var x = cellValue(a.cells[col]), y = cellValue(b.cells[col]);
          if (typeof x !== typeof y) { x = String(x); y = String(y); }
          return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1);
        });
        rows.forEach(function (r) { body.appendChild(r); });
        asc = !asc;
      });
    });
  }
  // ---- Countdowns (Template:Timer: data-targets="iso,iso,...") ---------
  function setupTimer(el) {
    var targets = (el.getAttribute("data-targets") || el.getAttribute("data-target") || "")
      .split(",").map(function (s) { return Date.parse(s.trim()); })
      .filter(function (t) { return !isNaN(t); }).sort();
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    function tick() {
      var now = Date.now(), next = null;
      for (var i = 0; i < targets.length; i++) if (targets[i] > now) { next = targets[i]; break; }
      if (next === null) { el.textContent = "Now"; return; }
      var s = Math.floor((next - now) / 1000), d = Math.floor(s / 86400);
      s -= d * 86400;
      var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      el.textContent = (d ? d + "d " : "") + pad(h) + ":" + pad(m) + ":" + pad(sec);
    }
    tick();
    setInterval(tick, 1000);
  }
  function run(fn, list) { Array.prototype.forEach.call(list, function (x) { try { fn(x); } catch (e) {} }); }
  function init() {
    run(setupTabber, document.querySelectorAll(".tabber"));
    run(setupCollapsible, document.querySelectorAll(".mw-collapsible"));
    run(setupSortable, document.querySelectorAll("table.sortable"));
    run(setupTimer, document.querySelectorAll(".template-timer"));
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
</script>
"""

SCRIPT_RE = re.compile(rb"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
BODY_OPEN_RE = re.compile(rb"<body\b[^>]*>", re.I)

# Keep in sync with YOUTUBE_ID_RE in deadlock_mirror.py.
YOUTUBE_ID_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/|v/)|youtu\.be/|ytimg\.com/vi/)"
    r"([A-Za-z0-9_-]{11})")
EMBED_FIGURE_RE = re.compile(
    r"<figure\b[^>]*class=\"[^\"]*embedvideo[^\"]*\"[^>]*>.*?</figure>", re.I | re.S)
IFRAME_RE = re.compile(r"<iframe\b[^>]*>.*?</iframe>", re.I | re.S)
FIGCAPTION_RE = re.compile(r"<figcaption\b.*?</figcaption>", re.I | re.S)
YT_LINK_RE = re.compile(r"href=\"(https?:)?//(?:www\.)?(?:youtube\.com|youtu\.be)/[^\"]*\"", re.I)
YT_TIME_RE = re.compile(r"[?&](?:amp;)?t=(\d+)")


def local_video(vid):
    rel = f"_media/yt/{vid}.mp4"
    return "/" + rel if os.path.isfile(os.path.join(SITE, rel)) else None


def video_tag(src, caption=""):
    return (f'<figure class="mirror-video"><video controls preload="metadata" '
            f'src="{src}"></video>{caption}</figure>')


def swap_youtube(body):
    """Replace YouTube embeds and links with videos downloaded into the mirror."""
    def fig(m):
        ids = YOUTUBE_ID_RE.findall(html.unescape(m.group(0)))
        src = local_video(ids[0]) if ids else None
        if not src:
            return m.group(0)
        cap = FIGCAPTION_RE.search(m.group(0))
        return video_tag(src, cap.group(0) if cap else "")

    def frame(m):
        ids = YOUTUBE_ID_RE.findall(html.unescape(m.group(0)))
        src = local_video(ids[0]) if ids else None
        return video_tag(src) if src else m.group(0)

    def link(m):
        url = html.unescape(m.group(0))
        ids = YOUTUBE_ID_RE.findall(url)
        src = local_video(ids[0]) if ids else None
        if not src:
            return m.group(0)
        t = YT_TIME_RE.search(url)
        return f'href="{src}' + (f"#t={t.group(1)}" if t else "") + '"'

    body = EMBED_FIGURE_RE.sub(fig, body)
    body = IFRAME_RE.sub(frame, body)
    return YT_LINK_RE.sub(link, body)


def prepare_page(body, rel):
    body = SCRIPT_RE.sub(b"", body)
    if b"youtu" in body or b"ytimg" in body:
        body = swap_youtube(body.decode("utf-8", "replace")).encode("utf-8")
    if b"</head>" in body:
        body = body.replace(b"</head>", INJECT_CSS.encode() + b"</head>", 1)
    m = BODY_OPEN_RE.search(body)
    if m:
        banner = (f'<div class="mirror-banner">Offline mirror &middot; <a href="/_status">status</a>'
                  f' &middot; <a href="{LIVE}/{quote(rel)}" rel="noreferrer">live page</a></div>')
        body = body[:m.end()] + banner.encode() + body[m.end():]
    if b"</body>" in body:
        body = body.replace(b"</body>", MIRROR_JS.encode() + b"</body>", 1)
    else:
        body += MIRROR_JS.encode()
    return body


# ---------------------------------------------------------------------------
# Redirects, files, search
# ---------------------------------------------------------------------------

_redirects = {"mtime": None, "map": {}}


def redirects():
    path = os.path.join(HERE, "redirects.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if mtime != _redirects["mtime"]:
        try:
            with open(path, encoding="utf-8") as f:
                _redirects["map"] = json.load(f)
        except (OSError, ValueError):
            _redirects["map"] = {}
        _redirects["mtime"] = mtime
    return _redirects["map"]


def page_file(title_rel):
    path = os.path.join(PAGES, fs_name(title_rel) + ".html")
    if os.path.isfile(path):
        return path
    # MediaWiki titles always start with a capital letter.
    if title_rel[:1].islower():
        path = os.path.join(PAGES, fs_name(title_rel[:1].upper() + title_rel[1:]) + ".html")
        if os.path.isfile(path):
            return path
    return None


def file_page_target(title_rel):
    """'File:Abrams_card.png' -> '/images/6/6d/Abrams_card.png' if saved."""
    if ":" not in title_rel or title_rel.split(":", 1)[0].lower() not in ("file", "image", "media"):
        return None
    name = title_rel.split(":", 1)[1].replace(" ", "_")
    name = name[:1].upper() + name[1:]
    h = hashlib.md5(name.encode("utf-8")).hexdigest()
    rel = f"images/{h[0]}/{h[:2]}/{name}"
    if os.path.isfile(os.path.join(SITE, fs_name(rel))):
        return "/" + quote(rel)
    thumb = largest_thumb(rel)
    return "/" + quote(os.path.relpath(thumb, SITE).replace(os.sep, "/")) if thumb else None


def largest_thumb(rel):
    """Missing images/X/XY/file.png -> the largest saved thumbnail of it."""
    if not rel.startswith("images/") or rel.startswith("images/thumb/"):
        return None
    d = os.path.join(SITE, "images", "thumb", fs_name(rel[len("images/"):]))
    if not os.path.isdir(d):
        return None

    def size(n):
        try:
            return int(n.split("px-")[0])
        except ValueError:
            return 0
    names = [n for n in os.listdir(d) if "px-" in n]
    return os.path.join(d, max(names, key=size)) if names else None


TAG_RE = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>""")
CONTENT_RE = re.compile(r'id="mw-content-text".*?(?=<div[^>]+class="printfooter"|id="catlinks"|$)',
                        re.S)
_index = {"built": 0, "pages": []}


def search_index():
    if time.time() - _index["built"] > 600:
        pages = []
        for root, _, files in os.walk(PAGES):
            for f in files:
                if not f.endswith(".html"):
                    continue
                full = os.path.join(root, f)
                rel = unquote(os.path.relpath(full, PAGES)[:-5].replace(os.sep, "/"))
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                m = CONTENT_RE.search(text)
                part = m.group(0).split(">", 1)[-1] if m else text
                body = html.unescape(TAG_RE.sub(" ", part))
                pages.append((rel, " ".join(body.split())))
        _index.update(built=time.time(), pages=pages)
    return _index["pages"]


def search_page(query):
    q = query.strip()
    ql = q.lower()
    pages = search_index()
    titles = [rel for rel, _ in pages if ql.replace(" ", "_") in rel.lower()]
    titles.sort(key=lambda r: (r.lower() != ql.replace(" ", "_"), len(r)))
    hits = []
    for rel, text in pages:
        at = text.lower().find(ql)
        if at >= 0 and rel not in titles:
            hits.append((rel, text[max(0, at - 80):at + len(q) + 120]))
        if len(hits) >= 100:
            break
    out = [f"<h1>Search: {html.escape(q)}</h1>",
           '<form action="/index.php"><input name="search" value="%s" autofocus>'
           ' <button>Search</button></form>' % html.escape(q, quote=True)]
    if titles:
        out.append("<h2>Page titles</h2><ul>" + "".join(
            f'<li><a href="{url_for(r)}">{html.escape(r.replace("_", " "))}</a></li>'
            for r in titles[:100]) + "</ul>")
    if hits:
        out.append("<h2>Page text</h2><ul>" + "".join(
            f'<li><a href="{url_for(r)}">{html.escape(r.replace("_", " "))}</a>'
            f"<br><small>&hellip;{html.escape(s)}&hellip;</small></li>" for r, s in hits) + "</ul>")
    if not titles and not hits:
        out.append("<p>No matches in the mirror.</p>")
    return simple_page(f"Search: {q}", "\n".join(out))


PAGE_STYLE = ("body{font-family:system-ui,sans-serif;max-width:52rem;margin:2rem auto;"
              "padding:0 1rem;line-height:1.5;color:#222;background:#fafafa}"
              "h1{font-size:1.5rem}li{margin:.3rem 0}a{color:#36c}small{color:#555}"
              "input{font:inherit;padding:.3rem;width:60%}button{font:inherit}")


def simple_page(title, body_html):
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width'>"
            f"<title>{html.escape(title)}</title><style>{PAGE_STYLE}</style></head>"
            f"<body>{body_html}<p><a href='/'>Main Page</a> &middot; "
            "<a href='/_status'>Mirror status</a></p></body></html>").encode("utf-8")


def status_page():
    try:
        with open(os.path.join(HERE, "status.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = "# No status yet\n\nThe mirror hasn't been downloaded here yet."
    out, in_list = [], False
    for line in text.splitlines():
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(line))
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{esc[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("# "):
            out.append(f"<h1>{esc[2:]}</h1>")
        elif line.strip():
            out.append(f"<p>{esc}</p>")
    if in_list:
        out.append("</ul>")
    return simple_page("Mirror status", "\n".join(out))


def not_found_page(rel):
    title = rel.replace("_", " ")
    return simple_page("Not in the mirror",
                       f"<h1>{html.escape(title)}</h1><p>This page isn't in the offline mirror.</p>"
                       f'<p><a href="/index.php?search={quote(title)}">Search the mirror for it</a></p>')


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("MIRROR_QUIET"):
            return
        super().log_message(fmt, *args)

    def send_bytes(self, body, ctype="text/html; charset=utf-8", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_file(self, path):
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        partial = bool(m and (m.group(1) or m.group(2)))
        if partial:
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "max-age=86400")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def route(self):
        parsed = urlparse(self.path)
        rel = unquote(parsed.path).lstrip("/")
        params = parse_qs(parsed.query)

        if rel in ("", "index.php") and not params:
            return self.redirect("/" + MAIN_PAGE)
        if rel.rstrip("/") == "_status":
            return self.send_bytes(status_page())
        if rel in ("index.php", "Special:Search") or rel.startswith("Special:Search/"):
            q = (params.get("search") or [""])[0]
            if rel.startswith("Special:Search/"):
                q = rel.split("/", 1)[1].replace("_", " ")
            if not q and params.get("title"):
                t = params["title"][0].replace(" ", "_")
                if t.startswith("Special:Search"):
                    return self.send_bytes(search_page(""))
                return self.redirect(url_for(t))
            if q and (params.get("go") or params.get("fulltext") is None):
                key = q.strip().replace(" ", "_")
                target = redirects().get(key) or redirects().get(key[:1].upper() + key[1:])
                if page_file(key) or target:
                    return self.redirect(url_for(target or key[:1].upper() + key[1:]))
            return self.send_bytes(search_page(q))

        # Files (images, stylesheets, fonts, videos) at their wiki paths.
        if rel:
            path = os.path.join(SITE, fs_name(rel))
            if os.path.isfile(path):
                return self.send_file(path)
            thumb = largest_thumb(rel)
            if thumb:
                return self.send_file(thumb)

        # Pages.
        page = page_file(rel)
        if page:
            with open(page, "rb") as f:
                body = f.read()
            return self.send_bytes(prepare_page(body, rel))
        target = redirects().get(rel)
        if target:
            return self.redirect(url_for(target))
        target = file_page_target(rel)
        if target:
            return self.redirect(target)
        if rel.startswith(("images/", "resources/", "skins/", "extensions/", "_assets/")):
            return self.send_bytes(b"Not found", "text/plain", 404)
        return self.send_bytes(not_found_page(rel), status=404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"Serving the Deadlock wiki mirror on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
