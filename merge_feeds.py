"""merge_feeds.py

Fetches every feed listed in journals.csv, normalises the items into a common
shape, merges them with anything seen on previous runs, and writes a single
RSS 2.0 file that Zotero (or any reader) can subscribe to.

    python merge_feeds.py

Outputs, both written next to this script unless --out-dir is given:
    merged.xml   the combined feed
    store.json   every item ever seen, so entries survive after a source
                 feed drops them (source feeds usually show only the newest
                 issue, so without this the merged feed would forget things)

Options:
    --csv PATH        journal roster (default journals.csv)
    --out-dir PATH    where to write merged.xml and store.json (default .)
    --max-items N     how many items the merged feed carries (default 200)
    --self-test       parse built-in samples and exit; makes no network calls
    --crossref        after the feeds, query Crossref by ISSN for any journal
                      whose feed is absent or failed. Crossref is an API, not
                      a publisher website, so it answers requests from GitHub
                      that Taylor & Francis and UNISA Press refuse.
    --crossref-days N how far back to ask Crossref (default 400)
    --crossref-all    query Crossref for every journal, not just the gaps.
                      Slower, and duplicates are harmless, but useful once to
                      see which journals Crossref actually covers.
    --publish         after merging, upload the output to GitHub so the
                      published feed URL updates. Needs a token: see below.

Publishing:
    Set two environment variables (or pass --repo):
        GITHUB_TOKEN  a fine-grained personal access token with Contents:
                      read and write on the one repository, and nothing else
        GITHUB_REPO   e.g. lawresearchdesk-rsa/journal-feed
    The token is read from the environment only. Do not put it in this file
    and do not commit it anywhere.

Requires: Python 3.9+ and requests (pip install requests).
"""

import argparse
import base64
import csv
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime

import time

import requests
import urllib3


class FetchError(Exception):
    """A feed could not be retrieved; the message says why."""

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 30
RETRIES = 3          # attempts per feed
BACKOFF = 4          # seconds, multiplied by attempt number
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-ZA,en;q=0.9",
}

