#!/usr/bin/env python3

import os
import re
import logging
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from lxml import html as lxml_html
from lxml import etree

# --------------------------------------------------
# Configuration
# --------------------------------------------------

BASE_URL = "https://en.al-akhbar.com"
NEWS_URL = f"{BASE_URL}/news"
OUTPUT_FILE = "feed.xml"

MAX_ITEMS = 50
TIMEOUT = 30

# Enable only when you have permission to republish
# complete articles.
FULL_TEXT_SYNDICATION = (
    os.environ.get("FULL_TEXT_SYNDICATION", "true").lower()
    == "true"
)

# Your supplied article body XPath.
ARTICLE_BODY_SELECTOR = ".post-details-content > div:nth-child(1)"

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

CONTENT_NS = (
    "http://purl.org/rss/1.0/modules/content/"
)

MEDIA_NS = (
    "http://search.yahoo.com/mrss/"
)

ATOM_NS = (
    "http://www.w3.org/2005/Atom"
)

ET.register_namespace("content", CONTENT_NS)
ET.register_namespace("media", MEDIA_NS)
ET.register_namespace("atom", ATOM_NS)


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def fetch(url):
    """Fetch a webpage."""

    response = session.get(
        url,
        timeout=TIMEOUT
    )

    response.raise_for_status()

    return response.text


def clean_text(value):
    """Normalize whitespace."""

    return re.sub(r"\s+", " ", value or "").strip()


def absolute_url(url, base=BASE_URL):
    """Convert relative URLs into absolute URLs."""

    if not url:
        return ""

    return urljoin(base, url.strip())


def normalize_date(value):
    """Parse a date and ensure it is timezone-aware."""

    try:
        result = date_parser.parse(value)

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result.astimezone(timezone.utc)

    except (ValueError, TypeError, OverflowError):
        return None


# --------------------------------------------------
# Article metadata
# --------------------------------------------------

def extract_date_from_soup(soup, card_text):
    """Extract publication date from article metadata."""

    candidates = [
        soup.find(
            "meta",
            property="article:published_time"
        ),
        soup.find(
            "meta",
            attrs={"name": "date"}
        ),
        soup.find(
            "meta",
            attrs={"name": "publish-date"}
        ),
    ]

    for tag in candidates:
        if tag and tag.get("content"):
            result = normalize_date(tag["content"])

            if result:
                return result

    for tag in soup.find_all("time"):
        value = (
            tag.get("datetime")
            or tag.get_text(" ", strip=True)
        )

        result = normalize_date(value)

        if result:
            return result

    # Fallback to listing card date.
    match = re.search(
        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b",
        card_text
    )

    if match:
        result = normalize_date(
            match.group()
        )

        if result:
            return result

    return None


# --------------------------------------------------
# Article body extraction
# --------------------------------------------------

def extract_article_body(html, article_url):
    """
    Extract article content using the stable article container selector.
    Preserve original article HTML and inline links.
    """

    try:
        soup = BeautifulSoup(html, "html.parser")

        body = soup.select_one(ARTICLE_BODY_SELECTOR)

        if not body:
            logging.error(
                "Article body selector not found: %s",
                article_url
            )
            return ""

        # Work on a copy so the original soup remains untouched.
        body = BeautifulSoup(
            str(body),
            "html.parser"
        ).find("div")

        if not body:
            return ""

        # Remove scripts and non-content elements.
        for element in body.select(
            "script, style, noscript, iframe"
        ):
            element.decompose()

        # Convert relative links to absolute URLs.
        for link in body.select("a[href]"):
            link["href"] = absolute_url(
                link["href"],
                article_url
            )

        # Convert relative image URLs to absolute URLs.
        for image in body.select("img"):
            for attribute in (
                "src",
                "data-src",
                "data-original"
            ):
                if image.get(attribute):
                    image[attribute] = absolute_url(
                        image[attribute],
                        article_url
                    )

        content = body.decode_contents()

        text_length = len(
            body.get_text(" ", strip=True)
        )

        logging.info(
            "Extracted article: %d characters (%s)",
            text_length,
            article_url
        )

        if text_length < 200:
            logging.warning(
                "Article body unusually short: %s",
                article_url
            )
            return ""

        return content

    except Exception:
        logging.exception(
            "Article extraction failed: %s",
            article_url
        )
        return ""


# --------------------------------------------------
# Article attribution
# --------------------------------------------------

def add_attribution(content, article_url):
    """
    Append linked publisher attribution.
    """

    attribution = etree.Element("p")

    attribution.text = "Originally published by "

    link = etree.SubElement(
        attribution,
        "a",
        href=article_url
    )

    link.text = "Al-Akhbar"

    link.tail = "."

    attribution_html = etree.tostring(
        attribution,
        encoding="unicode",
        method="html"
    )

    return (content or "") + attribution_html


# --------------------------------------------------
# Article extraction
# --------------------------------------------------

def extract_article(article_url, card_text, description):
    """
    Fetch article once and extract its metadata and body.
    """

    result = {
        "date": None,
        "content": "",
        "description": description,
    }

    try:
        html = fetch(article_url)

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        # Extract publication date.
        result["date"] = extract_date_from_soup(
            soup,
            card_text
        )

        # Extract article body.
        body_html = extract_article_body(
            html,
            article_url
        )

        if body_html:
            result["content"] = body_html

            # Use a short text excerpt as RSS description.
            body_document = lxml_html.fragment_fromstring(
                body_html,
                create_parent="div"
            )

            body_text = clean_text(
                body_document.text_content()
            )

            if not result["description"]:
                result["description"] = body_text[:500]

    except requests.RequestException as exc:
        logging.warning(
            "Could not fetch article %s: %s",
            article_url,
            exc
        )

    if not result["date"]:
        result["date"] = (
            datetime.now(timezone.utc)
        )

    return result


