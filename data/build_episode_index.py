#!/usr/bin/env python3
"""Build a self-contained HTML index of BNI podcast episode links."""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CANONICAL_POSTS = ROOT / "canonical_posts.jsonl"
AUDIO_LINKS = ROOT / "audio_links.jsonl"
EPISODE_COVERAGE = ROOT / "episode_coverage.csv"
SUMMARY = ROOT / "summary.json"
OUTPUT = ROOT / "episode_index.html"
DEPLOY_OUTPUT = ROOT.parent / "index.html"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def clean_title(title: str) -> str:
    # Preserve the site's title text, but fix a known typo that affects display.
    return title.replace("Epiode ", "Episode ")


def matches_episode_title(title: str, episode: int) -> bool:
    pattern = rf"\b(?:Special\s+)?Epis?ode\s+0*{episode}\b"
    return re.search(pattern, title, re.IGNORECASE) is not None


def iso_date_to_label(value: str | None) -> str:
    if not value:
        return ""
    try:
        return dt.datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return value[:10]


def read_coverage_titles() -> dict[int, str]:
    titles: dict[int, str] = {}
    if not EPISODE_COVERAGE.exists():
        return titles

    with EPISODE_COVERAGE.open(newline="") as fp:
        for row in csv.DictReader(fp):
            raw_episode = row.get("episode")
            raw_titles = row.get("titles") or ""
            if not raw_episode or not raw_titles.strip():
                continue
            title = raw_titles.split(" | ", 1)[0].strip()
            if title:
                titles[int(raw_episode)] = clean_title(title)
    return titles


def best_audio_urls() -> dict[int, str]:
    urls: dict[int, str] = {}
    if not AUDIO_LINKS.exists():
        return urls

    for row in read_jsonl(AUDIO_LINKS):
        episode = row.get("episode")
        audio_url = row.get("audio_url")
        if isinstance(episode, int) and audio_url and episode not in urls:
            urls[episode] = audio_url
    return urls


def build_entries() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary = json.loads(SUMMARY.read_text()) if SUMMARY.exists() else {}
    coverage_titles = read_coverage_titles()
    audio_urls = best_audio_urls()
    entries: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()

    for row in read_jsonl(CANONICAL_POSTS):
        episode = row.get("episode")
        title = row.get("title") or ""
        link = row.get("link") or ""
        if not isinstance(episode, int) or not title or not link:
            continue
        if not matches_episode_title(title, episode):
            continue

        display_title = clean_title(title)
        key = (episode, display_title, link)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "episode": episode,
                "title": display_title,
                "date": iso_date_to_label(row.get("date")),
                "url": link,
                "kind": "Page",
                "special": display_title.lower().startswith("special episode"),
            }
        )

    start = int(summary.get("start_episode") or 1)
    end = int(summary.get("end_episode") or max((e["episode"] for e in entries), default=0))
    present = {entry["episode"] for entry in entries if not entry["special"]}

    for episode in range(start, end + 1):
        if episode in present:
            continue
        fallback_url = audio_urls.get(episode)
        if not fallback_url:
            continue
        entries.append(
            {
                "episode": episode,
                "title": coverage_titles.get(episode) or f"Episode {episode}",
                "date": "",
                "url": fallback_url,
                "kind": "Audio",
                "special": False,
            }
        )

    entries.sort(
        key=lambda entry: (
            -int(entry["episode"]),
            bool(entry["special"]),
            entry["date"] or "9999-99-99",
            entry["title"].lower(),
        )
    )
    return entries, summary