ATOM = "{http://www.w3.org/2005/Atom}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
RSS1 = "{http://purl.org/rss/1.0/}"
DC = "{http://purl.org/dc/elements/1.1/}"
PRISM = "{http://prismstandard.org/namespaces/basic/2.0/}"

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def clean_doi(raw):
    """Normalise anything DOI-shaped to a bare 10.xxxx/yyyy string."""
    if not raw:
        return ""
    raw = raw.strip()
    for prefix in ("doi:", "DOI:", "https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    raw = raw.strip()
    return raw if raw.startswith("10.") else ""


def title_key(journal, title):
    """A loose key for spotting the same article arriving by two routes.

    The feed gives a publisher link, Crossref gives a DOI, so the primary id
    differs even when the article is identical. Comparing normalised titles
    within a journal catches that.
    """
    flat = "".join(c.lower() for c in (title or "") if c.isalnum())
    return journal + "|" + flat[:120]


# ---------------------------------------------------------------- dates


def parse_date(raw: str):
    """Return a timezone-aware datetime, or None if the string is unusable."""
    if not raw:
        return None
    raw = raw.strip()
    try:  # RFC 822, as used by RSS 2.0 pubDate
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:  # ISO 8601, as used by Atom and RSS 1.0 dc:date
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def pages_of(el):
    """PRISM splits pagination across two elements; join them."""
    start = text_of(el, PRISM + "startingPage")
    end = text_of(el, PRISM + "endingPage")
    if start and end and start != end:
        return "%s-%s" % (start, end)
    return start or end or ""


def text_of(el, *paths):
    """First non-empty text found at any of the given child paths."""
    if el is None:
        return ""
    for p in paths:
        found = el.findtext(p)
        if found and found.strip():
            return found.strip()
    return ""


# ---------------------------------------------------------------- parsing


def parse_feed(body: str, journal: str):
    """Turn a feed document of any of the three formats into a list of dicts."""
    try:
        root = ET.fromstring(body.strip())
    except ET.ParseError:
        return []

    tag = root.tag
    items = []

    if tag.lower() == "rss":
        for it in root.findall("./channel/item"):
            items.append(
                {
                    "title": text_of(it, "title"),
                    "link": text_of(it, "link", "guid"),
                    "date": text_of(it, "pubDate", DC + "date"),
                    "author": text_of(it, DC + "creator", "author"),
                    "summary": text_of(it, "description"),
                    "doi": clean_doi(text_of(it, PRISM + "doi", DC + "identifier")),
                    "publication": text_of(it, PRISM + "publicationName", DC + "source"),
                    "volume": text_of(it, PRISM + "volume"),
                    "issue": text_of(it, PRISM + "number"),
                    "pages": pages_of(it),
                }
            )

    elif tag == RDF + "RDF":
        # RSS 1.0: items are siblings of <channel>, not children of it.
        for it in root.findall(RSS1 + "item") or root.findall("item"):
            items.append(
                {
                    "title": text_of(it, RSS1 + "title", "title", DC + "title"),
                    "link": text_of(it, RSS1 + "link", "link"),
                    "date": text_of(it, DC + "date"),
                    "author": text_of(it, DC + "creator"),
                    "summary": text_of(it, RSS1 + "description", "description"),
                    "doi": clean_doi(text_of(it, PRISM + "doi", DC + "identifier")),
                    "publication": text_of(it, PRISM + "publicationName", DC + "source"),
                    "volume": text_of(it, PRISM + "volume"),
                    "issue": text_of(it, PRISM + "number"),
                    "pages": pages_of(it),
                }
            )

    elif tag == ATOM + "feed":
        for it in root.findall(ATOM + "entry"):
            link = ""
            for ln in it.findall(ATOM + "link"):
                rel = ln.get("rel", "alternate")
                if rel == "alternate" and ln.get("href"):
                    link = ln.get("href")
                    break
            author = ""
            au = it.find(ATOM + "author")
            if au is not None:
                author = text_of(au, ATOM + "name")
            items.append(
                {
                    "title": text_of(it, ATOM + "title"),
                    "link": link or text_of(it, ATOM + "id"),
                    "date": text_of(it, ATOM + "updated", ATOM + "published"),
                    "author": author,
                    "summary": text_of(it, ATOM + "summary", ATOM + "content"),
                    "doi": clean_doi(text_of(it, PRISM + "doi", DC + "identifier")),
                    "publication": text_of(it, PRISM + "publicationName"),
                    "volume": text_of(it, PRISM + "volume"),
                    "issue": text_of(it, PRISM + "number"),
                    "pages": pages_of(it),
                }
            )

    out = []
    for raw in items:
        if not raw["title"] and not raw["link"]:
            continue
        dt = parse_date(raw["date"])
        key = raw["link"] or (journal + "|" + raw["title"])
        out.append(
            {
                "id": hashlib.sha1(key.encode("utf-8")).hexdigest(),
                "journal": journal,
                "title": raw["title"] or "(untitled)",
                "link": raw["link"],
                "author": raw["author"],
                "summary": raw["summary"],
                "doi": raw.get("doi", ""),
                "publication": raw.get("publication", ""),
                "volume": raw.get("volume", ""),
                "issue": raw.get("issue", ""),
                "pages": raw.get("pages", ""),
                "date": dt.astimezone(timezone.utc).isoformat() if dt else "",
                "first_seen": datetime.now(timezone.utc).isoformat(),
            }
        )
    return out


# ---------------------------------------------------------------- roster


def load_roster(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    live = []
    for r in rows:
        url = (r.get("feed_url") or "").strip()
        status = (r.get("feed_status") or "").strip().lower()
        if url and status.startswith("confirmed"):
            live.append(
                {
                    "name": r["short_name"],
                    "title": r.get("title", r["short_name"]),
                    "url": url,
                    "tls": (r.get("tls") or "strict").strip().lower(),
                }
            )
    return live


def fetch(url, tls):
    """Fetch a feed, retrying transient failures.

    Raises FetchError carrying a human-readable reason: an HTTP status code
    where the server answered, or the underlying network error where it did
    not. Knowing which is which is the whole diagnostic value.
    """
    verify = tls != "relaxed"
    last = "unknown"
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=verify)
        except requests.exceptions.SSLError as exc:
            last = "SSL: %s" % str(exc)[:120]
        except requests.exceptions.ConnectTimeout:
            last = "connect timeout after %ds" % TIMEOUT
        except requests.exceptions.ReadTimeout:
            last = "read timeout after %ds" % TIMEOUT
        except requests.exceptions.ConnectionError as exc:
            last = "connection refused or unreachable: %s" % str(exc)[:120]
        except requests.RequestException as exc:
            last = "%s: %s" % (exc.__class__.__name__, str(exc)[:120])
        else:
            if r.status_code == 200:
                return r.text
            last = "HTTP %d %s" % (r.status_code, r.reason or "")
            if r.status_code in (403, 401, 404, 451):
                break  # a refusal will not improve on retry
        if attempt < RETRIES:
            time.sleep(BACKOFF * attempt)
    raise FetchError(last)


# ---------------------------------------------------------------- output


def sort_key(item):
    dt = parse_date(item.get("date", "")) or parse_date(item.get("first_seen", ""))
    return dt or EPOCH


def build_rss(items, max_items):
    items = sorted(items, key=sort_key, reverse=True)[:max_items]
    now = datetime.now(timezone.utc)

    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:dc": "http://purl.org/dc/elements/1.1/",
        # PRISM carries the DOI and pagination. Zotero reads these when it
        # saves a feed item, and the DOI is what lets it find an open-access
        # copy for articles whose own publisher page has no free PDF.
        "xmlns:prism": "http://prismstandard.org/namespaces/basic/2.0/",
    })
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "South African law journals (merged)"
    ET.SubElement(ch, "link").text = "https://example.invalid/merged.xml"
    ET.SubElement(ch, "description").text = (
        "Combined tables of contents from the journals listed in journals.csv."
    )
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(now)

    for it in items:
        el = ET.SubElement(ch, "item")
        ET.SubElement(el, "title").text = "[%s] %s" % (it["journal"], it["title"])
        if it["link"]:
            ET.SubElement(el, "link").text = it["link"]
        g = ET.SubElement(el, "guid")
        g.text = it["link"] or it["id"]
        g.set("isPermaLink", "true" if it["link"] else "false")
        dt = parse_date(it.get("date", ""))
        if dt:
            ET.SubElement(el, "pubDate").text = format_datetime(dt)
        if it.get("author"):
            ET.SubElement(el, "dc:creator").text = it["author"]
        doi = it.get("doi", "")
        if doi:
            ET.SubElement(el, "prism:doi").text = doi
            ET.SubElement(el, "dc:identifier").text = "doi:" + doi
        if it.get("publication"):
            ET.SubElement(el, "prism:publicationName").text = it["publication"]
        if it.get("volume"):
            ET.SubElement(el, "prism:volume").text = it["volume"]
        if it.get("issue"):
            ET.SubElement(el, "prism:number").text = it["issue"]
        pages = it.get("pages", "")
        if pages:
            bits = pages.replace("--", "-").split("-")
            ET.SubElement(el, "prism:startingPage").text = bits[0].strip()
            if len(bits) > 1 and bits[1].strip():
                ET.SubElement(el, "prism:endingPage").text = bits[1].strip()
        ET.SubElement(el, "category").text = it["journal"]
        if it.get("summary"):
            ET.SubElement(el, "description").text = it["summary"]

    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


