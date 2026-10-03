#!/usr/bin/env python3
"""
organism_dl.py - bulk-download the free audio in the organism.earth library,
person by person.

Pure standard library (Python 3.8+), nothing to pip install.

Examples
--------
  # Pick people from a numbered menu
  python organism_dl.py

  # Just list everyone the library index links to
  python organism_dl.py --list

  # Download one or more people by name (case/spacing insensitive)
  python organism_dl.py "terence mckenna" "alan watts"

  # See what would be downloaded without downloading anything
  python organism_dl.py "terence mckenna" --dry-run

  # Point at a person's page directly if name matching misses it
  python organism_dl.py --url https://www.organism.earth/library/author/terence-mckenna

Files are saved to ./downloads/<Person Name>/. Re-running skips files that
are already complete, so an interrupted run can just be started again.
"""

import argparse
import concurrent.futures as cf
import html
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from html.parser import HTMLParser

BASE_URL = "https://www.organism.earth/library/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 organism-dl/1.0"
)
AUDIO_EXTS = (".mp3", ".m4a", ".m4b", ".ogg", ".oga", ".opus", ".wav", ".flac", ".aac")
# URL path segments that usually mean "this is a person's page"
PERSON_HINTS = ("author", "authors", "people", "person", "speaker", "speakers", "creator", "contributor")
# Links we never want to crawl into
SKIP_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".css", ".js", ".ico",
             ".pdf", ".epub", ".zip", ".mp4", ".webm", ".woff", ".woff2", ".ttf")

