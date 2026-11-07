#!/usr/bin/env python3

import os
import re
import hashlib
import logging
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

# --------------------------------------------------
# Configuration
# --------------------------------------------------

BASE_URL = "https://en.al-akhbar.com"
NEWS_URL = f"{BASE_URL}/news"
OUTPUT_FILE = "feed.xml"

MAX_ITEMS = 50
TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AlAkhbarRSS/1.0; "
        "+https://github.com/)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

session = requests.Session()
session.headers.update(HEADERS)


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def fetch(url):
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def absolute_url(url):
    return urljoin(BASE_URL, url)


def article_date(article_url, card_text):
    """Try article metadata first, then listing text."""

    try:
        html = fetch(article_url)
        soup = BeautifulSoup(html, "html.parser")

        candidates = [
            soup.find("meta", property="article:published_time"),
            soup.find("meta", attrs={"name": "date"}),
            soup.find("meta", attrs={"name": "publish-date"}),
        ]

        for tag in candidates:
            if tag and tag.get("content"):
                try:
                    return date_parser.parse(
                        tag["content"]
                    ).replace(tzinfo=timezone.utc)
                except (ValueError, TypeError, OverflowError):
                    pass

        for tag in soup.find_all("time"):
            value = tag.get("datetime") or tag.get_text(" ", strip=True)
            if value:
                try:
                    return date_parser.parse(value).replace(
                        tzinfo=timezone.utc
                    )
                except (ValueError, TypeError, OverflowError):
                    pass

    except requests.RequestException as exc:
        logging.warning("Could not fetch article metadata: %s", exc)

    # Fallback: look for a date in the listing card.
    try:
        match = re.search(
            r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b",
            card_text
        )
        if match:
            return date_parser.parse(match.group()).replace(
                tzinfo=timezone.utc
            )
    except (ValueError, TypeError, OverflowError):
        pass

    return datetime.now(timezone.utc)


def extract_articles():
    logging.info("Fetching Al-Akhbar news listing")

    html = fetch(NEWS_URL)
    soup = BeautifulSoup(html, "html.parser")

    articles = {}
    seen_urls = set()

    # News article URLs use /news/...
    for link in soup.find_all("a", href=True):
        url = absolute_url(link["href"])
        parsed = urlparse(url)

        if parsed.netloc != urlparse(BASE_URL).netloc:
            continue

        if not parsed.path.startswith("/news/"):
            continue

        if parsed.path.rstrip("/") == "/news":
            continue

        url = url.split("#")[0].split("?")[0]

        if url in seen_urls:
            continue

        title = clean_text(link.get_text(" ", strip=True))

        # Avoid empty links and navigation elements.
        if len(title) < 12:
            continue

        seen_urls.add(url)

        # Try to find the surrounding article card.
        container = link
        for _ in range(5):
            if container.parent:
                container = container.parent

        card_text = clean_text(
            container.get_text(" ", strip=True)
        )

        # Extract a short introductory description.
        description = ""
        for paragraph in container.find_all("p"):
            candidate = clean_text(
                paragraph.get_text(" ", strip=True)
            )
            if len(candidate) > 35 and candidate != title:
                description = candidate
                break

        image_url = ""
        image = container.find("img")
        if image:
            image_url = (
                image.get("src")
                or image.get("data-src")
                or ""
            )
            if image_url:
                image_url = absolute_url(image_url)

        articles[url] = {
            "title": title,
            "url": url,
            "description": description[:1000],
            "image": image_url,
            "date": article_date(url, card_text),
        }

        if len(articles) >= MAX_ITEMS:
            break

    logging.info("Found %d candidate articles", len(articles))

    return sorted(
        articles.values(),
        key=lambda item: item["date"],
        reverse=True,
    )


# --------------------------------------------------
# RSS generation
# --------------------------------------------------

def generate_feed(articles):

    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
        "xmlns:content": "http://purl.org/rss/1.0/modules/content/",
        "xmlns:media": "http://search.yahoo.com/mrss/",
    })

    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = (
        "Al-Akhbar English – News"
    )

    ET.SubElement(channel, "link").text = NEWS_URL

    ET.SubElement(channel, "description").text = (
        "Latest news articles from Al-Akhbar English."
    )

    ET.SubElement(channel, "language").text = "en"

    ET.SubElement(channel, "generator").text = (
        "Al-Akhbar RSS Generator"
    )

    ET.SubElement(channel, "lastBuildDate").text = (
        format_datetime(datetime.now(timezone.utc))
    )

    ET.SubElement(
        channel,
        "{http://www.w3.org/2005/Atom}link",
        {
            "href": os.environ.get("FEED_URL", ""),
            "rel": "self",
            "type": "application/rss+xml",
        }
    )

    for article in articles:
        item = ET.SubElement(channel, "item")

        ET.SubElement(item, "title").text = article["title"]
        ET.SubElement(item, "link").text = article["url"]

        ET.SubElement(item, "guid", {
            "isPermaLink": "true"
        }).text = article["url"]

        ET.SubElement(item, "pubDate").text = (
            format_datetime(article["date"])
        )

        ET.SubElement(item, "description").text = (
            article["description"]
        )

        # Preserve the original article URL for Feeder.
        ET.SubElement(item, "source", {
            "url": BASE_URL
        }).text = "Al-Akhbar"

        if article["image"]:
            ET.SubElement(
                item,
                "{http://search.yahoo.com/mrss/}thumbnail",
                {"url": article["image"]}
            )

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")

    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True
    )

    logging.info(
        "Successfully generated %s with %d articles",
        OUTPUT_FILE,
        len(articles)
    )


# --------------------------------------------------
# Main
# --------------------------------------------------

if __name__ == "__main__":
    try:
        articles = extract_articles()

        if not articles:
            raise RuntimeError(
                "No articles found. Refusing to overwrite feed."
            )

        generate_feed(articles)

    except Exception:
        logging.exception("Feed generation failed")
        raise
