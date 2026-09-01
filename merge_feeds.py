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

Requires: Python 3.9+ and requests (pip install requests).
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (journal-feed-merger; academic use)"}

ATOM = "{http://www.w3.org/2005/Atom}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
RSS1 = "{http://purl.org/rss/1.0/}"
DC = "{http://purl.org/dc/elements/1.1/}"

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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
    verify = tls != "relaxed"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=verify)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------- output


def sort_key(item):
    dt = parse_date(item.get("date", "")) or parse_date(item.get("first_seen", ""))
    return dt or EPOCH


def build_rss(items, max_items):
    items = sorted(items, key=sort_key, reverse=True)[:max_items]
    now = datetime.now(timezone.utc)

    rss = ET.Element("rss", {"version": "2.0",
                             "xmlns:dc": "http://purl.org/dc/elements/1.1/"})
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
        ET.SubElement(el, "category").text = it["journal"]
        if it.get("summary"):
            ET.SubElement(el, "description").text = it["summary"]

    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


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

    failures = 0
    for j in roster:
        try:
            body = fetch(j["url"], j["tls"])
            items = parse_feed(body, j["name"])
        except Exception as exc:
            failures += 1
            print("  %-10s FAILED  %s" % (j["name"], exc.__class__.__name__))
            continue
        fresh = 0
        for it in items:
            if it["id"] not in store:
                store[it["id"]] = it
                fresh += 1
        print("  %-10s %3d items, %d new" % (j["name"], len(items), fresh))

    os.makedirs(args.out_dir, exist_ok=True)
    with open(store_path, "w", encoding="utf-8") as fh:
        json.dump(sorted(store.values(), key=sort_key, reverse=True), fh,
                  indent=1, ensure_ascii=False)
    with open(merged_path, "w", encoding="utf-8") as fh:
        fh.write(build_rss(list(store.values()), args.max_items))

    print("\n%d new items this run, %d in store, %d feeds failed"
          % (len(store) - before, len(store), failures))
    print("wrote %s and %s" % (merged_path, store_path))


if __name__ == "__main__":
    main()
