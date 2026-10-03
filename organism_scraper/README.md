# organism.earth audio downloader

Downloads all the free audio files for the people you choose from
<https://www.organism.earth/library/>, so you don't have to click each one.

Needs Python 3.8+ and nothing else (no `pip install`).

```bash
python organism_dl.py                       # numbered menu: pick people (e.g. 3,7,12-15)
python organism_dl.py --list                # list everyone in the library
python organism_dl.py "terence mckenna"     # download one person (names are case-insensitive)
python organism_dl.py "terence mckenna" "alan watts"
python organism_dl.py "terence mckenna" --dry-run   # show what it would download
python organism_dl.py --url https://www.organism.earth/library/<person-page>   # if name lookup misses
```

Files go to `./downloads/<Person>/`, named after each talk's title (e.g.
`Calendar for the Goddess.mp3`), plus an `index.tsv` listing each file's title,
document page and source URL. Change the folder with `-o`. If a run is cut
short, run the same command again: finished files are skipped and partial
ones resume.

Options: `--workers 3` (parallel downloads), `--delay 0.5` (seconds between
requests, keep it polite).

How it works, in two steps:

1. Reads the author page (e.g. `/library/author/terence-mckenna`, including
   its "next page" links) and collects every `/library/document/...` link and
   its title.
2. Opens each document page (e.g. `/library/document/calendar-for-the-goddess`)
   and takes the audio from that page only: `<audio>` players, download
   buttons, `.mp3`/`.m4a`/… links, and URLs inside the page's JavaScript. If a
   document has no audio itself but has a "Listen"/"Audio"/"Download" link, it
   follows that one link. Text-only documents are reported and skipped.
   "Related" documents by other authors are never followed.

If a name isn't found on the library index, it tries
`/library/author/<first-last>` directly. You can always pass a person's page
with `--url`.