# ---------------------------------------------------------------- crossref


CROSSREF = "https://api.crossref.org/journals/%s/works"
# Crossref asks that heavy users identify themselves; doing so also puts the
# request in their faster "polite" pool. Any working address is acceptable.
CROSSREF_UA = "journal-feed-merger/1.0 (mailto:noreply@example.invalid)"


def crossref_items(issn, journal, days):
    """Fetch recent works for one ISSN. Returns (items, note)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    params = {
        "filter": "from-pub-date:%s,type:journal-article" % since,
        "rows": "100",
        "sort": "published",
        "order": "desc",
        "select": ("DOI,title,author,issued,published,abstract,"
                   "container-title,volume,issue,page,ISSN"),
    }
    try:
        r = requests.get(CROSSREF % issn, params=params,
                         headers={"User-Agent": CROSSREF_UA}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return [], "request failed: %s" % exc.__class__.__name__
    if r.status_code == 404:
        return [], "ISSN not known to Crossref"
    if r.status_code != 200:
        return [], "HTTP %d %s" % (r.status_code, r.reason)

    try:
        works = r.json()["message"]["items"]
    except (ValueError, KeyError):
        return [], "unexpected response format"

    out = []
    for w in works:
        title_list = w.get("title") or []
        title = title_list[0] if title_list else ""
        doi = w.get("DOI", "")
        if not title:
            continue  # front matter and errata often arrive titleless

        parts = ((w.get("published") or w.get("issued") or {})
                 .get("date-parts") or [[]])[0]
        date = ""
        if parts:
            y = parts[0]
            mo = parts[1] if len(parts) > 1 else 1
            d = parts[2] if len(parts) > 2 else 1
            try:
                date = datetime(y, mo, d, tzinfo=timezone.utc).isoformat()
            except (TypeError, ValueError):
                date = ""

        names = []
        for a in (w.get("author") or [])[:8]:
            nm = " ".join(x for x in (a.get("given"), a.get("family")) if x)
            if nm:
                names.append(nm)

        link = "https://doi.org/" + doi if doi else ""
        key = link or (journal + "|" + title)
        containers = w.get("container-title") or []
        out.append({
            "id": hashlib.sha1(key.encode("utf-8")).hexdigest(),
            "journal": journal,
            "title": title or "(untitled)",
            "link": link,
            "author": ", ".join(names),
            "summary": (w.get("abstract") or "")[:2000],
            "doi": doi,
            "publication": containers[0] if containers else "",
            "volume": str(w.get("volume") or ""),
            "issue": str(w.get("issue") or ""),
            "pages": str(w.get("page") or ""),
            "date": date,
            "first_seen": datetime.now(timezone.utc).isoformat(),
        })
    return out, "ok"


def crossref_targets(csv_path, failed_names, everything):
    """Rows worth asking Crossref about, with every ISSN they carry.

    Journals often deposit under their print ISSN rather than the online one,
    or vice versa, so both are worth trying before concluding that Crossref
    does not hold the journal.
    """
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        name = r["short_name"]
        has_feed = bool((r.get("feed_url") or "").strip()) and \
            (r.get("feed_status") or "").lower().startswith("confirmed")
        gap = (not has_feed) or (name in failed_names)
        if not (everything or gap):
            continue
        issns = []
        for field in ("issn_online", "issn_print"):
            v = (r.get(field) or "").strip()
            if v and v not in issns:
                issns.append(v)
        if issns:
            out.append((name, issns))
    return out


# ---------------------------------------------------------------- publishing


API = "https://api.github.com"


def publish_file(repo, path, content, token, branch="main"):
    """Create or update one file in a GitHub repository via the API.

    Returns a short status string. Uses the Contents API, which needs the
    file's current sha to replace it, so we look that up first.
    """
    url = "%s/repos/%s/contents/%s" % (API, repo, path)
    head = {
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    sha = None
    r = requests.get(url, headers=head, params={"ref": branch}, timeout=TIMEOUT)
    if r.status_code == 200:
        sha = r.json().get("sha")
    elif r.status_code not in (404,):
        return "lookup failed: HTTP %d %s" % (r.status_code, r.reason)

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    body = {
        "message": "Update %s (%s)" % (path, datetime.now(timezone.utc).date()),
        "content": encoded,
        "branch": branch,
    }
    if sha:
        body["sha"] = sha

    r = requests.put(url, headers=head, json=body, timeout=TIMEOUT)
    if r.status_code in (200, 201):
        return "uploaded"
    return "upload failed: HTTP %d %s %s" % (r.status_code, r.reason, r.text[:200])


# ---------------------------------------------------------------- self-test

SAMPLE_RSS2 = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Alpha</title><link>https://e.invalid/a</link>
<pubDate>Mon, 31 Aug 2026 10:00:00 GMT</pubDate></item></channel></rss>"""

