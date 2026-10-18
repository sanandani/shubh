#!/usr/bin/env python3
"""Dependency-free article metadata loading, feed generation, and validation."""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://www.imshubh.com/"
FEED_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}
for prefix, uri in FEED_NS.items():
    ET.register_namespace(prefix, uri)
ET.register_namespace("", "")

ARTICLE_TEMPLATE = (ROOT / "templates/article.html").read_text(encoding="utf-8")




def parse_iso(value: str, context: str) -> datetime:
    if not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", value):
        raise ValueError(f"{context}: publish/updated date must include timezone: {value!r}")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError(f"{context}: date has no timezone: {value!r}")
    return result


def normalize_existing_date(value: str) -> str:
    """Give a legacy visible publication day a deterministic local timestamp."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = date.fromisoformat(value)
        return datetime.combine(day, time(12, 0), ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds")
    return parse_iso(value, "publish date").isoformat(timespec="seconds")


class ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.head = True
        self.data: dict[str, str] = {}
        self.h1 = False
        self.time = False
        self.title = ""
        self.visible_date = ""
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs)
        if tag == "body":
            self.head = False
        if tag == "h1":
            self.h1 = True
        if tag == "time":
            self.time = True
            self.visible_date = attrs.get("datetime", "") or ""
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name") or ""
            if key:
                self.data[key] = attrs.get("content", "") or ""
        if tag == "link" and attrs.get("rel") == "canonical":
            self.data["canonical"] = attrs.get("href", "") or ""
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.data["jsonld"] = ""
        if attrs.get("id"):
            self.ids.add(attrs["id"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self.h1 = False
        elif tag == "time":
            self.time = False

    def handle_data(self, data: str) -> None:
        if self.h1:
            self.title += data
        if self.time:
            self.visible_date += data
        if "jsonld" in self.data and not self.data["jsonld"]:
            # JSON-LD may be delivered in one or several parser data callbacks.
            self.data["jsonld"] += data


class StrictArticleParser(HTMLParser):
    """Extract exact metadata and visible date from an article page."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.article_tags: list[str] = []
        self.canonical = ""
        self.alternates: list[tuple[str, str]] = []
        self.h1 = False
        self.time = False
        self.title_parts: list[str] = []
        self.date_parts: list[str] = []
        self.visible_datetime = ""
        self.jsonld = False
        self.jsonld_parts: list[str] = []
        self.imgs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key:
                self.meta[key] = a.get("content", "")
                if key == "article:tag":
                    self.article_tags.append(a.get("content", ""))
        elif tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href", "")
        elif tag == "link" and a.get("rel") == "alternate":
            self.alternates.append((a.get("type", ""), a.get("href", "")))
        elif tag == "h1":
            self.h1 = True
        elif tag == "time":
            self.time = True
            self.visible_datetime = a.get("datetime", "")
        elif tag == "script" and a.get("type") == "application/ld+json":
            self.jsonld = True
        elif tag == "img":
            self.imgs.append(a)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self.h1 = False
        elif tag == "time":
            self.time = False
        elif tag == "script":
            self.jsonld = False

    def handle_data(self, data: str) -> None:
        if self.h1:
            self.title_parts.append(data)
        if self.time:
            self.date_parts.append(data)
        if self.jsonld:
            self.jsonld_parts.append(data)

    @property
    def title(self) -> str:
        return "".join(self.title_parts).strip()

    @property
    def visible_date(self) -> str:
        return "".join(self.date_parts).strip()

    @property
    def structured_data(self) -> dict:
        return json.loads("".join(self.jsonld_parts))


