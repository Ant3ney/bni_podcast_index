#!/usr/bin/env python3
"""Collect BNI podcast audio-link evidence without deduplicating raw rows.

The Official BNI Podcast RSS feed is useful, but it is intentionally only one
input. This crawler treats bnipodcast.com as canonical and gathers evidence from
the WordPress API, canonical HTML pages, archive pages, per-episode search pages,
and mirror/search pages. Raw audio-link records are emitted one occurrence at a
time; duplicates are only collapsed in separate coverage summaries.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.client import HTTPResponse
from pathlib import Path
from typing import Any, Iterable


BASE_URL = "https://www.bnipodcast.com"
FEED_URL = f"{BASE_URL}/feed/podcast/"
WP_POSTS_URL = f"{BASE_URL}/wp-json/wp/v2/posts"
WP_SEARCH_URL = f"{BASE_URL}/wp-json/wp/v2/search"
SHOWS_BY_YEAR_URL = f"{BASE_URL}/shows-by-year/"
SUBSCRIBE_URL = f"{BASE_URL}/subscribe/"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

AUDIO_RE = re.compile(
    r"https?://[^\s\"'<>\\]+content\.blubrry\.com/bni/[^\s\"'<>\\]+?\.mp3"
    r"(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
EPISODE_PATTERNS = (
    re.compile(r"\bepisode[\s#:\-]*(\d{1,4})\b", re.IGNORECASE),
    re.compile(r"/episode-(\d{1,4})(?:[-/]|$)", re.IGNORECASE),
    re.compile(r"/(\d{1,4})-BNI-Podcast\.mp3\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,4})-BNI-Podcast\.mp3\b", re.IGNORECASE),
)

MIRROR_DOMAINS: dict[str, str] = {
    "podcasts.apple.com": "Apple Podcasts",
    "itunes.apple.com": "Apple Podcasts",
    "open.spotify.com": "Spotify",
    "spotify.com": "Spotify",
    "podbean.com": "Podbean",
    "podcastrepublic.net": "Podcast Republic",
    "listennotes.com": "Listen Notes",
    "castbox.fm": "Castbox",
    "music.amazon.com": "Amazon Music",
    "podcasts.amazon.com": "Amazon Music",
    "amazon.com": "Amazon",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "iheart.com": "iHeartRadio",
    "podchaser.com": "Podchaser",
    "deezer.com": "Deezer",
    "player.fm": "Player FM",
    "podtail.com": "Podtail",
    "podbay.fm": "Podbay",
    "overcast.fm": "Overcast",
    "goodpods.com": "Goodpods",
    "podcasts.google.com": "Google Podcasts",
}


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status: int | None
    content_type: str
    body: bytes
    error: str | None = None

    @property
    def text(self) -> str:
        charset = "utf-8"
        match = re.search(r"charset=([^;\s]+)", self.content_type, re.IGNORECASE)
        if match:
            charset = match.group(1).strip("\"'")
        return self.body.decode(charset, errors="replace")


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")

    def write(self, row: dict[str, Any]) -> None:
        self.handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def close(self) -> None:
        self.handle.close()


class Crawler:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out_dir = Path(args.out_dir)
        self.raw_dir = self.out_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.collected_at = dt.datetime.now(dt.UTC).isoformat()
        self.fetch_log = JsonlWriter(self.out_dir / "fetches.jsonl")
        self.audio_out = JsonlWriter(self.out_dir / "audio_links.jsonl")
        self.episode_pages_out = JsonlWriter(self.out_dir / "episode_pages.jsonl")
        self.mirror_links_out = JsonlWriter(self.out_dir / "mirror_links.jsonl")
        self.search_results_out = JsonlWriter(self.out_dir / "search_results.jsonl")
        self.posts_out = JsonlWriter(self.out_dir / "canonical_posts.jsonl")
        self.audio_rows: list[dict[str, Any]] = []
        self.episode_pages: list[dict[str, Any]] = []
        self.posts_by_episode: dict[int, list[dict[str, Any]]] = {}
        self.episodes_with_audio: set[int] = set()
        self.known_year_months: set[tuple[int, int]] = set()
        self.seen_fetches: set[str] = set()
        self.sleep_after_fetch = float(args.delay)
        self.user_agent = args.user_agent

    def close(self) -> None:
        self.fetch_log.close()
        self.audio_out.close()
        self.episode_pages_out.close()
        self.mirror_links_out.close()
        self.search_results_out.close()
        self.posts_out.close()

    def fetch(
        self,
        url: str,
        *,
        accept: str = "*/*",
        cache_name: str | None = None,
        timeout: int = 30,
        retries: int = 2,
    ) -> FetchResult:
        normalized = normalize_url(url)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
        last_error: str | None = None
        for attempt in range(retries + 1):
            if self.sleep_after_fetch:
                time.sleep(self.sleep_after_fetch + random.uniform(0, self.sleep_after_fetch / 3))
            try:
                req = urllib.request.Request(normalized, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    assert isinstance(response, HTTPResponse)
                    body = response.read()
                    result = FetchResult(
                        url=normalized,
                        final_url=response.geturl(),
                        status=response.status,
                        content_type=response.headers.get("Content-Type", ""),
                        body=body,
                    )
                    self.log_fetch(result)
                    if cache_name:
                        self.write_raw(cache_name, result.body)
                    return result
            except urllib.error.HTTPError as exc:
                body = exc.read()
                result = FetchResult(
                    url=normalized,
                    final_url=exc.geturl() or normalized,
                    status=exc.code,
                    content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
                    body=body,
                    error=f"HTTPError: {exc.code}",
                )
                self.log_fetch(result)
                if cache_name:
                    self.write_raw(cache_name, result.body)
                return result
            except Exception as exc:  # noqa: BLE001 - record failures and continue crawling.
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))

        result = FetchResult(
            url=normalized,
            final_url=normalized,
            status=None,
            content_type="",
            body=b"",
            error=last_error or "unknown fetch error",
        )
        self.log_fetch(result)
        return result

    def head(
        self,
        url: str,
        *,
        accept: str = "*/*",
        timeout: int = 20,
        retries: int = 1,
    ) -> FetchResult:
        normalized = normalize_url(url)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
        last_error: str | None = None
        for attempt in range(retries + 1):
            if self.sleep_after_fetch:
                time.sleep(self.sleep_after_fetch + random.uniform(0, self.sleep_after_fetch / 3))
            try:
                req = urllib.request.Request(normalized, headers=headers, method="HEAD")
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    assert isinstance(response, HTTPResponse)
                    header_blob = "\n".join(f"{key}: {value}" for key, value in response.headers.items()).encode("utf-8")
                    result = FetchResult(
                        url=normalized,
                        final_url=response.geturl(),
                        status=response.status,
                        content_type=response.headers.get("Content-Type", ""),
                        body=header_blob,
                    )
                    self.log_fetch(result)
                    return result
            except urllib.error.HTTPError as exc:
                header_blob = "\n".join(f"{key}: {value}" for key, value in exc.headers.items()).encode("utf-8") if exc.headers else b""
                result = FetchResult(
                    url=normalized,
                    final_url=exc.geturl() or normalized,
                    status=exc.code,
                    content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
                    body=header_blob,
                    error=f"HTTPError: {exc.code}",
                )
                self.log_fetch(result)
                return result
            except Exception as exc:  # noqa: BLE001 - record failures and continue crawling.
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        result = FetchResult(
            url=normalized,
            final_url=normalized,
            status=None,
            content_type="",
            body=b"",
            error=last_error or "unknown fetch error",
        )
        self.log_fetch(result)
        return result

    def log_fetch(self, result: FetchResult) -> None:
        self.fetch_log.write(
            {
                "collected_at": self.collected_at,
                "url": result.url,
                "final_url": result.final_url,
                "status": result.status,
                "content_type": result.content_type,
                "bytes": len(result.body),
                "error": result.error,
            }
        )

    def write_raw(self, name: str, body: bytes) -> None:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
        if not safe_name:
            return
        path = self.raw_dir / safe_name
        path.write_bytes(body)

    def collect_audio_from_text(
        self,
        text: str,
        *,
        source_type: str,
        source_platform: str,
        source_url: str,
        source_title: str | None = None,
        source_episode: int | None = None,
        source_id: str | None = None,
    ) -> int:
        decoded = html.unescape(text).replace("\\/", "/")
        count = 0
        for match in AUDIO_RE.finditer(decoded):
            audio_url = strip_trailing_url_punctuation(match.group(0))
            context_start = max(match.start() - 220, 0)
            context_end = min(match.end() + 220, len(decoded))
            context = squash_space(decoded[context_start:context_end])
            episode = infer_episode(audio_url) or source_episode or infer_episode(context)
            row = {
                "collected_at": self.collected_at,
                "episode": episode,
                "source_type": source_type,
                "source_platform": source_platform,
                "source_url": source_url,
                "source_title": clean_text(source_title),
                "source_id": source_id,
                "audio_url": audio_url,
                "context": context[:500],
                "occurrence_number": len(self.audio_rows) + 1,
            }
            self.audio_rows.append(row)
            self.audio_out.write(row)
            if episode:
                self.episodes_with_audio.add(episode)
            count += 1
        return count

    def collect_links_from_text(
        self,
        text: str,
        *,
        source_type: str,
        source_url: str,
        source_title: str | None = None,
    ) -> list[str]:
        decoded = html.unescape(text).replace("\\/", "/")
        links: list[str] = []
        for match in URL_RE.finditer(decoded):
            link = strip_trailing_url_punctuation(match.group(0))
            if not link:
                continue
            links.append(link)
            episode = infer_episode(link)
            if is_bnipodcast_episode_url(link):
                link_episode = episode
                link_title = source_title if infer_episode(source_title) == link_episode else None
                self.record_episode_page(
                    episode=link_episode,
                    page_url=link,
                    source_type=source_type,
                    source_url=source_url,
                    title=link_title,
                )
            platform = mirror_platform(link)
            if platform:
                self.mirror_links_out.write(
                    {
                        "collected_at": self.collected_at,
                        "episode": episode,
                        "source_type": source_type,
                        "source_url": source_url,
                        "source_title": clean_text(source_title),
                        "platform": platform,
                        "mirror_url": link,
                    }
                )
        return links

    def record_episode_page(
        self,
        *,
        episode: int | None,
        page_url: str,
        source_type: str,
        source_url: str,
        title: str | None,
        post_id: int | None = None,
        date: str | None = None,
    ) -> None:
        row = {
            "collected_at": self.collected_at,
            "episode": episode,
            "page_url": normalize_url(page_url),
            "source_type": source_type,
            "source_url": source_url,
            "title": clean_text(title),
            "post_id": post_id,
            "date": date,
            "occurrence_number": len(self.episode_pages) + 1,
        }
        self.episode_pages.append(row)
        self.episode_pages_out.write(row)

    def crawl_home_and_subscribe(self) -> None:
        for url, cache_name, source_type in (
            (BASE_URL + "/", "home.html", "canonical_home_html"),
            (SUBSCRIBE_URL, "subscribe.html", "canonical_subscribe_html"),
        ):
            result = self.fetch(url, accept="text/html,*/*", cache_name=cache_name)
            if result.status and result.status < 400:
                title = extract_html_title(result.text)
                self.collect_audio_from_text(
                    result.text,
                    source_type=source_type,
                    source_platform="bnipodcast",
                    source_url=result.final_url,
                    source_title=title,
                )
                self.collect_links_from_text(
                    result.text,
                    source_type=source_type,
                    source_url=result.final_url,
                    source_title=title,
                )

    def crawl_feed(self) -> None:
        result = self.fetch(FEED_URL, accept="application/rss+xml,application/xml,text/xml,*/*", cache_name="feed_podcast.xml")
        if not result.status or result.status >= 400:
            return
        text = result.text
        self.collect_audio_from_text(
            text,
            source_type="canonical_rss_text",
            source_platform="bnipodcast",
            source_url=result.final_url,
            source_title="The Official BNI Podcast RSS",
        )
        self.collect_links_from_text(
            text,
            source_type="canonical_rss_text",
            source_url=result.final_url,
            source_title="The Official BNI Podcast RSS",
        )
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return
        for item in root.findall("./channel/item"):
            title = get_xml_text(item, "title")
            link = get_xml_text(item, "link")
            pub_date = get_xml_text(item, "pubDate")
            episode = infer_episode(title) or infer_episode(link)
            if link:
                self.record_episode_page(
                    episode=episode,
                    page_url=link,
                    source_type="canonical_rss_item",
                    source_url=result.final_url,
                    title=title,
                    date=parse_rfc2822_date(pub_date),
                )
            for enclosure in item.findall("enclosure"):
                audio_url = enclosure.attrib.get("url")
                if not audio_url or "content.blubrry.com/bni/" not in audio_url:
                    continue
                row = {
                    "collected_at": self.collected_at,
                    "episode": episode or infer_episode(audio_url),
                    "source_type": "canonical_rss_enclosure",
                    "source_platform": "bnipodcast",
                    "source_url": link or result.final_url,
                    "source_title": clean_text(title),
                    "source_id": None,
                    "audio_url": html.unescape(audio_url).replace("\\/", "/"),
                    "context": "rss enclosure",
                    "occurrence_number": len(self.audio_rows) + 1,
                }
                self.audio_rows.append(row)
                self.audio_out.write(row)
                if row["episode"]:
                    self.episodes_with_audio.add(row["episode"])

    def crawl_wp_posts(self) -> int | None:
        latest_episode: int | None = None
        page = 1
        while True:
            url = add_query(
                WP_POSTS_URL,
                {
                    "per_page": "100",
                    "page": str(page),
                    "_fields": "id,date,date_gmt,modified,slug,link,title,content,excerpt",
                },
            )
            result = self.fetch(
                url,
                accept="application/json,text/plain,*/*",
                cache_name=f"wp_posts_page_{page:03d}.json",
            )
            if not result.status or result.status >= 400:
                if page == 1:
                    return latest_episode
                break
            try:
                posts = json.loads(result.text)
            except json.JSONDecodeError:
                break
            if not posts:
                break
            for post in posts:
                title = render_field(post.get("title"))
                content = render_field(post.get("content"))
                excerpt = render_field(post.get("excerpt"))
                link = post.get("link") or ""
                slug = post.get("slug") or ""
                episode = infer_episode(title) or infer_episode(slug) or infer_episode(link) or infer_episode(content)
                post_id = post.get("id")
                post_row = {
                    "collected_at": self.collected_at,
                    "episode": episode,
                    "post_id": post_id,
                    "date": post.get("date"),
                    "date_gmt": post.get("date_gmt"),
                    "modified": post.get("modified"),
                    "slug": slug,
                    "link": link,
                    "title": clean_text(title),
                }
                self.posts_out.write(post_row)
                if episode:
                    latest_episode = max(latest_episode or 0, episode)
                    self.posts_by_episode.setdefault(episode, []).append(post_row)
                    year_month = parse_year_month(post.get("date"))
                    if year_month:
                        self.known_year_months.add(year_month)
                if link:
                    self.record_episode_page(
                        episode=episode,
                        page_url=link,
                        source_type="canonical_wp_api_post",
                        source_url=result.final_url,
                        title=title,
                        post_id=post_id,
                        date=post.get("date"),
                    )
                combined = "\n".join([title, link, slug, content, excerpt])
                self.collect_audio_from_text(
                    combined,
                    source_type="canonical_wp_api_post",
                    source_platform="bnipodcast",
                    source_url=link or result.final_url,
                    source_title=title,
                    source_episode=episode,
                    source_id=str(post_id) if post_id is not None else None,
                )
                self.collect_links_from_text(
                    combined,
                    source_type="canonical_wp_api_post",
                    source_url=link or result.final_url,
                    source_title=title,
                )
            page += 1
        return latest_episode

    def crawl_archive_pages(self) -> None:
        urls: list[tuple[str, str]] = [(SHOWS_BY_YEAR_URL, "shows_by_year.html")]
        if self.known_year_months:
            years = sorted({year for year, _month in self.known_year_months})
            for year in years:
                urls.append((f"{BASE_URL}/{year}/", f"archive_{year}.html"))
            for year, month in sorted(self.known_year_months):
                urls.append((f"{BASE_URL}/{year}/{month:02d}/", f"archive_{year}_{month:02d}.html"))
        else:
            current_year = dt.datetime.now().year
            for year in range(2007, current_year + 1):
                urls.append((f"{BASE_URL}/{year}/", f"archive_{year}.html"))
                for month in range(1, 13):
                    urls.append((f"{BASE_URL}/{year}/{month:02d}/", f"archive_{year}_{month:02d}.html"))
        for url, cache_name in urls:
            result = self.fetch(url, accept="text/html,*/*", cache_name=cache_name)
            if not result.status or result.status >= 400:
                continue
            title = extract_html_title(result.text)
            self.collect_audio_from_text(
                result.text,
                source_type="canonical_archive_html",
                source_platform="bnipodcast",
                source_url=result.final_url,
                source_title=title,
            )
            self.collect_links_from_text(
                result.text,
                source_type="canonical_archive_html",
                source_url=result.final_url,
                source_title=title,
            )

    def crawl_episode_pages(self, start: int, end: int) -> None:
        canonical_by_episode: dict[int, str] = {}
        for episode, rows in self.posts_by_episode.items():
            if start <= episode <= end:
                for row in rows:
                    link = row.get("link")
                    if link:
                        canonical_by_episode.setdefault(episode, link)
        for row in self.episode_pages:
            episode = row.get("episode")
            page_url = row.get("page_url")
            if isinstance(episode, int) and start <= episode <= end and page_url:
                canonical_by_episode.setdefault(episode, page_url)

        for episode in range(start, end + 1):
            page_url = canonical_by_episode.get(episode)
            if not page_url:
                continue
            cache_name = f"episode_{episode:04d}.html"
            result = self.fetch(page_url, accept="text/html,*/*", cache_name=cache_name)
            if not result.status or result.status >= 400:
                continue
            title = extract_html_title(result.text)
            self.collect_audio_from_text(
                result.text,
                source_type="canonical_episode_html",
                source_platform="bnipodcast",
                source_url=result.final_url,
                source_title=title,
                source_episode=episode,
            )
            self.collect_links_from_text(
                result.text,
                source_type="canonical_episode_html",
                source_url=result.final_url,
                source_title=title,
            )

    def crawl_wp_search_for_missing(self, missing: Iterable[int]) -> None:
        for episode in missing:
            query = f"episode {episode}"
            url = add_query(WP_SEARCH_URL, {"search": query, "per_page": "10"})
            result = self.fetch(
                url,
                accept="application/json,text/plain,*/*",
                cache_name=f"wp_search_episode_{episode:04d}.json",
            )
            if result.status and result.status < 400:
                try:
                    rows = json.loads(result.text)
                except json.JSONDecodeError:
                    rows = []
                for item in rows:
                    title = item.get("title")
                    page_url = item.get("url")
                    found_episode = infer_episode(title) or infer_episode(page_url or "")
                    self.search_results_out.write(
                        {
                            "collected_at": self.collected_at,
                            "episode": episode,
                            "query": query,
                            "search_source": "bnipodcast_wp_search_api",
                            "result_title": clean_text(title),
                            "result_url": page_url,
                            "result_episode": found_episode,
                        }
                    )
                    if page_url and found_episode == episode:
                        self.record_episode_page(
                            episode=episode,
                            page_url=page_url,
                            source_type="canonical_wp_search_api",
                            source_url=url,
                            title=title,
                        )
                        page_result = self.fetch(
                            page_url,
                            accept="text/html,*/*",
                            cache_name=f"wp_search_episode_{episode:04d}_page.html",
                        )
                        if page_result.status and page_result.status < 400:
                            self.collect_audio_from_text(
                                page_result.text,
                                source_type="canonical_wp_search_result_html",
                                source_platform="bnipodcast",
                                source_url=page_result.final_url,
                                source_title=extract_html_title(page_result.text) or title,
                                source_episode=episode,
                            )
                            self.collect_links_from_text(
                                page_result.text,
                                source_type="canonical_wp_search_result_html",
                                source_url=page_result.final_url,
                                source_title=title,
                            )

            html_url = add_query(BASE_URL + "/", {"s": query})
            html_result = self.fetch(
                html_url,
                accept="text/html,*/*",
                cache_name=f"site_search_episode_{episode:04d}.html",
            )
            if html_result.status and html_result.status < 400:
                title = extract_html_title(html_result.text)
                self.collect_audio_from_text(
                    html_result.text,
                    source_type="canonical_site_search_html",
                    source_platform="bnipodcast",
                    source_url=html_result.final_url,
                    source_title=title,
                )
                self.collect_links_from_text(
                    html_result.text,
                    source_type="canonical_site_search_html",
                    source_url=html_result.final_url,
                    source_title=title,
                )

    def crawl_web_search_for_missing(self, missing: Iterable[int]) -> None:
        domains = list(MIRROR_DOMAINS)
        for episode in missing:
            queries = [f'"BNI Podcast" "Episode {episode}"']
            if self.args.search_mirror_domains:
                for domain in domains:
                    queries.append(f'site:{domain} "BNI Podcast" "Episode {episode}"')
            for query in queries[: self.args.max_queries_per_missing]:
                result_links = self.search_duckduckgo(query, episode)
                for link, title in result_links[: self.args.max_search_results]:
                    platform = mirror_platform(link) or ("bnipodcast" if "bnipodcast.com" in link else "web")
                    self.search_results_out.write(
                        {
                            "collected_at": self.collected_at,
                            "episode": episode,
                            "query": query,
                            "search_source": "duckduckgo_html",
                            "result_title": clean_text(title),
                            "result_url": link,
                            "result_episode": infer_episode(title) or infer_episode(link),
                            "platform": platform,
                        }
                    )
                    if platform == "web":
                        continue
                    fetched = self.fetch(
                        link,
                        accept="text/html,application/xhtml+xml,*/*",
                        cache_name=f"web_result_episode_{episode:04d}_{safe_filename(link)}.html",
                        timeout=20,
                        retries=1,
                    )
                    if fetched.status and fetched.status < 400:
                        fetched_title = extract_html_title(fetched.text) or title
                        self.collect_audio_from_text(
                            fetched.text,
                            source_type="mirror_web_search_result_html",
                            source_platform=platform,
                            source_url=fetched.final_url,
                            source_title=fetched_title,
                            source_episode=episode if infer_episode(fetched_title) == episode else None,
                        )
                        self.collect_links_from_text(
                            fetched.text,
                            source_type="mirror_web_search_result_html",
                            source_url=fetched.final_url,
                            source_title=fetched_title,
                        )

    def probe_direct_blubrry_audio(self, missing: Iterable[int]) -> None:
        for episode in missing:
            names = [f"{episode}-BNI-Podcast.mp3"]
            if episode < 100:
                names.append(f"{episode:03d}-BNI-Podcast.mp3")
            for name in names:
                url = f"https://media.blubrry.com/bni/content.blubrry.com/bni/{name}"
                result = self.head(url, accept="audio/mpeg,*/*")
                self.search_results_out.write(
                    {
                        "collected_at": self.collected_at,
                        "episode": episode,
                        "query": url,
                        "search_source": "direct_blubrry_head_probe",
                        "result_title": None,
                        "result_url": result.final_url,
                        "result_episode": infer_episode(result.final_url),
                        "status": result.status,
                        "content_type": result.content_type,
                    }
                )
                if result.status and 200 <= result.status < 300 and "content.blubrry.com/bni/" in result.final_url:
                    row = {
                        "collected_at": self.collected_at,
                        "episode": infer_episode(result.final_url) or episode,
                        "source_type": "direct_blubrry_head_probe",
                        "source_platform": "Blubrry",
                        "source_url": url,
                        "source_title": None,
                        "source_id": None,
                        "audio_url": result.final_url,
                        "context": squash_space(result.text)[:500],
                        "occurrence_number": len(self.audio_rows) + 1,
                    }
                    self.audio_rows.append(row)
                    self.audio_out.write(row)
                    self.episodes_with_audio.add(row["episode"])

    def search_duckduckgo(self, query: str, episode: int) -> list[tuple[str, str]]:
        url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        result = self.fetch(
            url,
            accept="text/html,*/*",
            cache_name=f"ddg_episode_{episode:04d}_{safe_filename(query)}.html",
            timeout=20,
            retries=1,
        )
        if not result.status or result.status >= 400:
            return []
        decoded = html.unescape(result.text)
        matches: list[tuple[str, str]] = []
        result_re = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        for match in result_re.finditer(decoded):
            href = html.unescape(match.group("href"))
            title = clean_text(re.sub(r"<[^>]+>", " ", match.group("title")))
            link = unwrap_duckduckgo_url(href)
            if link:
                matches.append((link, title))
        if matches:
            return matches
        for match in URL_RE.finditer(decoded):
            link = strip_trailing_url_punctuation(html.unescape(match.group(0)))
            if "duckduckgo.com" not in link and ("BNI" in decoded[max(match.start() - 100, 0) : match.end() + 100] or "bni" in link.lower()):
                matches.append((link, ""))
        return matches

    def write_coverage(self, start: int, end: int) -> list[int]:
        by_episode: dict[int, dict[str, Any]] = {
            episode: {
                "episode": episode,
                "raw_audio_occurrences": 0,
                "unique_audio_urls": set(),
                "canonical_audio_occurrences": 0,
                "mirror_audio_occurrences": 0,
                "episode_page_occurrences": 0,
                "canonical_post_count": 0,
                "titles": set(),
            }
            for episode in range(start, end + 1)
        }
        for row in self.audio_rows:
            episode = row.get("episode")
            if isinstance(episode, int) and start <= episode <= end:
                entry = by_episode[episode]
                entry["raw_audio_occurrences"] += 1
                entry["unique_audio_urls"].add(row.get("audio_url"))
                if row.get("source_platform") == "bnipodcast":
                    entry["canonical_audio_occurrences"] += 1
                else:
                    entry["mirror_audio_occurrences"] += 1
        for row in self.episode_pages:
            episode = row.get("episode")
            if isinstance(episode, int) and start <= episode <= end:
                by_episode[episode]["episode_page_occurrences"] += 1
                title = row.get("title")
                if title and infer_episode(title) == episode:
                    by_episode[episode]["titles"].add(title)
        for episode, posts in self.posts_by_episode.items():
            if start <= episode <= end:
                by_episode[episode]["canonical_post_count"] = len(posts)
                for post in posts:
                    title = post.get("title")
                    if title:
                        by_episode[episode]["titles"].add(title)

        path = self.out_dir / "episode_coverage.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "episode",
                    "has_audio",
                    "raw_audio_occurrences",
                    "unique_audio_url_count",
                    "canonical_audio_occurrences",
                    "mirror_audio_occurrences",
                    "episode_page_occurrences",
                    "canonical_post_count",
                    "titles",
                ],
            )
            writer.writeheader()
            for episode in range(start, end + 1):
                entry = by_episode[episode]
                writer.writerow(
                    {
                        "episode": episode,
                        "has_audio": "yes" if entry["raw_audio_occurrences"] else "no",
                        "raw_audio_occurrences": entry["raw_audio_occurrences"],
                        "unique_audio_url_count": len(entry["unique_audio_urls"]),
                        "canonical_audio_occurrences": entry["canonical_audio_occurrences"],
                        "mirror_audio_occurrences": entry["mirror_audio_occurrences"],
                        "episode_page_occurrences": entry["episode_page_occurrences"],
                        "canonical_post_count": entry["canonical_post_count"],
                        "titles": " | ".join(sorted(entry["titles"])),
                    }
                )
        missing = [episode for episode in range(start, end + 1) if not by_episode[episode]["raw_audio_occurrences"]]
        (self.out_dir / "missing_episodes.txt").write_text(
            "\n".join(str(episode) for episode in missing) + ("\n" if missing else ""),
            encoding="utf-8",
        )
        summary = {
            "collected_at": self.collected_at,
            "start_episode": start,
            "end_episode": end,
            "raw_audio_occurrences": len(self.audio_rows),
            "episode_page_occurrences": len(self.episode_pages),
            "episodes_with_audio": end - start + 1 - len(missing),
            "missing_episodes": missing,
        }
        (self.out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return missing


def normalize_url(url: str) -> str:
    return html.unescape(url).replace("\\/", "/").strip()


def add_query(url: str, params: dict[str, str]) -> str:
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def strip_trailing_url_punctuation(url: str) -> str:
    return url.strip().rstrip(").,;]}'\"")


def squash_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    return squash_space(html.unescape(str(value).replace("\\/", "/")))


def render_field(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("rendered") or "")
    return str(value or "")


def infer_episode(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        text = html.unescape(str(value)).replace("\\/", "/")
        for pattern in EPISODE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            try:
                number = int(match.group(1))
            except ValueError:
                continue
            if 1 <= number <= 9999:
                return number
    return None


def is_bnipodcast_episode_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.endswith("bnipodcast.com") and infer_episode(parsed.path) is not None


def mirror_platform(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith("bnipodcast.com"):
        return None
    for domain, platform in MIRROR_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return None


def extract_html_title(text: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return clean_text(re.sub(r"<[^>]+>", " ", match.group(1)))


def get_xml_text(element: ET.Element, name: str) -> str | None:
    child = element.find(name)
    if child is None or child.text is None:
        return None
    return child.text


def parse_rfc2822_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return value
    return parsed.isoformat()


def parse_year_month(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    match = re.match(r"(\d{4})-(\d{2})-", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def safe_filename(value: str) -> str:
    text = urllib.parse.unquote(value)
    text = re.sub(r"https?://", "", text)
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")
    return text[:100] or "result"


def unwrap_duckduckgo_url(href: str) -> str | None:
    href = html.unescape(href)
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc or href.startswith("//duckduckgo.com"):
        query = urllib.parse.parse_qs(parsed.query)
        uddg = query.get("uddg")
        if uddg:
            return strip_trailing_url_punctuation(urllib.parse.unquote(uddg[0]))
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("http"):
        return strip_trailing_url_punctuation(href)
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="data", help="Output directory for JSONL/CSV/raw files.")
    parser.add_argument("--start", type=int, default=1, help="First episode number to cover.")
    parser.add_argument("--end", type=int, default=None, help="Last episode number. Defaults to auto-detected latest.")
    parser.add_argument("--delay", type=float, default=0.05, help="Base delay in seconds between HTTP fetches.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP User-Agent.")
    parser.add_argument("--skip-episode-pages", action="store_true", help="Do not fetch every discovered episode detail page.")
    parser.add_argument("--skip-archives", action="store_true", help="Do not crawl shows-by-year and year/month archives.")
    parser.add_argument("--search-missing", action="store_true", help="Use DuckDuckGo HTML search for still-missing episode numbers.")
    parser.add_argument("--search-mirror-domains", action="store_true", help="Also run per-domain mirror searches for missing episodes.")
    parser.add_argument("--skip-direct-probes", action="store_true", help="Do not probe predictable Blubrry MP3 filenames for still-missing episodes.")
    parser.add_argument("--max-search-results", type=int, default=8, help="Maximum search results to fetch per query.")
    parser.add_argument("--max-queries-per-missing", type=int, default=4, help="Maximum web-search queries per missing episode.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    crawler = Crawler(args)
    try:
        print("Crawling canonical home/subscribe pages...", file=sys.stderr)
        crawler.crawl_home_and_subscribe()
        print("Crawling canonical RSS feed...", file=sys.stderr)
        crawler.crawl_feed()
        print("Crawling WordPress posts API...", file=sys.stderr)
        latest = crawler.crawl_wp_posts()
        end = args.end or latest
        if not end:
            print("Could not determine latest episode; pass --end.", file=sys.stderr)
            return 2
        print(f"Using episode range {args.start}-{end}.", file=sys.stderr)
        if not args.skip_archives:
            print("Crawling shows-by-year and year/month archives...", file=sys.stderr)
            crawler.crawl_archive_pages()
        if not args.skip_episode_pages:
            print("Crawling discovered episode detail pages...", file=sys.stderr)
            crawler.crawl_episode_pages(args.start, end)

        missing = crawler.write_coverage(args.start, end)
        print(f"Missing after canonical crawl: {len(missing)}", file=sys.stderr)
        if missing:
            print("Searching canonical WordPress/site search for missing episodes...", file=sys.stderr)
            crawler.crawl_wp_search_for_missing(missing)
            missing = crawler.write_coverage(args.start, end)
            print(f"Missing after canonical search: {len(missing)}", file=sys.stderr)
        if missing and args.search_missing:
            print("Running web search for missing episodes...", file=sys.stderr)
            crawler.crawl_web_search_for_missing(missing)
            missing = crawler.write_coverage(args.start, end)
            print(f"Missing after web search: {len(missing)}", file=sys.stderr)
        if missing and not args.skip_direct_probes:
            print("Probing predictable Blubrry filenames for missing episodes...", file=sys.stderr)
            crawler.probe_direct_blubrry_audio(missing)
            missing = crawler.write_coverage(args.start, end)
            print(f"Missing after direct Blubrry probes: {len(missing)}", file=sys.stderr)
        print(f"Wrote output to {Path(args.out_dir).resolve()}", file=sys.stderr)
        return 0
    finally:
        crawler.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
