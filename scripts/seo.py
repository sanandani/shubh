#!/usr/bin/env python3
"""Idempotently add head-only SEO metadata and generate sitemap.xml."""

from __future__ import annotations

import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://www.imshubh.com"


def parse_attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): html.unescape(m.group(3)) for m in re.finditer(r'''([\w:-]+)\s*=\s*(["'])(.*?)\2''', tag, re.S)}


def set_attr(tag: str, name: str, value: str) -> str:
    escaped = html.escape(value, quote=True)
    pattern = re.compile(rf'(\b{re.escape(name)}\s*=\s*)(["\'])(.*?)\2', re.I | re.S)
    if pattern.search(tag):
        return pattern.sub(lambda m: m.group(1) + m.group(2) + escaped + m.group(2), tag, count=1)
    close = tag.rfind("/>")
    if close < 0:
        close = tag.rfind(">")
    return tag[:close].rstrip() + f' {name}="{escaped}"' + tag[close:]


def replace_head(document: str, transform) -> str:
    match = re.search(r"(?P<opening><head\b[^>]*>)(?P<content>.*?)(?P<closing></head\s*>)", document, re.I | re.S)
    if not match:
        raise ValueError("document has no head element")
    content = match.group("content")
    suffix_match = re.search(r"\s*$", content, re.S)
    suffix_start = suffix_match.start() if suffix_match else len(content)
    head = content[:suffix_start]
    suffix = content[suffix_start:]
    updated = transform(head) + suffix
    return document[:match.start()] + match.group("opening") + updated + match.group("closing") + document[match.end():]


def find_tag(head: str, tag_name: str, predicate) -> re.Match | None:
    for match in re.finditer(rf"<{tag_name}\b[^>]*>", head, re.I | re.S):
        if predicate(parse_attrs(match.group(0))):
            return match
    return None


def set_meta(head: str, key_name: str, key: str, value: str) -> str:
    found = find_tag(head, "meta", lambda a: a.get(key_name, "").lower() == key.lower())
    if found:
        tag = set_attr(found.group(0), "content", value)
        return head[:found.start()] + tag + head[found.end():]
    return head + f'\n    <meta {key_name}="{html.escape(key, quote=True)}" content="{html.escape(value, quote=True)}" />'


def set_link(head: str, rel: str, href: str, media_type: str | None = None) -> str:
    def matches(attrs: dict[str, str]) -> bool:
        rels = attrs.get("rel", "").lower().split()
        return "canonical" in rels if rel == "canonical" else "alternate" in rels and attrs.get("type", "").lower() == (media_type or "").lower()
    found = find_tag(head, "link", matches)
    if found:
        tag = set_attr(found.group(0), "href", href)
        return head[:found.start()] + tag + head[found.end():]
    extra = f' type="{html.escape(media_type, quote=True)}"' if media_type else ""
    return head + f'\n    <link rel="{rel}"{extra} href="{html.escape(href, quote=True)}" />'