def render_html(entries: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    collected_at = summary.get("collected_at") or ""
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    normal_count = len({entry["episode"] for entry in entries if not entry["special"]})
    special_count = sum(1 for entry in entries if entry["special"])
    audio_fallback_count = sum(1 for entry in entries if entry["kind"] == "Audio")
    max_episode = max((entry["episode"] for entry in entries), default=0)

    rows = []
    for entry in entries:
        episode = int(entry["episode"])
        title = str(entry["title"])
        date = str(entry["date"])
        url = str(entry["url"])
        kind = str(entry["kind"])
        row_text = f"{episode} {title} {date} {kind} {url}".lower()
        date_html = html.escape(date) if date else '<span class="muted-placeholder">No date</span>'
        rows.append(
            "          <tr "
            f'data-search="{html.escape(row_text, quote=True)}" '
            f'data-kind="{html.escape(kind.lower(), quote=True)}">'
            f'<td class="episode-number" data-label="Episode">{episode}</td>'
            f'<td class="title-cell" data-label="Name and Link"><a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(title)}</a></td>'
            f'<td class="date-cell" data-label="Date">{date_html}</td>'
            f'<td class="type-cell" data-label="Type"><span class="type-label type-{html.escape(kind.lower(), quote=True)}">{html.escape(kind)}</span></td>'
            "</tr>"
        )

    rows_html = "\n".join(rows)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BNI Podcast Episode Index</title>
  <link rel="icon" type="image/svg+xml" href="favicon.svg">
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --surface: #ffffff;
      --ink: #1f252d;
      --muted: #657282;
      --line: #d9dee7;
      --accent: #b51f2a;
      --accent-dark: #8e1821;
      --link: #145c8f;
      --audio: #6d5a2f;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Arial, Helvetica, sans-serif;
      line-height: 1.45;
    }}

    header {{
      background: var(--accent);
      color: #fff;
      padding: 28px 20px 24px;
    }}

    .inner {{
      width: min(1120px, calc(100% - 32px));
      margin: 0 auto;
    }}

    h1 {{
      margin: 0 0 10px;
      font-size: 2.35rem;
      line-height: 1.1;
      letter-spacing: 0;
    }}

    .summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 18px;
      margin: 0;
      color: rgba(255, 255, 255, 0.9);
      font-size: 0.95rem;
    }}

    main {{
      padding: 22px 0 34px;
    }}

    .toolbar {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto;
      align-items: end;
      gap: 14px;
      margin-bottom: 14px;
    }}

    label {{
      display: block;
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 0.86rem;
      font-weight: 700;
      text-transform: uppercase;
    }}

    input[type="search"] {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      color: var(--ink);
      font: inherit;
      padding: 12px 14px;
      outline: none;
    }}

    input[type="search"]:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(181, 31, 42, 0.14);
    }}

    .visible-count {{
      color: var(--muted);
      font-weight: 700;
      white-space: nowrap;
      padding-bottom: 11px;
    }}

    .table-wrap {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }}

    thead {{
      background: #eef1f5;
      border-bottom: 1px solid var(--line);
    }}

    th,
    td {{
      padding: 12px 14px;
      text-align: left;
      vertical-align: top;
      border-bottom: 1px solid var(--line);
    }}

    th {{
      color: #3a4552;
      font-size: 0.82rem;
      text-transform: uppercase;
    }}

    tbody tr:last-child td {{
      border-bottom: 0;
    }}

    tbody tr:hover {{
      background: #fafbfc;
    }}

    .episode-number {{
      width: 92px;
      font-weight: 800;
      color: var(--accent-dark);
    }}

    .title-cell {{
      overflow-wrap: anywhere;
    }}

    .date-cell {{
      width: 132px;
      color: var(--muted);
      white-space: nowrap;
    }}

    .type-cell {{
      width: 92px;
    }}

    a {{
      color: var(--link);
      font-weight: 700;
      text-decoration: none;
    }}

    a:hover {{
      text-decoration: underline;
    }}

    .type-label {{
      display: inline-block;
      color: var(--muted);
      font-size: 0.84rem;
      font-weight: 800;
      line-height: 1.2;
    }}

    .type-page {{
      color: var(--muted);
    }}

    .type-audio {{
      color: var(--audio);
    }}

    .muted-placeholder {{
      color: #98a2af;
    }}

    .empty {{
      display: none;
      padding: 26px 14px;
      color: var(--muted);
      text-align: center;
      border-top: 1px solid var(--line);
    }}

    footer {{
      margin-top: 18px;
      color: var(--muted);
      font-size: 0.86rem;
    }}

    @media (max-width: 820px) {{
      header {{
        padding: 22px 0 20px;
      }}

      .inner {{
        width: min(100% - 24px, 1120px);
      }}

      h1 {{
        font-size: 1.9rem;
      }}

      .summary {{
        gap: 6px 12px;
        font-size: 0.9rem;
      }}

      .toolbar {{
        grid-template-columns: 1fr;
        gap: 8px;
      }}

      .visible-count {{
        padding-bottom: 0;
      }}

      .table-wrap {{
        border: 0;
        background: transparent;
        overflow: visible;
      }}

      table,
      thead,
      tbody,
      tr,
      td {{
        display: block;
      }}

      table {{
        table-layout: auto;
      }}

      thead {{
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
      }}

      tbody {{
        display: grid;
        gap: 10px;
      }}

      tbody tr {{
        display: grid;
        grid-template-columns: 64px minmax(0, 1fr);
        gap: 6px 12px;
        padding: 12px;
        background: var(--surface);
        border: 1px solid var(--line);
        border-radius: 8px;
      }}

      tbody tr:hover {{
        background: var(--surface);
      }}

      td {{
        padding: 0;
        border: 0;
      }}

      .episode-number {{
        grid-row: span 3;
        width: auto;
        font-size: 1.08rem;
      }}

      .episode-number::before {{
        content: "#";
      }}

      .title-cell {{
        min-width: 0;
      }}

      .date-cell,
      .type-cell {{
        width: auto;
        font-size: 0.88rem;
      }}

      .date-cell {{
        white-space: normal;
      }}

      .date-cell::before,
      .type-cell::before {{
        content: attr(data-label) ": ";
        color: var(--muted);
        font-weight: 700;
      }}

      a {{
        line-height: 1.3;
      }}
    }}

    @media (max-width: 480px) {{
      .inner {{
        width: min(100% - 16px, 1120px);
      }}

      main {{
        padding-top: 16px;
      }}

      h1 {{
        font-size: 1.62rem;
      }}

      .summary {{
        display: grid;
        gap: 4px;
      }}

      input[type="search"] {{
        padding: 11px 12px;
      }}

      tbody tr {{
        grid-template-columns: 52px minmax(0, 1fr);
        gap: 5px 10px;
        padding: 11px;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="inner">
      <h1>BNI Podcast Episode Index</h1>
      <p class="summary">
        <span>{normal_count} numbered episodes</span>
        <span>{special_count} special episodes</span>
        <span>Latest episode: {max_episode}</span>
        <span>{audio_fallback_count} audio-only fallback</span>
      </p>
    </div>
  </header>

  <main class="inner">
    <div class="toolbar">
      <div>
        <label for="episode-search">Search</label>
        <input id="episode-search" type="search" autocomplete="off" placeholder="Episode number, title, date, or link">
      </div>
      <div class="visible-count"><span id="visible-count">{len(entries)}</span> links</div>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Episode</th>
            <th>Name and Link</th>
            <th>Date</th>
            <th>Type</th>
          </tr>
        </thead>
        <tbody id="episode-rows">
{rows_html}
        </tbody>
      </table>
      <div id="empty-state" class="empty">No matching episodes.</div>
    </div>

    <footer>
      Generated {html.escape(generated_at)} from local project data. Data collected {html.escape(collected_at)}.
    </footer>
  </main>

  <script>
    const input = document.getElementById('episode-search');
    const rows = Array.from(document.querySelectorAll('#episode-rows tr'));
    const count = document.getElementById('visible-count');
    const empty = document.getElementById('empty-state');

    function updateRows() {{
      const query = input.value.trim().toLowerCase();
      let visible = 0;

      for (const row of rows) {{
        const show = !query || row.dataset.search.includes(query);
        row.hidden = !show;
        if (show) visible += 1;
      }}

      count.textContent = visible;
      empty.style.display = visible === 0 ? 'block' : 'none';
    }}

    input.addEventListener('input', updateRows);
  </script>
</body>
</html>
"""


def main() -> None:
    entries, summary = build_entries()
    rendered = render_html(entries, summary)
    OUTPUT.write_text(rendered, encoding="utf-8")
    DEPLOY_OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT.name} and {DEPLOY_OUTPUT.name} with {len(entries)} links.")


if __name__ == "__main__":
    main()