AUDIO_URL_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>()\\]+?\.(?:mp3|m4a|m4b|ogg|oga|opus|wav|flac|aac)(?:\?[^\s"'<>()\\]*)?"""
    r"""|(?<=["'(=])/[^\s"'<>()\\]+?\.(?:mp3|m4a|m4b|ogg|oga|opus|wav|flac|aac)(?:\?[^\s"'<>()\\]*)?""",
    re.IGNORECASE,
)

print_lock = threading.Lock()


def log(*a):
    with print_lock:
        print(*a, flush=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self, delay=0.5, retries=3, timeout=30):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._last = 0.0
        self._lock = threading.Lock()
        self._cache = {}

    def _polite_wait(self):
        with self._lock:
            wait = self._last + self.delay - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def open(self, url, headers=None):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        last_err = None
        for attempt in range(self.retries):
            self._polite_wait()
            try:
                return urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                # 404/403/416 won't get better by retrying
                if e.code in (403, 404, 410, 416):
                    raise
                last_err = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
            time.sleep(2 ** attempt)
        raise last_err

    def get_html(self, url):
        if url in self._cache:
            return self._cache[url]
        try:
            with self.open(url) as r:
                ctype = r.headers.get("Content-Type", "")
                final_url = r.geturl()
                if "html" not in ctype and "xml" not in ctype and ctype:
                    self._cache[url] = (final_url, "")
                    return self._cache[url]
                charset = r.headers.get_content_charset() or "utf-8"
                text = r.read().decode(charset, errors="replace")
        except Exception as e:
            log(f"  ! could not fetch {url}: {e}")
            text, final_url = "", url
        self._cache[url] = (final_url, text)
        return self._cache[url]


# --------------------------------------------------------------------------- #
# HTML parsing
# --------------------------------------------------------------------------- #
class LinkParser(HTMLParser):
    """Collects <a href> links (with their text) and any audio-looking URLs
    found in src/href/data-* attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []  # (href, text)
        self.media = set()
        self.title = ""
        self._a_href = None
        self._a_text = []
        self._in_title = False
        self._h1 = None
        self._in_h1 = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for k, v in attrs.items():
            if v and (k in ("src", "href", "content") or k.startswith("data-")):
                if urllib.parse.urlsplit(v).path.lower().endswith(AUDIO_EXTS):
                    self.media.add(v)
        if tag == "a" and attrs.get("href"):
            self._a_href = attrs["href"]
            self._a_text = []
        elif tag == "title":
            self._in_title = True
        elif tag == "h1" and self._h1 is None:
            self._in_h1 = True
            self._h1 = ""

    def handle_endtag(self, tag):
        if tag == "a" and self._a_href is not None:
            self.links.append((self._a_href, " ".join("".join(self._a_text).split())))
            self._a_href = None
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._a_href is not None:
            self._a_text.append(data)
        if self._in_title:
            self.title += data
        if self._in_h1:
            self._h1 += data

    @property
    def heading(self):
        return " ".join((self._h1 or self.title or "").split())


def parse(url, text):
    p = LinkParser()
    try:
        p.feed(text)
    except Exception:
        pass
    links = []
    for href, label in p.links:
        absu = normalize(urllib.parse.urljoin(url, html.unescape(href)))
        if absu:
            links.append((absu, label))
    media = {normalize(urllib.parse.urljoin(url, html.unescape(m))) for m in p.media}
    # Also catch audio URLs hidden in inline JS / JSON players
    for m in AUDIO_URL_RE.findall(text.replace("\\/", "/")):
        if m.startswith("//"):
            m = "https:" + m
        media.add(normalize(urllib.parse.urljoin(url, html.unescape(m))))
    media.discard(None)
    return p, links, media


def normalize(u):
    try:
        s = urllib.parse.urlsplit(u)
    except ValueError:
        return None
    if s.scheme not in ("http", "https"):
        return None
    # drop #fragments, keep the query (pagination often lives there)
    return urllib.parse.urlunsplit((s.scheme, s.netloc.lower(), s.path or "/", s.query, ""))


def same_site(a, b):
    strip = lambda h: h[4:] if h.startswith("www.") else h
    return strip(urllib.parse.urlsplit(a).netloc) == strip(urllib.parse.urlsplit(b).netloc)


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def safe_name(s, maxlen=150):
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    return (s or "untitled")[:maxlen]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def is_person_url(url):
    segs = [x for x in urllib.parse.urlsplit(url).path.lower().split("/") if x]
    return any(h in segs for h in PERSON_HINTS) and segs[-1] not in PERSON_HINTS


def discover_people(fetcher, start=BASE_URL, max_index_pages=60):
    """Return {person_url: display_name} from the library index (and its
    pagination / an 'authors' index page if there is one)."""
    people = {}
    seen = set()
    queue = deque([normalize(start)])
    while queue and len(seen) < max_index_pages:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        final, text = fetcher.get_html(url)
        if not text:
            continue
        _, links, _ = parse(final, text)
        for href, label in links:
            if not same_site(href, start):
                continue
            path = urllib.parse.urlsplit(href).path.lower()
            if is_person_url(href):
                name = label or path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
                # prefer the longest human-looking label seen for this URL
                if len(name) > len(people.get(href, "")) and len(name) < 80:
                    people[href] = name
                elif href not in people:
                    people[href] = name
            # follow index pages: pagination and "all authors"-type listings
            segs = [x for x in path.split("/") if x]
            if href not in seen and (
                (segs and segs[-1] in PERSON_HINTS)
                or (href.split("?")[0] == url.split("?")[0] and "page" in href.lower())
                or re.search(r"/page/\d+/?$", path)
            ):
                queue.append(href)
    return people


def match_people(people, queries):
    """Map each query to matching person URLs by name or URL slug."""
    out = {}
    for q in queries:
        qs = slug(q)
        hits = [u for u, n in people.items() if qs == slug(n)]
        if not hits:
            hits = [u for u, n in people.items()
                    if qs in slug(n) or qs in slug(urllib.parse.urlsplit(u).path)]
        if not hits:
            log(f"  ! no person matching '{q}' found on the index page")
        for u in hits:
            out[u] = people[u]
    return out


# --------------------------------------------------------------------------- #
# Crawl a person's pages for audio
# --------------------------------------------------------------------------- #
def crawl_person(fetcher, person_url, max_depth=2, max_pages=800):
    """Breadth-first crawl starting at a person's page.

    Depth 0 = the person page (plus its pagination),
    depth 1 = the talks/documents it links to, depth 2 = one hop further
    (handles 'document -> audio page -> file' layouts). Stays inside /library/
    and never wanders onto another person's page.
    """
    root = normalize(person_url)
    library_prefix = "/library/"
    found = {}  # audio_url -> (title, page_url)
    seen = set()
    queue = deque([(root, 0)])
    pages = 0
    person_path = urllib.parse.urlsplit(root).path.rstrip("/")

    while queue and pages < max_pages:
        url, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        final, text = fetcher.get_html(url)
        pages += 1
        if not text:
            continue
        p, links, media = parse(final, text)
        title = p.heading
        for m in media:
            if m not in found:
                found[m] = (title, final)
                log(f"    + {os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(m).path))}")

        for href, label in links:
            path = urllib.parse.urlsplit(href).path
            low = path.lower()
            if low.endswith(AUDIO_EXTS):
                if href not in found:
                    found[href] = (label or title, final)
                    log(f"    + {os.path.basename(urllib.parse.unquote(path))}")
                continue
            if href in seen or not same_site(href, root) or low.endswith(SKIP_EXTS):
                continue
            is_pagination = path.rstrip("/").startswith(person_path) or (
                href.split("?")[0] == final.split("?")[0] and "page" in href.lower())
            if is_pagination:
                queue.append((href, depth))  # same depth: still the listing
            elif depth < max_depth and library_prefix in low and not is_person_url(href):
                queue.append((href, depth + 1))
    return found


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def filename_for(url, title):
    base = os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(url).path))
    stem, ext = os.path.splitext(base)
    # Opaque names like 'a8f3c1.mp3' -> use the page title instead
    if title and (len(stem) < 4 or re.fullmatch(r"[0-9a-f\-_]{8,}", stem.lower())):
        return safe_name(f"{title}{ext}")
    return safe_name(base)


def download(fetcher, url, dest):
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    try:
        r = fetcher.open(url, headers)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:  # already fully downloaded into .part
            os.replace(tmp, dest)
            return "done"
        raise
    with r:
        total = r.headers.get("Content-Length")
        mode = "ab" if (have and r.status == 206) else "wb"
        if mode == "wb":
            have = 0
        total = int(total) + have if total else None
        if total and os.path.exists(dest) and os.path.getsize(dest) == total:
            return "skip"
        with open(tmp, mode) as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
    if total and os.path.getsize(tmp) < total:
        raise IOError(f"incomplete ({os.path.getsize(tmp)}/{total} bytes)")
    os.replace(tmp, dest)
    return "done"


def download_all(fetcher, items, outdir, workers):
    os.makedirs(outdir, exist_ok=True)
    used = set()
    jobs = []
    for url, (title, _page) in sorted(items.items()):
        name = filename_for(url, title)
        stem, ext = os.path.splitext(name)
        n = 2
        while name.lower() in used:
            name = f"{stem} ({n}){ext}"
            n += 1
        used.add(name.lower())
        jobs.append((url, os.path.join(outdir, name)))

    stats = {"done": 0, "skip": 0, "fail": 0}

    def work(job):
        url, dest = job
        if os.path.exists(dest) and os.path.getsize(dest) > 0 and not os.path.exists(dest + ".part"):
            return "skip", dest, None
        try:
            return download(fetcher, url, dest), dest, None
        except Exception as e:
            return "fail", dest, f"{url}: {e}"

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (status, dest, err) in enumerate(ex.map(work, jobs), 1):
            stats[status] += 1
            tag = {"done": "OK  ", "skip": "SKIP", "fail": "FAIL"}[status]
            log(f"  [{i}/{len(jobs)}] {tag} {os.path.basename(dest)}" + (f"  ({err})" if err else ""))
    return stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def pick_interactively(people):
    names = sorted(people.items(), key=lambda kv: kv[1].lower())
    for i, (_u, n) in enumerate(names, 1):
        print(f"  {i:3d}. {n}")
    raw = input("\nNumbers or names to download (e.g. 1,4,7-9 or 'terence mckenna'), blank to quit: ").strip()
    if not raw:
        return {}
    chosen = {}
    text_queries = []
    for part in [x.strip() for x in raw.split(",") if x.strip()]:
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            for k in range(int(m.group(1)), int(m.group(2)) + 1):
                if 1 <= k <= len(names):
                    chosen[names[k - 1][0]] = names[k - 1][1]
        elif part.isdigit() and 1 <= int(part) <= len(names):
            chosen[names[int(part) - 1][0]] = names[int(part) - 1][1]
        else:
            text_queries.append(part)
    chosen.update(match_people(people, text_queries))
    return chosen


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bulk-download audio from the organism.earth library by person.")
    ap.add_argument("people", nargs="*", help="person names to download, e.g. \"terence mckenna\"")
    ap.add_argument("--url", action="append", default=[], help="a person's page URL (repeatable)")
    ap.add_argument("--list", action="store_true", help="list people found on the library index and exit")
    ap.add_argument("--dry-run", action="store_true", help="find audio files but don't download them")
    ap.add_argument("-o", "--out", default="downloads", help="output folder (default: ./downloads)")
    ap.add_argument("--base", default=BASE_URL, help=f"library index URL (default: {BASE_URL})")
    ap.add_argument("--depth", type=int, default=2, help="how many links deep to follow from a person page (default 2)")
    ap.add_argument("--workers", type=int, default=3, help="parallel downloads (default 3)")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests (default 0.5)")
    args = ap.parse_args(argv)

    fetcher = Fetcher(delay=args.delay)
    targets = {}

    for u in args.url:
        final, text = fetcher.get_html(normalize(u))
        p, _, _ = parse(final, text)
        targets[normalize(u)] = p.heading.split("|")[0].split(" - ")[0].strip() or u.rstrip("/").rsplit("/", 1)[-1]

    if args.list or args.people or not args.url:
        log(f"Reading library index {args.base} ...")
        people = discover_people(fetcher, args.base)
        if not people and (args.list or not args.url):
            log("No person links found on the index. The site layout may have changed;\n"
                "open a person's page in your browser and pass it with --url instead.")
            return 1
        if args.list:
            for u, n in sorted(people.items(), key=lambda kv: kv[1].lower()):
                print(f"{n:40s} {u}")
            log(f"\n{len(people)} people.")
            return 0
        if args.people:
            targets.update(match_people(people, args.people))
        elif not args.url:
            targets.update(pick_interactively(people))

    if not targets:
        log("Nothing selected.")
        return 1

    grand = {"done": 0, "skip": 0, "fail": 0, "found": 0}
    for url, name in targets.items():
        log(f"\n== {name}  ({url})")
        log("  scanning for audio ...")
        items = crawl_person(fetcher, url, max_depth=args.depth)
        grand["found"] += len(items)
        log(f"  {len(items)} audio file(s) found")
        if not items:
            continue
        if args.dry_run:
            for a, (t, page) in sorted(items.items()):
                print(f"    {filename_for(a, t):60s} {a}")
            continue
        stats = download_all(fetcher, items, os.path.join(args.out, safe_name(name)), args.workers)
        for k in stats:
            grand[k] += stats[k]

    log(f"\nFinished: {grand['found']} found, {grand['done']} downloaded, "
        f"{grand['skip']} already had, {grand['fail']} failed.")
    return 1 if grand["fail"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted - run the same command again to resume.")
        sys.exit(130)
