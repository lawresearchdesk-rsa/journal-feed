# Merged journal feed: setup note

A single RSS feed combining new articles from South African and Africa-focused
law journals. It rebuilds itself daily on GitHub and needs nothing installed by
anyone who subscribes to it.

    https://lawresearchdesk-rsa.github.io/journal-feed/merged.xml

## What the pieces are

| File | Job |
|---|---|
| `journals.csv` | The roster. Every other file reads it and nothing else. One row per journal. |
| `feedcheck.py` | Diagnostic. Tests every `feed_url` and reports what came back. Run it when a journal changes host or a feed goes quiet. |
| `merge_feeds.py` | The merger. Fetches the feeds, fills the gaps from Crossref, and writes one combined RSS file. |
| `.github/workflows/merge-feeds.yml` | The scheduler. Runs the merger every morning and commits the result. |
| `docs/merged.xml` | The output. Served by GitHub Pages at the address above. |
| `docs/store.json` | Every item ever seen. See below. |

## How it collects

Two layers, in order.

**Feeds.** Most journals publish an RSS or Atom feed. Three formats turn up in
practice and the merger reads all three: RSS 2.0, Atom, and RSS 1.0 (which is
RDF, and which Sabinet and Taylor & Francis both use). A feed is the better
source where it exists, since it appears within hours of publication.

**Crossref.** For journals with no feed, and for any whose feed fails, the
merger queries `api.crossref.org` by ISSN. Crossref holds metadata deposited by
the publishers themselves, and being an API rather than a website it answers
requests from anywhere. It lags publication by days to weeks, so it is the
fallback rather than the first choice. Where a journal has two ISSNs, both are
tried before concluding Crossref does not hold it.

An article arriving by both routes is stored once: the merger compares
normalised titles within a journal, because the feed supplies a publisher link
and Crossref a DOI, so the two copies have different identifiers.

## Why there is a store

Most journal feeds show only the current issue. If the merged feed were rebuilt
from scratch each run, an article would vanish as soon as the next issue
appeared, possibly before you had read it. `store.json` accumulates everything
ever seen, so the merged feed has a memory. `--max-items` decides how many of
the newest are published; the rest stay in the store.

## The status vocabulary in journals.csv

`feed_status` decides how each journal is treated. Four values are in use:

| Value | Meaning |
|---|---|
| `confirmed` | Feed works from anywhere. 15 journals. |
| `confirmed-local-only` | Feed works from a South African connection but is refused from GitHub's datacentre. Collected via Crossref instead. 4 journals: SAJHR, SAPL, CILSA, JLSD. |
| `crossref` | No feed exists; collected via Crossref by ISSN. 3 journals: Fundamina, AJIC, AJLS. |
| `unreachable` | No feed, and the ISSN is not known to Crossref. Not collected. 3 journals: THRHR, Speculum Juris, Annual Survey. |

The merger attempts any row whose status begins with `confirmed`, which includes
the local-only four. **Their failures in the log are expected, not faults**:
Taylor & Francis returns HTTP 403 to datacentre traffic, and
`unisapressjournals.co.za` drops the connection outright. Crossref then recovers
all four in the same run. Leaving them in place means they would succeed
immediately if the merger were ever run from a South African machine.

One further wrinkle: two UFS journals carry `tls=relaxed`. That host sends an
incomplete certificate chain, which browsers repair silently and Python cannot,
so verification is skipped for those two hosts alone.

## Standing it up from scratch

Only needed if rebuilding on another account.

1. **Create a repository**, public (GitHub Pages on private repositories needs a
   paid plan). Nothing here is sensitive.
2. **Add the files.** `journals.csv`, `feedcheck.py` and `merge_feeds.py` at the
   top level. The workflow must be at exactly `.github/workflows/merge-feeds.yml`,
   including the leading dot, or GitHub will not find it. In the web editor,
   create folders by typing the whole path into the filename field.
3. **Create `docs/.gitkeep`** with a single space. Git does not track empty
   folders and the first run needs `docs/` to exist.
4. **Run it once.** Actions, "Merge journal feeds", "Run workflow". Note: "Run
   workflow" builds the current state of the branch; "Re-run all jobs" replays
   an old commit and will fail to push.
5. **Turn on Pages.** Settings, Pages, source "Deploy from a branch", branch
   `main`, folder `/docs`.
6. **Subscribe in Zotero.** File, New Feed, From URL. The Title field fills
   itself once Zotero fetches the feed; you cannot type into it. Set both
   retention periods to 30 days or so, which turns the feed into a
   self-clearing review queue.

Zotero cannot read a local file, incidentally: `file:///` addresses are refused,
which is why the merged feed has to be published on the web at all.

## Maintaining it

- **A journal moves host.** Update `feed_url`, run `python feedcheck.py`, commit.
- **A feed goes quiet.** Run `feedcheck.py`. `FAIL` means the address broke; `OK`
  with an old date means the journal has simply not published.
- **Adding a journal.** Add a row with its ISSNs. If it has a feed, set
  `feed_status` to `confirmed` once `feedcheck.py` says OK. If not, set
  `crossref` and the ISSN alone will do the work. Untested rows are ignored by
  the merger, so a bad row cannot break a run.
- **Finding a feed URL.** OJS sites:
  `[base]/gateway/plugin/WebFeedGatewayPlugin/rss2`. Sabinet:
  `journals.co.za/action/showFeed?type=etoc&feed=rss&jc=[code]`, where the code
  is the last segment of the journal's Sabinet URL, prefixed `jlc.` for Juta
  titles and `ju.` for PULP ones.

## Running it by hand

    python merge_feeds.py --self-test     parses built-in samples, no network
    python merge_feeds.py                 merge locally, write to this folder
    python merge_feeds.py --crossref      as the workflow runs it
    python merge_feeds.py --crossref-all  query Crossref for every journal, to
                                          see the full extent of its coverage
    python feedcheck.py                   test every feed URL
    python feedcheck.py --insecure        as above, ignoring all certificates

`merge_feeds.py --publish` also exists, which uploads the output to GitHub via
its API for when the merger is run locally. It needs `GITHUB_TOKEN` and
`GITHUB_REPO` in the environment and is not used by the scheduled workflow.

## Known gaps

THRHR, Speculum Juris and the Annual Survey of South African Law are not
collected. THRHR is a LexisNexis title outside the aggregators tried; the Annual
Survey is an annual volume rather than an article series; Speculum Juris
publishes from a bespoke site at `specjuris.ufh.ac.za` with no feed found and no
Crossref deposit under either of its ISSNs. If an OJS instance for Speculum
Juris comes to light, its row needs only a `feed_url` and a status change.