class PageDetails(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_head = False
        self.in_title = False
        self.in_h1 = False
        self.in_time = False
        self.in_p = False
        self.head_title: list[str] = []
        self.head_description = ""
        self.h1: list[str] = []
        self.publish_datetime = ""
        self.first_paragraph: list[str] = []
        self.body_seen = False

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag == "head":
            self.in_head = True
        elif tag == "body":
            self.in_head = False
            self.body_seen = True
        elif self.in_head and tag == "title":
            self.in_title = True
        elif self.in_head and tag == "meta" and values.get("name", "").lower() == "description":
            self.head_description = values.get("content", "")
        elif self.body_seen and tag == "h1" and not self.h1:
            self.in_h1 = True
        elif self.body_seen and tag == "time" and not self.publish_datetime:
            self.publish_datetime = values.get("datetime", "")
            self.in_time = True
        elif self.body_seen and tag == "p" and not self.first_paragraph and values.get("class", "") not in {"kicker", "article-document-meta"}:
            self.in_p = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.in_head = False
        elif tag == "title":
            self.in_title = False
        elif tag == "h1":
            self.in_h1 = False
        elif tag == "time":
            self.in_time = False
        elif tag == "p":
            if self.in_p:
                self.in_p = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.head_title.append(data)
        if self.in_h1:
            self.h1.append(data)
        if self.in_p:
            self.first_paragraph.append(data)


def clip_description(value: str) -> str:
    value = re.sub(r"\s+", " ", html.unescape(value)).strip()
    if len(value) <= 155:
        return value
    shortened = value[:154]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,;:-") + "…"


def extract_details(document: str) -> PageDetails:
    parser = PageDetails()
    parser.feed(document)
    return parser


def add_article_metadata(path: Path) -> str:
    relative = path.relative_to(ROOT).as_posix()
    canonical = f"{BASE}/{relative}"
    document = path.read_text(encoding="utf-8")
    details = extract_details(document)
    title = " ".join(details.h1).strip() or " ".join(details.head_title).strip()
    if not title:
        raise ValueError(f"{relative}: no headline/title")
    publish_date = details.publish_datetime
    if not publish_date:
        raise ValueError(f"{relative}: no visible publication date")
    description = clip_description(details.head_description or " ".join(details.first_paragraph))
    if not description:
        raise ValueError(f"{relative}: no description or first paragraph")
    changed = False

    def update(head: str) -> str:
        nonlocal changed
        before = head
        head = set_link(head, "canonical", canonical)
        head = set_meta(head, "name", "description", description)
        for key, value in (("og:title", title), ("og:description", description), ("og:type", "article"), ("og:url", canonical)):
            head = set_meta(head, "property", key, value)
        ld_start = find_tag(head, "script", lambda a: a.get("type", "").lower() == "application/ld+json")
        if ld_start:
            end = re.search(r"</script\s*>", head[ld_start.end():], re.I)
            if not end:
                raise ValueError(f"{relative}: unterminated JSON-LD script")
            ld_end = ld_start.end() + end.end()
            raw = head[ld_start.end():ld_start.end() + end.start()]
            try:
                structured = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{relative}: invalid existing JSON-LD: {exc}") from exc
            structured["@context"] = structured.get("@context", "https://schema.org")
            structured["@type"] = "BlogPosting"
            structured["headline"] = title
            structured["description"] = description
            structured["datePublished"] = publish_date
            existing_author = structured.get("author", {})
            if isinstance(existing_author, dict):
                structured["author"] = {**existing_author, "@type": "Person", "name": "Shubham Anandani"}
            else:
                structured["author"] = {"@type": "Person", "name": "Shubham Anandani"}
            structured["mainEntityOfPage"] = {"@type": "WebPage", "@id": canonical}
            script = '<script type="application/ld+json">' + json.dumps(structured, ensure_ascii=False, separators=(",", ":")) + "</script>"
            head = head[:ld_start.start()] + script + head[ld_end:]
        else:
            structured = {
                "@context": "https://schema.org", "@type": "BlogPosting", "headline": title,
                "description": description, "datePublished": publish_date,
                "author": {"@type": "Person", "name": "Shubham Anandani"},
                "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
            }
            head += '\n    <script type="application/ld+json">' + json.dumps(structured, ensure_ascii=False, separators=(",", ":")) + "</script>"
        changed = changed or head != before
        return head

    result = replace_head(document, update)
    if changed:
        path.write_text(result, encoding="utf-8")
        return "updated"
    return "unchanged"


def add_site_metadata(path: Path, canonical: str) -> str:
    document = path.read_text(encoding="utf-8")
    changed = False

    def update(head: str) -> str:
        nonlocal changed
        before = head
        head = set_link(head, "canonical", canonical)
        head = set_link(head, "alternate", "/rss.xml", "application/rss+xml")
        changed = head != before
        return head

    result = replace_head(document, update)
    if changed:
        path.write_text(result, encoding="utf-8")
        return "updated"
    return "unchanged"


def generate_sitemap(article_dates: dict[str, str]) -> None:
    root = ET.Element("urlset", {"xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9"})
    urls = [(BASE + "/", None), (BASE + "/blog.html", None)]
    urls.extend((BASE + "/" + path, date) for path, date in sorted(article_dates.items()))
    for url, date in urls:
        node = ET.SubElement(root, "url")
        ET.SubElement(node, "loc").text = url
        if date:
            ET.SubElement(node, "lastmod").text = date
    xml = ET.tostring(root, encoding="unicode", xml_declaration=False)
    (ROOT / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n", encoding="utf-8")


def update_robots() -> None:
    (ROOT / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: https://www.imshubh.com/sitemap.xml\n", encoding="utf-8"
    )


def report_noncanonical_references() -> list[str]:
    pattern = re.compile(r"https?://(?:www\.)?imshubh\.com[^\s\"'<>)]*", re.I)
    offenders: list[str] = []
    extensions = {".html", ".xml", ".txt", ".css", ".js", ".md", ".json", ".yml"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in extensions or path == Path(__file__):
            continue
        if ".git" in path.parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, 1):
            for match in pattern.finditer(line):
                url = match.group(0)
                if not url.lower().startswith("https://www.imshubh.com"):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {url}")
    return offenders


def main() -> int:
    articles = sorted((ROOT / "articles").glob("*/*.html"))
    article_dates: dict[str, str] = {}
    for path in articles:
        document = path.read_text(encoding="utf-8")
        details = extract_details(document)
        if not details.publish_datetime:
            raise ValueError(f"{path.relative_to(ROOT)}: no visible publication date")
        article_dates[path.relative_to(ROOT).as_posix()] = details.publish_datetime[:10]
        add_article_metadata(path)
    add_site_metadata(ROOT / "index.html", BASE + "/")
    add_site_metadata(ROOT / "blog.html", BASE + "/blog.html")
    generate_sitemap(article_dates)
    update_robots()
    print(f"Processed {len(articles)} article pages; sitemap has {len(articles) + 2} URLs.")
    offenders = report_noncanonical_references()
    if offenders:
        print("Non-www or HTTP imshubh.com references:")
        print("\n".join(offenders))
    else:
        print("No HTTP or non-www imshubh.com references found.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"SEO generation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