SAMPLE_RDF = """<?xml version="1.0"?><rdf:RDF
 xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="x"><title>T</title></channel>
<item rdf:about="https://e.invalid/b"><title>Beta</title>
<link>https://e.invalid/b</link><dc:date>2026-06-01T07:00:00Z</dc:date>
<dc:creator>G Brink</dc:creator></item></rdf:RDF>"""

SAMPLE_ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Gamma</title><link rel="alternate" href="https://e.invalid/c"/>
<updated>2026-07-15T00:00:00Z</updated>
<author><name>A Author</name></author></entry></feed>"""


def self_test():
    ok = True
    for label, doc, expect in (
        ("RSS 2.0", SAMPLE_RSS2, "Alpha"),
        ("RSS 1.0", SAMPLE_RDF, "Beta"),
        ("Atom", SAMPLE_ATOM, "Gamma"),
    ):
        got = parse_feed(doc, "TEST")
        good = len(got) == 1 and got[0]["title"] == expect and got[0]["date"]
        print("%-8s parsed=%d title=%s date=%s %s"
              % (label, len(got), got[0]["title"] if got else "-",
                 got[0]["date"] if got else "-", "OK" if good else "FAILED"))
        ok = ok and good

    merged = []
    for doc in (SAMPLE_RSS2, SAMPLE_RDF, SAMPLE_ATOM):
        merged.extend(parse_feed(doc, "TEST"))
    merged.extend(parse_feed(SAMPLE_RSS2, "TEST"))  # duplicate on purpose
    by_id = {i["id"]: i for i in merged}
    dedup_ok = len(by_id) == 3
    print("dedup   %d unique from %d items %s"
          % (len(by_id), len(merged), "OK" if dedup_ok else "FAILED"))

    xml = build_rss(list(by_id.values()), 200)
    order = [e.findtext("title") for e in ET.fromstring(xml).findall("./channel/item")]
    order_ok = order == ["[TEST] Alpha", "[TEST] Gamma", "[TEST] Beta"]
    print("output  %d items, newest first: %s"
          % (len(order), "OK" if order_ok else "FAILED " + str(order)))
    return ok and dedup_ok and order_ok


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="journals.csv")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--max-items", type=int, default=200)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--crossref", action="store_true")
    ap.add_argument("--crossref-days", type=int, default=400)
    ap.add_argument("--crossref-all", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPO", ""))
    ap.add_argument("--branch", default="main")
    ap.add_argument("--remote-dir", default="docs")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    store_path = os.path.join(args.out_dir, "store.json")
    merged_path = os.path.join(args.out_dir, "merged.xml")

    store = {}
    if os.path.exists(store_path):
        with open(store_path, encoding="utf-8") as fh:
            store = {i["id"]: i for i in json.load(fh)}
    before = len(store)

    roster = load_roster(args.csv)
    print("%d confirmed feeds in %s" % (len(roster), args.csv))

    failures = []
    seen_titles = set(title_key(i["journal"], i["title"]) for i in store.values())
    for j in roster:
        try:
            body = fetch(j["url"], j["tls"])
            items = parse_feed(body, j["name"])
        except FetchError as exc:
            failures.append((j["name"], str(exc)))
            print("  %-10s FAILED  %s" % (j["name"], exc))
            continue
        except Exception as exc:
            failures.append((j["name"], exc.__class__.__name__))
            print("  %-10s FAILED  %s" % (j["name"], exc.__class__.__name__))
            continue
        fresh = 0
        for it in items:
            tk = title_key(it["journal"], it["title"])
            if it["id"] in store or tk in seen_titles:
                continue
            store[it["id"]] = it
            seen_titles.add(tk)
            fresh += 1
        print("  %-10s %3d items, %d new" % (j["name"], len(items), fresh))

    if args.crossref or args.crossref_all:
        failed_names = set(n for n, _ in failures)
        targets = crossref_targets(args.csv, failed_names, args.crossref_all)
        print("\nCrossref: querying %d journals (%d days back)"
              % (len(targets), args.crossref_days))
        for name, issns in targets:
            items, note, issn = [], "no ISSN recorded", ""
            for candidate in issns:
                items, note = crossref_items(candidate, name, args.crossref_days)
                issn = candidate
                if note == "ok" and items:
                    break  # this ISSN works; no need to try the other
            fresh = 0
            dupes = 0
            for it in items:
                tk = title_key(it["journal"], it["title"])
                if it["id"] in store or tk in seen_titles:
                    dupes += 1
                    continue
                store[it["id"]] = it
                seen_titles.add(tk)
                fresh += 1
            if note != "ok":
                print("  %-10s %-14s %s" % (name, issn, note))
            else:
                print("  %-10s %-14s %3d works, %d new, %d already held"
                      % (name, issn, len(items), fresh, dupes))
                if name in failed_names and items:
                    failures = [f for f in failures if f[0] != name]

    os.makedirs(args.out_dir, exist_ok=True)
    with open(store_path, "w", encoding="utf-8") as fh:
        json.dump(sorted(store.values(), key=sort_key, reverse=True), fh,
                  indent=1, ensure_ascii=False)
    with open(merged_path, "w", encoding="utf-8") as fh:
        fh.write(build_rss(list(store.values()), args.max_items))

    print("\n%d new items this run, %d in store, %d of %d feeds unrecovered"
          % (len(store) - before, len(store), len(failures), len(roster)))
    if failures:
        print("failed feeds:")
        for name, why in failures:
            print("  %-10s %s" % (name, why))
    print("wrote %s and %s" % (merged_path, store_path))

    if args.publish:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token or not args.repo:
            print("publish skipped: set GITHUB_TOKEN and GITHUB_REPO first")
            return
        for local, remote in (
            (merged_path, args.remote_dir + "/merged.xml"),
            (store_path, args.remote_dir + "/store.json"),
        ):
            with open(local, encoding="utf-8") as fh:
                body = fh.read()
            print("  %-18s %s" % (remote, publish_file(
                args.repo, remote, body, token, args.branch)))


if __name__ == "__main__":
    main()
