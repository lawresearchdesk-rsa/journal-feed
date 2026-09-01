"""feedcheck.py

Reads journals.csv, fetches every non-empty feed_url, and reports whether it
is a working RSS or Atom feed. Run from the folder containing journals.csv:

    python feedcheck.py

Optional: pass a different CSV path as the first argument.
Optional: add --insecure to skip TLS certificate checks for every row
(diagnostic only). Individual rows may instead carry tls=relaxed in the CSV,
which skips verification for that host alone: use it where a host is known to
send an incomplete certificate chain that browsers repair and Python cannot.

Requires: Python 3.9+ and the requests library (pip install requests).
"""

import csv
import re
import sys
import xml.etree.ElementTree as ET

import requests

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
INSECURE = "--insecure" in sys.argv
CSV_PATH = ARGS[0] if ARGS else "journals.csv"
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
TIMEOUT = 20
# A browser-like header; some hosts refuse the default Python identity.
HEADERS = {"User-Agent": "Mozilla/5.0 (feedcheck; academic use)"}

ATOM_NS = "{http://www.w3.org/2005/Atom}"
RDF_NS = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
RSS1_NS = "{http://purl.org/rss/1.0/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


def classify(body: str):
    """Return (kind, item_count, newest_date) or (None, 0, '') if not a feed."""
    try:
        root = ET.fromstring(body.strip())
    except ET.ParseError:
        return None, 0, ""

    tag = root.tag.lower()
    if tag == "rss":
        items = root.findall("./channel/item")
        dates = [i.findtext("pubDate") or "" for i in items]
        return "RSS", len(items), (dates[0] if dates else "")
    if tag == (RDF_NS + "rdf").lower():
        # RSS 1.0 / RDF: items are siblings of <channel>, not children.
        items = root.findall(RSS1_NS + "item") or root.findall("item")
        dates = [i.findtext(DC_NS + "date") or "" for i in items]
        dates = [d for d in dates if d]
        return "RSS 1.0", len(items), (max(dates) if dates else "")
    if tag == ATOM_NS + "feed" or tag.endswith("}feed") or tag == "feed":
        entries = root.findall(ATOM_NS + "entry") or root.findall("entry")
        dates = [
            (e.findtext(ATOM_NS + "updated") or e.findtext(ATOM_NS + "published") or "")
            for e in entries
        ]
        return "Atom", len(entries), (dates[0] if dates else "")
    return None, 0, ""


def check(url: str, tls: str = "strict"):
    verify = not (INSECURE or tls.strip().lower() == "relaxed")
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, verify=verify)
    except requests.RequestException as exc:
        return "ERROR", f"request failed: {exc.__class__.__name__}"

    if r.status_code != 200:
        return "FAIL", f"HTTP {r.status_code}"

    body = r.text
    if re.search(r"<html", body[:2000], re.I) and not re.search(r"<(rss|feed|rdf:RDF)\b", body[:2000], re.I):
        return "FAIL", "HTML page returned, not a feed"

    kind, count, newest = classify(body)
    if kind is None:
        return "FAIL", "not parseable as RSS, RSS 1.0 or Atom"
    if count == 0:
        return "WARN", f"{kind} feed but no items"
    return "OK", f"{kind}, {count} items, newest {newest[:25]}"


def main():
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    if INSECURE:
        print("NOTE: TLS verification disabled for this run")
    print(f"{'journal':<12} {'status':<6} detail")
    print("-" * 72)
    for row in rows:
        url = (row.get("feed_url") or "").strip()
        if not url:
            continue
        tls = (row.get("tls") or "strict")
        status, detail = check(url, tls)
        if status == "OK" and tls.strip().lower() == "relaxed":
            detail += "  [tls=relaxed]"
        print(f"{row['short_name']:<12} {status:<6} {detail}")
        if status != "OK":
            print(f"{'':<12}        {url}")


if __name__ == "__main__":
    main()
