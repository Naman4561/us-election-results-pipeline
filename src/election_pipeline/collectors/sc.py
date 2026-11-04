"""Resolve only dated workbooks explicitly linked by SC's official fallback page."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)


def latest_report(index, source):
    if source.election_date != "2026-11-03" or source.state != "SC":
        raise ValueError("Unsupported South Carolina election")
    links = Links()
    links.feed(index)
    matches = {}
    for link in links.links:
        url = urljoin(source.url, link)
        parts = urlsplit(url)
        match = re.fullmatch(
            r"/wp-content/uploads/2026/11/2026-11-03-(\d{1,2})(\d{2})-(AM|PM)"
            r"-General-Election-Results-?\.xlsx",
            parts.path,
        )
        if not match:
            continue
        if (
            parts.scheme != "https"
            or parts.netloc != "scvotes.gov"
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Unexpected host or parameters in SC workbook link")
        hour, minute = int(match[1]), int(match[2])
        if not 1 <= hour <= 12 or not 0 <= minute < 60:
            raise ValueError("Invalid SC report time")
        time = (hour % 12 + (12 if match[3] == "PM" else 0)) * 60 + minute
        if time in matches and matches[time] != url:
            raise ValueError("Ambiguous SC report links at the same time")
        matches[time] = url
    if not matches:
        raise ValueError("No supported dated SC workbook link; retain the last publication")
    return matches[max(matches)]


def resolve(source, paths):
    return latest_report(paths["index.html"].read_text(encoding="utf-8-sig"), source)