class VisibleLinkParser(HTMLParser):
    """Collect visible text while excluding code, raw-text blocks, and anchors."""
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    EXCLUDED_TAGS = {"pre", "code", "script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        pass

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        hidden = bool(set(self.stack) & (self.EXCLUDED_TAGS | {"a"}))
        self.parts.append(" " if hidden else data)


VISIBLE_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(https?://[^)\s]+\)")
VISIBLE_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
SOURCE_LIST = re.compile(r"(<h2\b[^>]*>\s*[^<]*\bSources\b[^<]*</h2>\s*<ul\b[^>]*>)(.*?)(</ul>)", re.I | re.S)
SOURCE_ITEM = re.compile(r"<li\b[^>]*>.*?</li>", re.I | re.S)
SOURCE_TEXT_TOKEN = re.compile(r"<a\b[^>]*>.*?</a>|<code\b[^>]*>.*?</code>|<[^>]+>|[^<]+", re.I | re.S)
SOURCE_MARKDOWN = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
SOURCE_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def visible_unlinked_links(text: str) -> tuple[int, int]:
    markdown = list(VISIBLE_MARKDOWN_LINK.finditer(text))
    bare = [match for match in VISIBLE_URL.finditer(text)
            if not any(match.start() < link.end() and match.end() > link.start() for link in markdown)]
    return len(markdown), len(bare)


def unlinked_source_items(body: str) -> int:
    return sum(1 for section in SOURCE_LIST.finditer(body)
               for item in SOURCE_ITEM.finditer(section.group(2))
               if not re.search(r"<a\b", item.group(), re.I))


def clip_meta_description(value: str) -> str:
    value = re.sub(r"\s+", " ", html.unescape(value)).strip()
    if len(value) <= 155:
        return value
    shortened = value[:154]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,;:-") + "…"


def linkify_source_text(text: str) -> str:
    text = html.unescape(text)
    markdown = list(SOURCE_MARKDOWN.finditer(text))
    replacements: list[tuple[int, int, str]] = []

    def anchor(url: str, label: str) -> str:
        return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">{html.escape(label, quote=False)}</a>'

    def clean_url(value: str) -> tuple[str, str]:
        suffix = ""
        while value and value[-1] in ".,;:!?":
            suffix = value[-1] + suffix
            value = value[:-1]
        while value.endswith(")") and value.count(")") > value.count("("):
            suffix = ")" + suffix
            value = value[:-1]
        return value, suffix

    for match in markdown:
        url, suffix = clean_url(match.group(2))
        replacements.append((match.start(), match.end(), anchor(url, match.group(1)) + suffix))
    for match in SOURCE_URL.finditer(text):
        if any(match.start() < item.end() and match.end() > item.start() for item in markdown):
            continue
        url, suffix = clean_url(match.group())
        if url:
            replacements.append((match.start(), match.start() + len(url), anchor(url, url)))
    if not replacements:
        return html.escape(text, quote=False) if text != html.unescape(text) else text
    result: list[str] = []
    last = 0
    for start, end, value in sorted(replacements):
        result.extend((html.escape(text[last:start], quote=False), value))
        last = end
    result.append(html.escape(text[last:], quote=False))
    return "".join(result)


def linkify_source_item(item: str) -> str:
    return "".join(token if token.startswith("<") else linkify_source_text(token)
                   for token in SOURCE_TEXT_TOKEN.findall(item))


def linkify_sources(body: str) -> str:
    def replace_section(match: re.Match) -> str:
        contents = SOURCE_ITEM.sub(lambda item: linkify_source_item(item.group()), match.group(2))
        return match.group(1) + contents + match.group(3)
    return SOURCE_LIST.sub(replace_section, body)


def validate_article_link_css(css: str) -> list[str]:
    errors: list[str] = []
    rules = [(selectors, declarations) for selectors, declarations in
             re.findall(r"([^{}]+)\{([^{}]*)\}", css, re.S)]

    def article_anchor(selector: str) -> bool:
        has_anchor = bool(re.search(r"(?:^|[\s>+~])a(?=[:.#\s]|$)", selector.strip()))
        article_context = bool(re.search(r"(?:^|[\s>+~])(?:body|main|article|ul|li)(?=[\s>.:#]|$)|\.(?:article|prose|post|content|sources|references)(?:[-_\w]*)?", selector))
        return has_anchor and (article_context or selector.strip() == "a")

    matched = []
    for selectors, declarations in rules:
        selected = [item.strip() for item in selectors.split(",")]
        if not any(article_anchor(item) for item in selected):
            continue
        props = {name.strip().lower(): value.strip() for name, value in
                 re.findall(r"([\w-]+)\s*:\s*([^;]+)", declarations)}
        matched.append((selected, props))
        if props.get("pointer-events", "").lower() == "none":
            errors.append("pointer-events:none applies to article-content links")

    base = [props for selectors, props in matched
            if any(".article-prose" in selector and re.search(r"(?:^|\s)a(?=[:.#\s]|$)", selector)
                   and not re.search(r":(?:visited|hover|focus-visible|active)", selector)
                   for selector in selectors)]
    base_colors = {props.get("color", "").lower() for props in base}
    good_color = any(color and color not in {"inherit", "var(--ink)", "var(--muted)"} for color in base_colors)
    good_decoration = any(props.get("text-decoration", "").lower() not in {"", "none"} or
                          props.get("text-decoration-line", "").lower() not in {"", "none"}
                          for props in base)
    if not good_color:
        errors.append("article-content links need a distinct color")
    if not good_decoration:
        errors.append("article-content links need visible text decoration")
    visited_colors = {props.get("color", "").lower() for selectors, props in matched
                      if any(":visited" in selector and ".article-prose" in selector for selector in selectors)}
    if not visited_colors or not any(color and color not in base_colors for color in visited_colors):
        errors.append("article-content visited links need a distinct color")
    return errors


def load_articles() -> list[dict]:
    source = ROOT / "data/articles.json"
    articles = json.loads(source.read_text())
    if not isinstance(articles, list):
        raise ValueError("data/articles.json must contain an array")
    return articles


def page_meta(article: dict) -> StrictArticleParser:
    parser = StrictArticleParser()
    parser.feed((ROOT / article["page"]).read_text())
    return parser


def absolute_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"https", "http"} and bool(parsed.netloc)


def atom_date(value: str) -> str:
    return parse_iso(value, "date").isoformat(timespec="seconds")


def render_feeds(articles: list[dict]) -> None:
    sorted_articles = sorted(articles, key=lambda item: parse_iso(item["published"], item["slug"]), reverse=True)
    newest = parse_iso(sorted_articles[0]["published"], "newest article")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Shubham Anandani — Writing"
    ET.SubElement(channel, "link").text = BASE_URL + "blog.html"
    ET.SubElement(channel, "description").text = "Engineering notes on identity, AI, systems, and open source by Shubham Anandani."
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(newest)
    ET.SubElement(channel, f"{{{FEED_NS['atom']}}}link", {"href": BASE_URL + "rss.xml", "rel": "self", "type": "application/rss+xml"})
    ET.SubElement(channel, "managingEditor").text = "shubhamanandani@gmail.com (Shubham Anandani)"
    image = ET.SubElement(channel, "image")
    ET.SubElement(image, "url").text = BASE_URL + "assets/shubham-anandani.jpg"
    ET.SubElement(image, "title").text = "Shubham Anandani"
    ET.SubElement(image, "link").text = BASE_URL
    for article in sorted_articles:
        url = article["url"]
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = article["title"]
        ET.SubElement(item, "link").text = url
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = url
        ET.SubElement(item, "pubDate").text = format_datetime(parse_iso(article["published"], article["slug"]))
        ET.SubElement(item, f"{{{FEED_NS['dc']}}}creator").text = article["author"]
        ET.SubElement(item, "description").text = article["summary"]
        for tag in article["tags"]:
            ET.SubElement(item, "category").text = tag
        cover_path = ROOT / urlparse(article["cover"]).path.lstrip("/")
        ET.SubElement(item, "enclosure", {"url": article["cover"], "type": "image/jpeg", "length": str(cover_path.stat().st_size)})
        content = make_content_absolute(article["body"], url)
        ET.SubElement(item, f"{{{FEED_NS['content']}}}encoded").text = content
    write_rss_with_cdata(rss, ROOT / "rss.xml")

    atom = ET.Element(f"{{{FEED_NS['atom']}}}feed")
    ET.SubElement(atom, f"{{{FEED_NS['atom']}}}title").text = "Shubham Anandani — Writing"
    ET.SubElement(atom, f"{{{FEED_NS['atom']}}}id").text = BASE_URL + "blog.html"
    ET.SubElement(atom, f"{{{FEED_NS['atom']}}}updated").text = atom_date(newest.isoformat())
    ET.SubElement(atom, f"{{{FEED_NS['atom']}}}link", {"href": BASE_URL + "atom.xml", "rel": "self", "type": "application/atom+xml"})
    ET.SubElement(atom, f"{{{FEED_NS['atom']}}}link", {"href": BASE_URL + "blog.html", "rel": "alternate"})
    for article in sorted_articles:
        entry = ET.SubElement(atom, f"{{{FEED_NS['atom']}}}entry")
        ET.SubElement(entry, f"{{{FEED_NS['atom']}}}title").text = article["title"]
        ET.SubElement(entry, f"{{{FEED_NS['atom']}}}id").text = article["url"]
        ET.SubElement(entry, f"{{{FEED_NS['atom']}}}link", {"href": article["url"], "rel": "alternate"})
        ET.SubElement(entry, f"{{{FEED_NS['atom']}}}published").text = atom_date(article["published"])
        ET.SubElement(entry, f"{{{FEED_NS['atom']}}}updated").text = atom_date(article.get("updated", article["published"]))
        author = ET.SubElement(entry, f"{{{FEED_NS['atom']}}}author")
        ET.SubElement(author, f"{{{FEED_NS['atom']}}}name").text = article["author"]
        ET.SubElement(entry, f"{{{FEED_NS['atom']}}}summary").text = article["summary"]
        for tag in article["tags"]:
            ET.SubElement(entry, f"{{{FEED_NS['atom']}}}category", {"term": tag})
        content = make_content_absolute(article["body"], article["url"])
        ET.SubElement(entry, f"{{{FEED_NS['atom']}}}content", {"type": "html"}).text = content
    (ROOT / "atom.xml").write_bytes(xml_bytes(atom))


def make_content_absolute(content: str, page_url: str) -> str:
    def fix(match: re.Match) -> str:
        prefix, quote, value = match.group(1), match.group(2), match.group(3)
        if not value or value.startswith(("#", "mailto:", "tel:", "data:", "javascript:")):
            if value.startswith("#"):
                value = page_url + value
            return f"{prefix}{quote}{value}{quote}"
        if value.startswith("//"):
            value = "https:" + value
        elif not absolute_url(value):
            value = urljoin(page_url, value)
        return f"{prefix}{quote}{value}{quote}"
    return re.sub(r'((?:href|src)\s*=\s*)([\"\'])(.*?)\2', fix, content, flags=re.I)


def write_rss_with_cdata(element: ET.Element, path: Path) -> None:
    replacements: dict[str, str] = {}
    for index, node in enumerate(element.iter(f"{{{FEED_NS['content']}}}encoded")):
        token = f"__ARTICLE_CDATA_{index}__"
        replacements[token] = node.text or ""
        node.text = token
    raw = ET.tostring(element, encoding="unicode", xml_declaration=False)
    for token, value in replacements.items():
        raw = raw.replace(token, "<![CDATA[" + value.replace("]]>", "]] ]]><![CDATA[>".replace(" ", "")) + "]]>" )
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + raw)