def extract_articles():
    logging.info(
        "Fetching Al-Akhbar news listing"
    )

    html = fetch(NEWS_URL)

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    articles = {}
    seen_urls = set()

    # Find news article links.
    for link in soup.find_all("a", href=True):

        url = absolute_url(link["href"])

        parsed = urlparse(url)

        if parsed.netloc != urlparse(BASE_URL).netloc:
            continue

        if not parsed.path.startswith("/news/"):
            continue

        if parsed.path.rstrip("/") == "/news":
            continue

        # Remove query strings and fragments.
        url = url.split("#")[0].split("?")[0]

        if url in seen_urls:
            continue

        title = clean_text(
            link.get_text(" ", strip=True)
        )

        if len(title) < 12:
            continue

        seen_urls.add(url)

        # Find surrounding listing card.
        container = link

        for _ in range(5):
            if container.parent:
                container = container.parent

        card_text = clean_text(
            container.get_text(" ", strip=True)
        )

        # Extract listing description.
        description = ""

        for paragraph in container.find_all("p"):

            candidate = clean_text(
                paragraph.get_text(" ", strip=True)
            )

            if (
                len(candidate) > 35
                and candidate != title
            ):
                description = candidate
                break

        # Extract listing image.
        image_url = ""

        image = container.find("img")

        if image:
            image_url = (
                image.get("src")
                or image.get("data-src")
                or image.get("data-original")
                or ""
            )

            if image_url:
                image_url = absolute_url(
                    image_url,
                    url
                )

        logging.info(
            "Processing article: %s",
            title
        )

        # Fetch article once.
        article_data = extract_article(
            url,
            card_text,
            description
        )

        articles[url] = {
            "title": title,
            "url": url,
            "description": (
                article_data["description"][:1000]
            ),
            "content": article_data["content"],
            "image": image_url,
            "date": article_data["date"],
        }

        if len(articles) >= MAX_ITEMS:
            break

    logging.info(
        "Found %d candidate articles",
        len(articles)
    )

    return sorted(
        articles.values(),
        key=lambda item: item["date"],
        reverse=True,
    )


# --------------------------------------------------
# RSS generation
# --------------------------------------------------

def generate_feed(articles):

    rss = ET.Element(
        "rss",
        {"version": "2.0"}
    )

    channel = ET.SubElement(
        rss,
        "channel"
    )

    ET.SubElement(
        channel,
        "title"
    ).text = "Al-Akhbar English – News"

    ET.SubElement(
        channel,
        "link"
    ).text = NEWS_URL

    ET.SubElement(
        channel,
        "description"
    ).text = (
        "Latest news articles from Al-Akhbar English."
    )

    ET.SubElement(
        channel,
        "language"
    ).text = "en"

    ET.SubElement(
        channel,
        "generator"
    ).text = "Al-Akhbar RSS Generator"

    ET.SubElement(
        channel,
        "lastBuildDate"
    ).text = format_datetime(
        datetime.now(timezone.utc)
    )

    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {
            "href": os.environ.get("FEED_URL", ""),
            "rel": "self",
            "type": "application/rss+xml",
        }
    )

    for article in articles:

        item = ET.SubElement(
            channel,
            "item"
        )

        ET.SubElement(
            item,
            "title"
        ).text = article["title"]

        ET.SubElement(
            item,
            "link"
        ).text = article["url"]

        ET.SubElement(
            item,
            "guid",
            {"isPermaLink": "true"}
        ).text = article["url"]

        ET.SubElement(
            item,
            "pubDate"
        ).text = format_datetime(
            article["date"]
        )

        ET.SubElement(
            item,
            "description"
        ).text = article["description"]

        # Publisher attribution.
        ET.SubElement(
            item,
            "source",
            {"url": BASE_URL}
        ).text = "Al-Akhbar"

        # Publish full HTML only with permission.
        if (
            FULL_TEXT_SYNDICATION
            and article["content"]
        ):

            content = add_attribution(
                article["content"],
                article["url"]
            )

        else:
            # Excerpt-only publication with linked attribution.
            excerpt = article["description"]

            content = (
                f"<p>{escape_html(excerpt)}</p>"
                f'<p>Read the full article at '
                f'<a href="{escape_html(article["url"])}">'
                f'Al-Akhbar</a>.</p>'
            )

        ET.SubElement(
            item,
            f"{{{CONTENT_NS}}}encoded"
        ).text = content

        # Thumbnail.
        if article["image"]:

            ET.SubElement(
                item,
                f"{{{MEDIA_NS}}}thumbnail",
                {"url": article["image"]}
            )

    # Write RSS XML.
    tree = ET.ElementTree(rss)

    ET.indent(
        tree,
        space="  "
    )

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
# HTML escaping
# --------------------------------------------------

def escape_html(value):
    """Escape text for safe HTML insertion."""

    from html import escape

    return escape(value or "", quote=True)


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

        logging.exception(
            "Feed generation failed"
        )

        raise
