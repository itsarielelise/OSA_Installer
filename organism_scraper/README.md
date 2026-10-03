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

Files go to `./downloads/<Person>/` (change with `-o`). If a run is cut short,
run the same command again: finished files are skipped and partial ones resume.

Options: `--workers 3` (parallel downloads), `--delay 0.5` (seconds between
requests, keep it polite), `--depth 2` (how many links deep to follow from a person page).

How it works: it reads the library index to find each person's page, then
follows that person's pages (including pagination) and the talk/document pages
they link to. It collects every audio link it finds (`.mp3`, `.m4a`, `.ogg`, …),
including ones in `<audio>` players and inline JavaScript. If the site changes
its layout and `--list` finds nobody, use `--url` with a person's page instead.
