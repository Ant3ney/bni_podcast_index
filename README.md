# BNI Podcast Collection

This folder contains a standalone crawler for collecting The Official BNI Podcast
audio-link evidence.

The collection rule is intentionally conservative: raw collection files preserve
every observed occurrence of every `content.blubrry.com/bni/` MP3 link. Duplicate
links from the RSS feed, canonical pages, archive pages, search pages, and mirror
pages are not deduplicated in `audio_links.jsonl`. Coverage and summary files are
separate analysis outputs.

## Run

```bash
python3 bni_podcast_crawler.py --out-dir data --search-missing --search-mirror-domains
```

By default the crawler:

- Treats `https://www.bnipodcast.com` as canonical.
- Uses the WordPress posts API to detect the latest episode.
- Crawls the current RSS feed as one source, but does not rely on it as complete.
- Crawls home, subscribe, shows-by-year, yearly, monthly, and discovered episode pages.
- Checks episode numbers from `1` through the detected latest episode.
- Uses canonical WordPress/site search for missing episode numbers.
- Optionally uses DuckDuckGo HTML search for missing episode numbers and mirror domains.
- Probes predictable Blubrry filenames for still-missing numbers after search, including
  zero-padded early filenames such as `025-BNI-Podcast.mp3`.

## Outputs

- `data/audio_links.jsonl`: raw duplicate-preserving audio-link occurrences.
- `data/episode_pages.jsonl`: raw duplicate-preserving episode page observations.
- `data/mirror_links.jsonl`: observed Apple, Spotify, Podbean, Podcast Republic,
  Listen Notes, Castbox, Amazon, YouTube, Facebook, Instagram, and other mirror links.
- `data/search_results.jsonl`: canonical and web-search results used for missing episodes.
- `data/canonical_posts.jsonl`: WordPress post metadata by crawl occurrence.
- `data/episode_coverage.csv`: per-episode summary. This file counts and groups; it
  does not replace the raw collection.
- `data/missing_episodes.txt`: episode numbers still missing audio evidence.
- `data/raw/`: cached response bodies for inspection.

## Proc Update
