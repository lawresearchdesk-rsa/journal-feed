# Merged journal feed: setup note

## What the pieces are

Four files, each with one job.

| File | Job |
|---|---|
| `journals.csv` | The roster. Every other file reads it and nothing else. One row per journal; `feed_status` decides whether a row is used, `tls` handles hosts with broken certificates. |
| `feedcheck.py` | Diagnostic. Tests every `feed_url` and reports what came back. Run it whenever a journal changes host or a feed goes quiet. |
| `merge_feeds.py` | The merger. Fetches every `confirmed` feed, normalises the items, adds them to a running store, and writes one combined RSS file. |
| `merge-feeds.yml` | The scheduler. Tells GitHub to run the merger every morning and publish the result. |

## Why there is a store

Most journal feeds show only the current issue. If the merged feed were rebuilt
from scratch each run, an article would vanish from it as soon as the next issue
appeared, possibly before you had read it. `store.json` accumulates everything
ever seen, keyed on the article link, so the merged feed has a memory. The
`--max-items` setting decides how many of the newest are published; the rest stay
in the store.

## Standing it up

You need a GitHub account. Everything used here is free.

1. **Create a repository.** Name it something like `journal-feed`. Public is
   simplest, since GitHub Pages on private repositories needs a paid plan.
   Nothing in it is sensitive.

2. **Add the files.** Upload `journals.csv`, `feedcheck.py` and
   `merge_feeds.py` to the top level. Create a folder `.github/workflows` and
   put `merge-feeds.yml` inside it. The workflow only runs if it is at that
   exact path.

3. **Create the output folder.** Add an empty file at `docs/.gitkeep`. Git does
   not track empty folders, and the first run needs `docs/` to exist.

4. **Run it once by hand.** Go to the Actions tab, choose "Merge journal
   feeds", and press "Run workflow". It takes a minute or two. When it
   finishes, `docs/merged.xml` should appear in the repository. If the run
   fails, open it and read the log for the step that failed; the merger prints
   one line per journal, so a failing feed is easy to spot.

5. **Turn on Pages.** Settings, then Pages, then set the source to "Deploy from
   a branch", branch `main`, folder `/docs`. Save. After a few minutes your
   feed is live at:

   `https://YOUR-USERNAME.github.io/journal-feed/merged.xml`

6. **Subscribe in Zotero.** File, New Feed, From URL, and paste that address.
   Set the retention period in the feed's settings to 30 or 60 days, which turns
   the feed into a self-clearing review queue.

After that it runs itself. The daily schedule costs nothing and means an article
is in the feed within a day of publication; you can still read it monthly.

## Maintaining it

- **A journal moves host.** Update its `feed_url` in `journals.csv`, run
  `python feedcheck.py`, commit the change.
- **A feed goes quiet.** Run `feedcheck.py`. A `FAIL` means the address broke;
  an `OK` with an old date means the journal simply has not published.
- **Adding a journal.** Add a row, find its feed, set `feed_status` to
  `confirmed` once `feedcheck.py` says OK. The merger picks up only
  `confirmed` rows, so an untested row cannot break the run.
- **The two `tls=relaxed` rows.** Those UFS journals send an incomplete
  certificate chain. Browsers repair it silently; Python cannot. The flag skips
  verification for those two hosts alone. If UFS ever fixes its configuration,
  set them back to `strict`.

## Testing without touching the network

`python merge_feeds.py --self-test` parses built-in samples of all three feed
formats, checks that duplicates collapse and that output is ordered newest
first, then exits. Useful after editing the script.

## What this does not cover

Six journals on the roster have no feed at all: THRHR, Speculum Juris,
Fundamina, AJIC, Annual Survey and the African Journal of Legal Studies. They
need the Crossref route, which queries by ISSN rather than subscribing to
anything. That is a separate script, not yet written.