def xml_bytes(element: ET.Element) -> bytes:
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(element, encoding="utf-8", xml_declaration=False)


def render_logs(articles: list[dict]) -> None:
    ordered = sorted(articles, key=lambda item: parse_iso(item["published"], item["slug"]), reverse=True)
    with (ROOT / "publishing-log.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["title", "canonical_url", "publish_date", "tags", "summary"])
        for article in ordered:
            writer.writerow([article["title"], article["url"], article["published"], ", ".join(article["tags"]), article["summary"]])
    rows = "\n".join(
        "<tr><td>" + html.escape(a["title"]) + "</td><td><a href=\"" + html.escape(a["url"], quote=True) + "\">" + html.escape(a["url"]) + "</a></td><td><time datetime=\"" + html.escape(a["published"], quote=True) + "\">" + html.escape(a["published"]) + "</time></td><td>" + html.escape(", ".join(a["tags"])) + "</td><td>" + html.escape(a["summary"]) + "</td></tr>"
        for a in ordered
    )
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Publishing log — Shubham Anandani</title><link rel="alternate" type="application/rss+xml" title="RSS" href="rss.xml"><link rel="alternate" type="application/atom+xml" title="Atom" href="atom.xml"><style>body{font:16px/1.55 system-ui,sans-serif;max-width:1200px;margin:3rem auto;padding:0 1.25rem;color:#181a36}table{border-collapse:collapse;width:100%}th,td{padding:.65rem;border:1px solid #ddd;text-align:left;vertical-align:top}time{white-space:nowrap}</style></head><body><h1>Publishing log</h1><p>Copy verified publication dates, canonical URLs, tags, and summaries into Hashnode or Zenodo records.</p><p><a href="publishing-log.csv">Download CSV</a> · <a href="blog.html">Writing archive</a></p><table><thead><tr><th>Title</th><th>Canonical URL</th><th>Publish date</th><th>Tags</th><th>Summary</th></tr></thead><tbody>''' + rows + "</tbody></table></body></html>"
    (ROOT / "publishing-log.html").write_text(page)


def render_sitemap(articles: list[dict]) -> None:
    root = ET.Element("urlset", {"xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9"})
    urls = [(BASE_URL, None), (BASE_URL + "blog.html", None), (BASE_URL + "publishing-log.html", None)] + [(a["url"], a) for a in articles]
    for loc, article in urls:
        node = ET.SubElement(root, "url")
        ET.SubElement(node, "loc").text = loc
    (ROOT / "sitemap.xml").write_bytes(xml_bytes(root))


def validate(articles: list[dict]) -> list[str]:
    errors: list[str] = []
    required = {"title", "slug", "page", "published", "summary", "tags", "cover", "author", "url"}
    by_url: dict[str, dict] = {}
    for article in articles:
        slug = article.get("slug", "(missing-slug)")
        missing = required - article.keys()
        if missing:
            errors.append(f"{slug}: missing fields {sorted(missing)}")
            continue
        if article["url"] in by_url:
            errors.append(f"duplicate canonical URL/guid: {article['url']}")
        by_url[article["url"]] = article
        for key in ("url", "cover"):
            if not absolute_url(article[key]):
                errors.append(f"{slug}: {key} must be absolute: {article[key]}")
        if article["url"] != BASE_URL + article["page"]:
            errors.append(f"{slug}: canonical URL/page mismatch")
        try:
            published = parse_iso(article["published"], slug)
            if "updated" in article and parse_iso(article["updated"], slug + " updated") < published:
                errors.append(f"{slug}: updated date precedes publish date")
        except (ValueError, TypeError) as exc:
            errors.append(str(exc))
            continue
        if not isinstance(article["tags"], list) or len(article["tags"]) > 4:
            errors.append(f"{slug}: tags must be a list with at most four entries")
        for tag in article.get("tags", []):
            if not re.fullmatch(r"[a-z0-9]{1,20}", tag):
                errors.append(f"{slug}: invalid feed category {tag!r}")
        page = ROOT / article["page"]
        if not page.is_file():
            errors.append(f"{slug}: page missing: {article['page']}")
            continue
        page_html = page.read_text(encoding="utf-8")
        missing_source_links = unlinked_source_items(page_html)
        if missing_source_links:
            errors.append(f"{slug}: {missing_source_links} Sources list items missing links")
        parser = StrictArticleParser()
        try:
            parser.feed(page_html); parser.close()
        except Exception as exc:
            errors.append(f"{slug}: invalid HTML: {exc}")
            continue
        if parser.canonical != article["url"]:
            errors.append(f"{slug}: missing or incorrect self-referential canonical")
        required_feeds = {("application/rss+xml", BASE_URL + "rss.xml"), ("application/atom+xml", BASE_URL + "atom.xml")}
        if not required_feeds.issubset(set(parser.alternates)):
            errors.append(f"{slug}: missing RSS or Atom alternate link")
        published_meta = parser.meta.get("article:published_time", "")
        if not published_meta:
            errors.append(f"{slug}: missing article:published_time")
        if published_meta != article["published"]:
            errors.append(f"{slug}: article:published_time mismatch")
        if parser.visible_datetime != article["published"]:
            errors.append(f"{slug}: visible publish date mismatch")
        if parser.visible_date != "Published " + published.strftime("%B %-d, %Y"):
            errors.append(f"{slug}: visible publish date text mismatch")
        if parser.title != article["title"]:
            errors.append(f"{slug}: visible headline mismatch")
        if any(not img.get("alt") for img in parser.imgs):
            errors.append(f"{slug}: article image missing alt text")
        for prop, expected in (("og:type", "article"), ("og:title", article["title"]),
            ("og:description", clip_meta_description(article["summary"])), ("og:url", article["url"]), ("og:image", article["cover"]),
            ("article:author", article["author"])):
            if parser.meta.get(prop) != expected:
                errors.append(f"{slug}: {prop} mismatch")
        if parser.article_tags != article["tags"]:
            errors.append(f"{slug}: article:tag values mismatch")
        if bool(article.get("updated")) != ("article:modified_time" in parser.meta):
            errors.append(f"{slug}: article:modified_time must appear only for updated articles")
        if article.get("updated") and parser.meta.get("article:modified_time") != article["updated"]:
            errors.append(f"{slug}: article:modified_time mismatch")
        try:
            ld = parser.structured_data
            if ld.get("@type") != "Article" or ld.get("headline") != article["title"]:
                errors.append(f"{slug}: Article JSON-LD headline/type mismatch")
            if ld.get("datePublished") != article["published"]:
                errors.append(f"{slug}: JSON-LD published date mismatch")
            if ld.get("image") != article["cover"]:
                errors.append(f"{slug}: JSON-LD image mismatch")
            if ld.get("author", {}).get("name") != article["author"]:
                errors.append(f"{slug}: JSON-LD author mismatch")
            if article.get("updated"):
                if ld.get("dateModified") != article["updated"]:
                    errors.append(f"{slug}: JSON-LD modified date mismatch")
            elif "dateModified" in ld:
                errors.append(f"{slug}: JSON-LD dateModified must be omitted unless updated")
        except (ValueError, TypeError):
            errors.append(f"{slug}: missing or invalid Article JSON-LD")

    rss_dates: dict[str, str] = {}
    atom_dates: dict[str, str] = {}
    try:
        rss_root = ET.parse(ROOT / "rss.xml").getroot()
        channel = rss_root.find("channel")
        if rss_root.tag != "rss" or rss_root.get("version") != "2.0" or channel is None:
            errors.append("rss.xml: invalid RSS 2.0 channel")
        else:
            for child in ("title", "link", "description", "language", "lastBuildDate", "managingEditor", "image"):
                if channel.find(child) is None:
                    errors.append(f"rss.xml: missing channel {child}")
            self_link = channel.find(f"{{{FEED_NS['atom']}}}link")
            if self_link is None or self_link.get("rel") != "self" or self_link.get("href") != BASE_URL + "rss.xml":
                errors.append("rss.xml: missing atom self link")
            channel_link = channel.findtext("link", "")
            if not absolute_url(channel_link):
                errors.append("rss.xml: relative channel link")
            items = channel.findall("item")
            if len(items) != len(articles):
                errors.append(f"rss.xml: expected {len(articles)} items, found {len(items)}")
            guids: set[str] = set()
            for item in items:
                link = item.findtext("link", "")
                guid = item.findtext("guid", "")
                if not guid:
                    errors.append(f"rss.xml: missing guid for {link}")
                if guid in guids:
                    errors.append(f"rss.xml: duplicate guid {guid}")
                guids.add(guid)
                if guid != link:
                    errors.append(f"rss.xml: guid must equal permalink {link}")
                if not absolute_url(link):
                    errors.append(f"rss.xml: relative link {link}")
                article = by_url.get(link)
                if not article:
                    errors.append(f"rss.xml: unknown article link {link}")
                    continue
                if item.findtext("title", "") != article["title"]:
                    errors.append(f"rss.xml: title mismatch for {link}")
                if item.findtext("description", "") != article["summary"]:
                    errors.append(f"rss.xml: summary mismatch for {link}")
                if item.findtext(f"{{{FEED_NS['dc']}}}creator", "") != article["author"]:
                    errors.append(f"rss.xml: creator mismatch for {link}")
                pub = item.findtext("pubDate", "")
                if not pub:
                    errors.append(f"rss.xml: missing pubDate for {link}")
                else:
                    try: rss_dates[link] = parsedate_to_datetime(pub).isoformat(timespec="seconds")
                    except (TypeError, ValueError): errors.append(f"rss.xml: invalid pubDate for {link}")
                categories = item.findall("category")
                if len(categories) > 4:
                    errors.append(f"rss.xml: more than four categories for {link}")
                if [c.text or "" for c in categories] != article["tags"]:
                    errors.append(f"rss.xml: category list mismatch for {link}")
                if any(not re.fullmatch(r"[a-z0-9]{1,20}", c.text or "") for c in categories):
                    errors.append(f"rss.xml: invalid category for {link}")
                enclosure = item.find("enclosure")
                if enclosure is None or not absolute_url(enclosure.get("url", "")):
                    errors.append(f"rss.xml: missing absolute cover enclosure for {link}")
                elif enclosure.get("url") != article["cover"]:
                    errors.append(f"rss.xml: cover mismatch for {link}")
                encoded = item.find(f"{{{FEED_NS['content']}}}encoded")
                expected = make_content_absolute(article["body"], link)
                if encoded is None or not encoded.text or encoded.text != expected:
                    errors.append(f"rss.xml: missing or incomplete full article content for {link}")
                elif re.search(r'(?:href|src)=["\'](?:\.\.?/|/|#)', encoded.text, re.I):
                    errors.append(f"rss.xml: relative URL inside content for {link}")
    except (ET.ParseError, OSError) as exc:
        errors.append(f"rss.xml: invalid or missing XML: {exc}")

    try:
        atom_root = ET.parse(ROOT / "atom.xml").getroot()
        ns = "{" + FEED_NS["atom"] + "}"
        for link in atom_root.findall(ns + "link"):
            if not absolute_url(link.get("href", "")):
                errors.append(f"atom.xml: relative feed URL {link.get('href', '')}")
        entries = atom_root.findall(ns + "entry")
        if len(entries) != len(articles):
            errors.append(f"atom.xml: expected {len(articles)} entries, found {len(entries)}")
        for entry in entries:
            link_node = next((e for e in entry.findall(ns + "link") if e.get("rel") == "alternate"), None)
            link = link_node.get("href", "") if link_node is not None else ""
            if not absolute_url(link):
                errors.append(f"atom.xml: relative link {link}")
            article = by_url.get(link)
            if not article:
                errors.append(f"atom.xml: unknown article link {link}")
                continue
            if entry.findtext(ns + "title", "") != article["title"]:
                errors.append(f"atom.xml: title mismatch for {link}")
            if entry.findtext(ns + "summary", "") != article["summary"]:
                errors.append(f"atom.xml: summary mismatch for {link}")
            if entry.findtext(ns + "author/" + ns + "name", "") != article["author"]:
                errors.append(f"atom.xml: author mismatch for {link}")
            if [c.get("term", "") for c in entry.findall(ns + "category")] != article["tags"]:
                errors.append(f"atom.xml: category list mismatch for {link}")
            published_text = entry.findtext(ns + "published", "")
            if not published_text:
                errors.append(f"atom.xml: missing published date for {link}")
            else:
                try: atom_dates[link] = parse_iso(published_text, "Atom published").isoformat(timespec="seconds")
                except ValueError: errors.append(f"atom.xml: invalid published date for {link}")
            content = entry.find(ns + "content")
            expected = make_content_absolute(article["body"], link)
            if content is None or not content.text or content.text != expected:
                errors.append(f"atom.xml: missing or incomplete full article content for {link}")
            elif re.search(r'(?:href|src)=["\'](?:\.\.?/|/|#)', content.text, re.I):
                errors.append(f"atom.xml: relative URL inside content for {link}")
    except (ET.ParseError, OSError) as exc:
        errors.append(f"atom.xml: invalid or missing XML: {exc}")

    try:
        with (ROOT / "publishing-log.csv").open(newline="", encoding="utf-8") as f:
            log_rows = list(csv.DictReader(f))
        if len(log_rows) != len(articles):
            errors.append("publishing-log.csv: item count mismatch")
        for row in log_rows:
            article = by_url.get(row.get("canonical_url", ""))
            if not article:
                errors.append(f"publishing-log.csv: unknown canonical URL {row.get('canonical_url')}")
                continue
            if row.get("publish_date") != article["published"]:
                errors.append(f"{article['slug']}: publishing log date mismatch")
            if row.get("title") != article["title"] or row.get("summary") != article["summary"]:
                errors.append(f"{article['slug']}: publishing log title/summary mismatch")
            if row.get("tags") != ", ".join(article["tags"]):
                errors.append(f"{article['slug']}: publishing log tags mismatch")
        log_html = (ROOT / "publishing-log.html").read_text(encoding="utf-8")
        visible_log_dates = re.findall(r'<time datetime="([^"]+)">', log_html)
        expected_log_dates = [a["published"] for a in sorted(articles, key=lambda x: parse_iso(x["published"], x["slug"]), reverse=True)]
        if visible_log_dates != expected_log_dates:
            errors.append("publishing-log.html: dates differ from the article data")
        if any(not absolute_url(url) for url in re.findall(r'<a href="(https?://[^"]+)"', log_html)):
            errors.append("publishing-log.html: invalid absolute link")
    except (OSError, csv.Error) as exc:
        errors.append(f"publishing-log.csv: missing or invalid CSV: {exc}")

    for article in articles:
        expected = article["published"]
        for name, actual in (("RSS", rss_dates.get(article["url"])), ("Atom", atom_dates.get(article["url"]))):
            if actual != expected:
                errors.append(f"{article['slug']}: {name} date {actual!r} differs from {expected!r}")
    try:
        sitemap_root = ET.parse(ROOT / "sitemap.xml").getroot()
        sitemap = {n.text for n in sitemap_root.iter() if n.tag.endswith("loc")}
        for article in articles:
            if article["url"] not in sitemap:
                errors.append(f"sitemap.xml: missing {article['url']}")
    except (ET.ParseError, OSError) as exc:
        errors.append(f"sitemap.xml: invalid or missing XML: {exc}")
    for name in ("index.html", "blog.html"):
        parser = StrictArticleParser()
        try:
            parser.feed((ROOT / name).read_text(encoding="utf-8")); parser.close()
            if not {("application/rss+xml", "/rss.xml"), ("application/atom+xml", BASE_URL + "atom.xml")}.issubset(set(parser.alternates)):
                errors.append(f"{name}: missing RSS or Atom alternate link")
        except OSError as exc:
            errors.append(f"{name}: missing or invalid HTML: {exc}")
    for page in ROOT.rglob("*.html"):
        if ".git" in page.parts:
            continue
        try:
            parser = VisibleLinkParser()
            parser.feed(page.read_text(encoding="utf-8")); parser.close()
            markdown_count, bare_count = visible_unlinked_links("".join(parser.parts))
            if markdown_count or bare_count:
                errors.append(f"{page.relative_to(ROOT)}: {markdown_count} visible markdown links, {bare_count} bare visible URLs")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{page.relative_to(ROOT)}: cannot check visible links: {exc}")
    try:
        errors.extend(f"styles.css: {error}" for error in
                      validate_article_link_css((ROOT / "styles.css").read_text(encoding="utf-8")))
    except (OSError, UnicodeError) as exc:
        errors.append(f"styles.css: cannot validate article link styles: {exc}")
    return errors


def ensure_article_page(article: dict) -> None:
    path = ROOT / article["page"]
    published = article["published"]
    summary = article["summary"]
    description = clip_meta_description(summary)
    canonical = article["url"]
    cover = article["cover"]

    def meta_tag(prop: str, value: str) -> str:
        return f'<meta property="{prop}" content="{html.escape(value, quote=True)}" />'

    new_meta = [
        meta_tag("article:published_time", published),
        '<meta name="twitter:card" content="summary" />',
        f'<meta name="twitter:title" content="{html.escape(article["title"], quote=True)}" />',
        f'<meta name="twitter:description" content="{html.escape(description, quote=True)}" />',
        meta_tag("og:type", "article"), meta_tag("og:title", article["title"]),
        meta_tag("og:description", description), meta_tag("og:url", canonical),
        meta_tag("og:image", cover), meta_tag("article:author", article["author"]),
    ]
    if article.get("updated"):
        new_meta.append(meta_tag("article:modified_time", article["updated"]))
    new_meta.extend(meta_tag("article:tag", tag) for tag in article["tags"])
    ld = {"@context": "https://schema.org", "@type": "Article", "headline": article["title"], "datePublished": published, "author": {"@type": "Person", "name": article["author"], "url": BASE_URL}, "image": cover}
    if article.get("updated"):
        ld["dateModified"] = article["updated"]
    back_href = "../../blog.html#" + article.get("section", article["category"].lower())
    back_label = html.escape(article.get("category", "Writing"))
    rendered = ARTICLE_TEMPLATE.format(
        title=html.escape(article["title"]), summary=html.escape(description, quote=True),
        canonical=html.escape(canonical, quote=True), meta="\n    ".join(new_meta),
        kicker=html.escape(article.get("kicker", article["category"])),
        subtitle=html.escape(article.get("subtitle", "")),
        published=html.escape(published, quote=True),
        visible_date=datetime.fromisoformat(published).strftime("%B %-d, %Y"),
        author=html.escape(article["author"]), minutes=article.get("minutes", ""),
        body=linkify_sources(article["body"]), jsonld=json.dumps(ld, ensure_ascii=False),
    ).replace("← All articles", "← " + back_label + " articles").replace('../../blog.html">←', html.escape(back_href, quote=True) + '">←')
    path.write_text(rendered.rstrip("\n"), encoding="utf-8")


def render_all(articles: list[dict]) -> None:
    for article in articles:
        ensure_article_page(article)
    render_feeds(articles)
    render_logs(articles)
    render_sitemap(articles)
    render_archive(articles)
    add_feed_alternates()


def add_feed_alternates() -> None:
    for path in [ROOT / "index.html", ROOT / "blog.html", *sorted((ROOT / "articles").glob("*/*.html"))]:
        text = path.read_text()
        for feed in ("rss", "atom"):
            href = BASE_URL + feed + ".xml"
            media_type = "application/" + ("rss+xml" if feed == "rss" else "atom+xml")
            tag = f'<link rel="alternate" type="{media_type}" title="{feed.upper()}" href="{href}" />'
            already_present = any(
                "alternate" in dict(re.findall(r'''([\w:-]+)\s*=\s*["'](.*?)["']''', link, re.I | re.S)).get("rel", "").lower().split()
                and dict(re.findall(r'''([\w:-]+)\s*=\s*["'](.*?)["']''', link, re.I | re.S)).get("type", "").lower() == media_type
                for link in re.findall(r"<link\b[^>]*>", text, re.I | re.S)
            )
            if tag in text or already_present:
                continue
            text = text.replace("</head>", "    " + tag + "\n  </head>", 1)
        path.write_text(text)



def create_article(args: argparse.Namespace) -> int:
    if not all((args.title, args.published, args.tags, args.body)):
        print("new requires --title, --date, --tags, and --body", file=sys.stderr)
        return 2
    published = parse_iso(args.published, "new article date").isoformat(timespec="seconds")
    tags = [tag.strip().lower() for tag in args.tags.split(",") if tag.strip()]
    if not 1 <= len(tags) <= 4 or any(not re.fullmatch(r"[a-z0-9]{1,20}", tag) for tag in tags):
        print("tags must be 1–4 comma-separated lowercase alphanumeric values (max 20 chars)", file=sys.stderr)
        return 2
    slug = re.sub(r"[^a-z0-9]+", "-", args.title.lower()).strip("-")
    category = tags[0].replace("-", " ").title()
    section = re.sub(r"[^a-z0-9]+", "-", tags[0]).strip("-")
    page = f"articles/{section}/{slug}.html"
    url = BASE_URL + page
    records = load_articles()
    if any(a["slug"] == slug or a["url"] == url for a in records) or (ROOT / page).exists():
        print(f"article already exists: {slug}", file=sys.stderr)
        return 1
    body = Path(args.body).read_text(encoding="utf-8")
    p = re.search(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I)
    summary = re.sub(r"<[^>]+>", " ", p.group(1) if p else body)
    summary = re.sub(r"\s+", " ", html.unescape(summary)).strip()[:280]
    article = {"title": args.title, "slug": slug, "page": page, "url": url, "published": published,
        "summary": summary, "tags": tags, "cover": BASE_URL + "assets/shubham-anandani.jpg", "author": "Shubham Anandani",
        "category": category, "kicker": html.escape(category), "subtitle": "", "minutes": "5", "body": body,
        "section": section, "archive_label": tags[0].upper(), "archive_summary": summary}
    records.append(article)
    (ROOT / "data/articles.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    render_all(records)
    return 0


def render_archive(articles: list[dict]) -> None:
    path = ROOT / "blog.html"
    text = path.read_text()
    # Rebuild existing category lists from their single-source article records.
    sections = {}
    for article in articles:
        sections.setdefault(article.get("section", article["category"].lower()), []).append(article)
    for section, items in sections.items():
        marker = f'<div class="docker-article-list"'
        section_start = text.find(f'id="{section}"')
        if section_start < 0:
            name = items[0]["category"]
            safe_section = html.escape(section, quote=True)
            new_section = (f'<section class="docker-archive" id="{safe_section}" aria-labelledby="{safe_section}-heading">'
                f'<div class="docker-heading"><div><p class="kicker">{html.escape(name)} / field notes</p>'
                f'<h2 id="{safe_section}-heading">{html.escape(name)}<br /><em>writing.</em></h2></div>'
                f'<span class="docker-count">{len(items):02} ARTICLES</span></div>'
                f'<div class="docker-article-list"></div></section>')
            insertion = text.find('        </div>\n      </section>\n    </main>')
            if insertion >= 0:
                text = text[:insertion] + "          " + new_section + "\n" + text[insertion:]
                section_start = text.find(f'id="{section}"')
                # Expose the new category through the existing left rail.
                sidebar_insert = text.find('          <div class="blog-aside-note">')
                if sidebar_insert >= 0:
                    link = f'          <a href="#{safe_section}">{html.escape(name)} <span class="topic-count">{len(items)}</span></a>\n'
                    text = text[:sidebar_insert] + link + text[sidebar_insert:]
            else:
                continue
        list_start = text.find(marker, section_start)
        if list_start < 0:
            continue
        list_open = text.find(">", list_start) + 1
        depth = 1
        list_end = -1
        for token in re.finditer(r"</?div\b[^>]*>", text[list_open:], re.I):
            if token.group(0).startswith("</"):
                depth -= 1
                if depth == 0:
                    list_end = list_open + token.start()
                    break
            else:
                depth += 1
        if list_end < 0:
            raise ValueError(f"unclosed article list for section {section}")
        cards = []
        # Preserve hand-curated historical order; new articles sort newest first after existing entries.
        items = sorted(items, key=lambda a: (a.get("archive_order", 999), -parse_iso(a["published"], a["slug"]).timestamp()))
        for number, article in enumerate(items, 1):
            date_value = parse_iso(article["published"], article["slug"])
            card = (f'<a class="docker-article-card" href="{html.escape(article["page"], quote=True)}">'
                f'<span class="docker-index">{number:02}</span><div><div class="article-meta"><span>{html.escape(article.get("archive_label", article["category"]).upper())}</span>'
                f'<time datetime="{date_value.date().isoformat()}">{date_value.strftime("%b %d, %Y").upper()}</time></div>'
                f'<h3>{html.escape(article["title"])}</h3><p>{html.escape(article.get("archive_summary", article["summary"]))}</p>'
                f'<span class="read-time">{html.escape(article.get("read_label", str(article.get("minutes", 5)) + " MIN READ ↗"))}</span></div></a>')
            cards.append(card)
        text = text[:list_open] + "\n" + "\n".join("              " + card for card in cards) + "\n            " + text[list_end:]
        section_start = text.find(f'id="{section}"')
        section_end = text.find("</section>", section_start)
        count_match = re.search(r'<span class="docker-count">\d+ ARTICLES</span>', text[section_start:section_end])
        if count_match:
            old = count_match.group(0)
            text = text[:section_start] + text[section_start:section_end].replace(old, f'<span class="docker-count">{len(items):02} ARTICLES</span>', 1) + text[section_end:]
        rail = re.search(r'href="#' + re.escape(section) + r'"[^>]*>.*?<span class="topic-count">\d+</span>', text, re.S)
        if rail:
            text = text[:rail.start()] + re.sub(r'<span class="topic-count">\d+</span>', f'<span class="topic-count">{len(items)}</span>', rail.group(0), count=1) + text[rail.end():]
    text = re.sub(r'(<span class="blog-author-count">ARTICLES <b>)\d+(</b>)', lambda m: m.group(1) + str(len(articles)) + m.group(2), text, count=1)
    (ROOT / "blog.html").write_text(text)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "validate", "new"])
    parser.add_argument("--title")
    parser.add_argument("--date", dest="published")
    parser.add_argument("--tags")
    parser.add_argument("--body")
    args = parser.parse_args()
    if args.command == "new":
        return create_article(args)
    articles = load_articles()
    for article in articles:
        article["published"] = normalize_existing_date(article["published"])
    if args.command == "build":
        errors = validate_data(articles)
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        (ROOT / "data/articles.json").write_text(json.dumps(articles, ensure_ascii=False, indent=2) + "\n")
        render_all(articles)
    errors = validate(articles)
    if errors:
        print("Validation failed:\n- " + "\n- ".join(errors), file=sys.stderr)
        return 1
    print(f"Validated {len(articles)} articles, dates, pages, and feeds.")
    if args.command == "build":
        subprocess.run([sys.executable, str(ROOT / "scripts/seo.py")], check=True)
    return 0


def validate_data(articles: list[dict]) -> list[str]:
    errors=[]
    seen=set()
    for a in articles:
        missing={"title","slug","page","published","summary","tags","cover","author","url"}-a.keys()
        if missing: errors.append(f"{a.get('slug','?')}: missing fields {sorted(missing)}"); continue
        if a["slug"] in seen: errors.append(f"duplicate slug {a['slug']}")
        seen.add(a["slug"])
        try: parse_iso(a["published"],a["slug"])
        except ValueError as e: errors.append(str(e))
        if "updated" in a:
            try: parse_iso(a["updated"],a["slug"]+" updated")
            except ValueError as e: errors.append(str(e))
        if len(a["tags"])>4:errors.append(f"{a['slug']}: more than four tags")
        for tag in a["tags"]:
            if not re.fullmatch(r"[a-z0-9]{1,20}",tag):errors.append(f"{a['slug']}: invalid category {tag!r}")
        if a["url"] != BASE_URL+a["page"]:errors.append(f"{a['slug']}: URL/page mismatch")
    return errors


if __name__ == "__main__":
    raise SystemExit(main())
