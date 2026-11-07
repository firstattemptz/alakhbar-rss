#!/usr/bin/env python3

import os
import re
import logging
import json
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

def extract_metadata(soup, article_url):
    """Extract article metadata from JSON-LD and HTML meta tags."""

    metadata = {}

    def get_meta(property_name):
        tag = (
            soup.find("meta", property=property_name)
            or soup.find("meta", attrs={"name": property_name})
        )
        return tag.get("content", "").strip() if tag else ""

    # JSON-LD
    for script in soup.find_all(
        "script",
        type="application/ld+json"
    ):
        try:
            data = json.loads(
                script.string or script.get_text()
            )

            if isinstance(data, list):
                data = next(
                    (x for x in data if isinstance(x, dict)),
                    {}
                )

            if data.get("@type") == "NewsArticle":
                metadata["title"] = data.get("headline", "")

                author = data.get("author", [])

                if isinstance(author, dict):
                    author = [author]

                names = [
                    person.get("name", "")
                    for person in author
                    if isinstance(person, dict)
                ]

                metadata["author"] = ", ".join(
                    name for name in names if name
                )

                metadata["date"] = data.get(
                    "datePublished", ""
                )

                break

        except (ValueError, TypeError):
            continue

    # HTML metadata fallbacks
    metadata["title"] = (
        metadata.get("title")
        or get_meta("og:title")
        or (soup.title.get_text(strip=True) if soup.title else "")
    )

    metadata["author"] = (
        metadata.get("author")
        or get_meta("author")
        or "Al-Akhbar"
    )

    metadata["description"] = (
        get_meta("og:description")
        or get_meta("description")
    )

    metadata["image"] = (
        get_meta("og:image")
    )

    metadata["date"] = (
        metadata.get("date")
        or get_meta("article:published_time")
        or get_meta("date")
    )

    metadata["thumbnailCaption"] = (
    get_meta("thumbnailCaption")
    or get_meta("og:thumbnailCaption")
    or get_meta("twitter:description")
    or get_meta("og:description")
    or get_meta("description")
    )

    if metadata["image"]:
        metadata["image"] = urljoin(
            article_url,
            metadata["image"]
        )

    return metadata


# --------------------------------------------------
# Article body extraction
# --------------------------------------------------

def inspect_downloaded_page(html, article_url):
    soup = BeautifulSoup(html, "html.parser")

    logging.info("========== NEXT.JS DIAGNOSTICS ==========")

    # Identify Next.js scripts.
    for script in soup.find_all("script", src=True):
        src = absolute_url(script["src"], article_url)

        logging.info("SCRIPT: %s", src)

    # Check for React Server Component payloads.
    markers = [
        "self.__next_f.push",
        "self.__next_f",
        "self.__next",
        "post-details-content",
        "articleBody",
        "909571",
        "rubio-s-possible-exit",
    ]

    for marker in markers:
        logging.info(
            "MARKER '%s': %s",
            marker,
            marker.lower() in html.lower()
        )

    # Check for embedded article-related data.
    for script in soup.find_all("script"):
        content = script.string or script.get_text()

        if content and (
            "__next_f" in content
            or "909571" in content
        ):
            logging.info(
                "EMBEDDED DATA SCRIPT: %s",
                content[:1500]
            )

    logging.info("========== END NEXT.JS DIAGNOSTICS ==========")

def extract_article_body(html, article_url):
    """
    Extract article HTML from Next.js React Server Component data.
    """

    soup = BeautifulSoup(html, "html.parser")

    # Collect Next.js server-component payloads.
    payloads = []

    pattern = re.compile(
        r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)',
        re.DOTALL
    )

    for script in soup.find_all("script"):
        raw = script.string or script.get_text()

        if not raw:
            continue

        for match in pattern.finditer(raw):
            try:
                payloads.append(
                    json.loads(match.group(1))
                )
            except (ValueError, TypeError):
                logging.warning(
                    "Could not decode Next.js payload"
                )

    if not payloads:
        logging.error(
            "No Next.js payload found: %s",
            article_url
        )
        return ""

    stream = "\n".join(payloads)

    # Locate React Server Component text chunks.
    records = list(
        re.finditer(
            r"(?m)^([0-9a-f]+):",
            stream
        )
    )

    candidates = []

    for index, record in enumerate(records):
        header = re.match(
            r"T[0-9a-f]+,",
            stream[record.end():]
        )

        if not header:
            continue

        start = record.end() + header.end()

        end = (
            records[index + 1].start()
            if index + 1 < len(records)
            else len(stream)
        )

        chunk = stream[start:end].strip()

        if "<p" not in chunk.lower():
            continue

        candidate = BeautifulSoup(
            chunk,
            "html.parser"
        )

        text = candidate.get_text(
            " ",
            strip=True
        )

        # Reject metadata and unrelated short fragments.
        if len(text) < 300:
            continue

        if len(candidate.find_all("p")) < 2:
            continue

        candidates.append(
            (len(text), candidate)
        )

    if not candidates:
        logging.error(
            "No article text chunks found: %s",
            article_url
        )
        return ""

    # The article body is the largest substantial text chunk.
    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    text_length, body = candidates[0]

    # Remove non-editorial elements.
    for element in body.select(
        "script, style, noscript, iframe"
    ):
        element.decompose()

    # Preserve links, converting relative URLs to absolute URLs.
    for link in body.select("a[href]"):
        link["href"] = urljoin(
            article_url,
            link["href"]
        )

    # Preserve images and their absolute URLs.
    for image in body.select("img"):
        for attribute in (
            "src",
            "data-src",
            "data-original"
        ):
            if image.get(attribute):
                image[attribute] = urljoin(
                    article_url,
                    image[attribute]
                )

    content = body.decode_contents()

    logging.info(
        "SUCCESS: Extracted %d characters of article text",
        text_length
    )

    logging.info(
        "Article HTML length: %d",
        len(content)
    )

    return content

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

def extract_article(article_url):

    logging.info("Processing article: %s", article_url)

    try:
        html = fetch(article_url)

        soup = BeautifulSoup(html, "html.parser")

        # 1. Extract metadata
        metadata = extract_metadata(soup, article_url)

        # 2. Extract full article text
        body_html = extract_article_body(
            html,
            article_url
        )

        # 3. Build article dictionary
        article = {
            "title": metadata["title"],
            "author": metadata["author"],
            "description": metadata["description"],
            "image": metadata["image"],
            "thumbnailCaption": metadata["thumbnailCaption"],
            "date": (
                date_parser.parse(metadata["date"])
                if metadata["date"]
                else datetime.now(timezone.utc)
            ),
            "url": article_url,
            "content": body_html,
        }

        return article

    except Exception:
        logging.exception(
            "Failed to extract article: %s",
            article_url
        )
        return None



def extract_articles():
    logging.info("Fetching Al-Akhbar news listing")

    html = fetch(NEWS_URL)

    soup = BeautifulSoup(html, "html.parser")

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

        # Listing title is used only as a preliminary validation.
        listing_title = clean_text(
            link.get_text(" ", strip=True)
        )

        if len(listing_title) < 12:
            continue

        seen_urls.add(url)

        logging.info(
            "Processing article: %s",
            url
        )

        # Extract all metadata and content from the article page.
        article_data = extract_article(url)

        # Skip failed extractions.
        if not article_data:
            logging.warning(
                "Skipping article because extraction failed: %s",
                url
            )
            continue

        # Use metadata extracted from the article itself.
        title = clean_text(
            article_data.get("title")
            or listing_title
        )

        description = clean_text(
        article_data.get("thumbnailCaption")
        or article_data.get("description")
        or ""
        )

        image = article_data.get("image") or ""

        content = article_data.get("content") or ""

        date = article_data.get("date")

        if not date:
            date = datetime.now(timezone.utc)

        # Ensure timezone-aware dates.
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)

        articles[url] = {
            "title": title,
            "url": url,
            "description": description[:1000],
            "content": content,
            "image": image,
            "date": date,
            "author": article_data.get("author", "Al-Akhbar"),
        }

        logging.info(
            "Successfully extracted: %s",
            title
        )

        if len(articles) >= MAX_ITEMS:
            break

    logging.info(
        "Found %d successfully extracted articles",
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
        logging.info(
            "SYNDICATION CHECK: enabled=%s, content_length=%d, "
            "description_length=%d",
            FULL_TEXT_SYNDICATION,
            len(article.get("content", "")),
            len(article.get("description", ""))
        )
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
