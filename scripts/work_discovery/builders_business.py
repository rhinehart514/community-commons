#!/usr/bin/env python3
"""Collect public, credited student and business-building work in WNY."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


UA = "Mozilla/5.0 (compatible; regional-work-discovery/1.0)"
RIT_INDEX = "https://www.rit.edu/engineering/seniordesign/projects"
UR_INDEX = "https://www.hajim.rochester.edu/senior-design-day/"
UB_CASE_INDEX = "https://www.buffalo.edu/partnerships/collaborate/case-studies.html"
PANASCI_2025 = "https://www.buffalo.edu/home/story-repository.host.html/content/shared/university/news/ub-reporter-articles/stories/2025/04/panasci-winners.detail.html"
AIA_2024 = "https://www.buffalo.edu/campaign/impact/bold-stories.host.html/content/shared/ap/articles/news/2024/aiabuffalowny-designawards-2024.detail.html"
CMI_2026 = "https://www.buffalo.edu/shared-facilities-equip/news.host.html/content/shared/university/news/news-center-releases/2026/01/ub-cmi-fiar-projects.detail.html"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def make_id(url: str, key: str) -> str:
    return hashlib.sha256((url + "|" + key).encode("utf-8")).hexdigest()[:24]


class TextParser(HTMLParser):
    BREAKS = {"br", "p", "div", "h1", "h2", "h3", "h4", "li", "tr", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(markup: str) -> str:
    parser = TextParser()
    parser.feed(markup)
    return html.unescape("\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip()))


def lines(markup: str) -> list[str]:
    parser = TextParser()
    parser.feed(markup)
    return [clean(line) for line in "".join(parser.parts).splitlines() if clean(line)]


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.current_href = ""
        self.current_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current_href = dict(attrs).get("href", "")
            self.current_text = []

    def handle_data(self, data):
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current_href:
            self.links.append((self.current_href, clean(" ".join(self.current_text))))
            self.current_href = ""
            self.current_text = []


class SectionListParser(HTMLParser):
    """Read list items under one named heading, stopping at the next heading."""

    def __init__(self, target_heading: str):
        super().__init__(convert_charrefs=True)
        self.target_heading = target_heading.casefold()
        self.heading_tag = ""
        self.heading_text: list[str] = []
        self.current_heading = ""
        self.item_open = False
        self.paragraph_open = False
        self.item_text: list[str] = []
        self.items: list[str] = []

    def handle_starttag(self, tag, attrs):
        if re.fullmatch(r"h[1-6]", tag):
            self.heading_tag = tag
            self.heading_text = []
            self.current_heading = ""
            self.item_open = False
        elif tag == "li" and self.current_heading == self.target_heading:
            self.item_open = True
            self.item_text = []
        elif tag == "p" and self.current_heading == self.target_heading and not self.item_open:
            self.paragraph_open = True
            self.item_text = []
        elif tag == "br" and self.paragraph_open:
            self.item_text.append("\n")

    def handle_data(self, data):
        if self.heading_tag:
            self.heading_text.append(data)
        elif self.item_open or self.paragraph_open:
            self.item_text.append(data)

    def handle_endtag(self, tag):
        if self.heading_tag == tag:
            self.current_heading = clean(" ".join(self.heading_text)).casefold()
            self.heading_tag = ""
        elif tag == "li" and self.item_open:
            value = clean(" ".join(self.item_text))
            if value:
                self.items.append(value)
            self.item_open = False
        elif tag == "p" and self.paragraph_open:
            self.items.extend(clean(value) for value in "".join(self.item_text).splitlines() if clean(value))
            self.paragraph_open = False

class PortfolioParser(HTMLParser):
    """Extract the live portfolio cards without relying on site JavaScript."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self.card: dict[str, str] | None = None
        self.in_heading = False
        self.in_anchor = False
        self.in_location = False
        self.in_desc = False
        self.in_badge = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "")
        if tag == "article" and "portfolio-list-item" in classes:
            self.card = {"name": "", "url": "", "location": "", "description": "", "cohort": ""}
        if self.card is None:
            return
        if tag == "h2" and "portfolio-list-item__heading" in classes:
            self.in_heading = True
        elif tag == "a" and self.in_heading:
            self.in_anchor = True
            self.card["url"] = attrs.get("href", "")
        elif tag == "small":
            self.in_location = True
        elif tag == "p" and not self.card["description"]:
            self.in_desc = True
        elif tag == "a" and "badge" in classes and not self.card["cohort"]:
            self.in_badge = True

    def handle_endtag(self, tag):
        if tag == "article" and self.card is not None:
            self.items.append(self.card)
            self.card = None
        elif tag == "h2":
            self.in_heading = False
            self.in_anchor = False
        elif tag == "a":
            self.in_anchor = False
            self.in_badge = False
        elif tag == "small":
            self.in_location = False
        elif tag == "p":
            self.in_desc = False

    def handle_data(self, data):
        if self.card is None:
            return
        value = clean(data)
        if self.in_anchor:
            self.card["name"] += value
        if self.in_location:
            self.card["location"] += value
        if self.in_desc:
            self.card["description"] += value
        if self.in_badge:
            self.card["cohort"] += value


class Collector:
    def __init__(self, out: Path, max_requests: int, max_minutes: float, max_rit: int, max_ur: int, reuse_raw: bool = False):
        self.out = out
        self.raw_dir = out / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.max_requests = max_requests
        self.deadline = time.monotonic() + max_minutes * 60
        self.max_rit = max_rit
        self.max_ur = max_ur
        self.reuse_raw = reuse_raw
        self.started = utc_now()
        self.request_count = 0
        self.reused_raw = 0
        self.last_request = 0.0
        self.works: list[dict] = []
        self.contributions: list[dict] = []
        self.sources: list[dict] = []
        self.errors: list[dict] = []
        self.limitations: list[str] = []
        self.raw_by_url: dict[str, tuple[str, str, int]] = {}

    def can_request(self) -> bool:
        return self.request_count < self.max_requests and time.monotonic() < self.deadline

    def fetch(self, url: str, key: str) -> tuple[str, bytes] | None:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", key).strip("-")[:92] or "response"
        raw_name = f"{safe}-{digest(url.encode())[:10]}.html"
        path = self.raw_dir / raw_name
        if self.reuse_raw and path.is_file():
            body = path.read_bytes()
            rel = f"raw/{raw_name}"
            self.raw_by_url[url] = (rel, digest(body), 200)
            self.reused_raw += 1
            return url, body
        if not self.can_request():
            return None
        self.request_count += 1
        wait = 0.12 - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml"})
        status = 0
        try:
            with urllib.request.urlopen(req, timeout=22) as response:
                body = response.read()
                status = response.status
                actual = response.geturl()
            path.write_bytes(body)
            rel = f"raw/{raw_name}"
            sha = digest(body)
            self.raw_by_url[url] = (rel, sha, status)
            return actual, body
        except urllib.error.HTTPError as exc:
            status = exc.code
            message = f"HTTP {exc.code} for {url}"
        except Exception as exc:  # network errors are recorded and collection continues
            message = f"{type(exc).__name__} for {url}: {str(exc)[:200]}"
        self.errors.append({"url": url, "status": status, "error": message})
        self.raw_by_url[url] = ("", "", status)
        return None

    def source(self, family: str, url: str, status: str, records: int, coverage: str, limitation: str = ""):
        self.sources.append({"family": family, "url": url, "status": status, "records": records, "coverage": coverage, "limitation": limitation})

    def add_work(self, *, url: str, title: str, kind: str, organization: str, geography: str, evidence: str, description: str, work_date: str = "", key: str | None = None, raw_url: str | None = None):
        raw_path, raw_sha, _ = self.raw_by_url.get(raw_url or url, ("", "", 0))
        work_id = make_id(url, key or title)
        self.works.append({
            "id": work_id,
            "title": clean(title),
            "kind": kind,
            "source_url": url,
            "observed_at": utc_now(),
            "work_date": work_date,
            "organization": organization,
            "regional_evidence": evidence,
            "geography_scope": geography,
            "description": clean(description),
            "raw_path": raw_path,
            "raw_sha256": raw_sha,
        })
        return work_id

    def add_credit(self, work_id: str, name: str, role: str, source_url: str, evidence: str, person_url: str = ""):
        name = clean(name)
        if not name or not role:
            return
        self.contributions.append({
            "work_id": work_id,
            "name": name,
            "role": clean(role),
            "person_url": person_url,
            "identifiers": {},
            "evidence": clean(evidence),
            "source_url": source_url,
        })

    def collect_43north(self):
        family = "accelerator cohorts and demo days"
        cards: dict[str, dict[str, str]] = {}
        pages_ok = 0
        for page in range(1, 8):
            url = "https://43north.org/portfolio-companies/" if page == 1 else f"https://43north.org/portfolio-companies/page/{page}/"
            fetched = self.fetch(url, f"43north-portfolio-page-{page}")
            if not fetched:
                continue
            pages_ok += 1
            parser = PortfolioParser()
            parser.feed(fetched[1].decode("utf-8", "replace"))
            for item in parser.items:
                if item["url"]:
                    cards[item["url"]] = item
        local = [c for c in cards.values() if any(term in c["location"].lower() for term in ("buffalo", "rochester", "finger lakes", "niagara"))]
        detail_count = 0
        for card in local:
            if not self.can_request():
                break
            fetched = self.fetch(card["url"], "43north-company-" + card["name"])
            if not fetched:
                continue
            detail_count += 1
            markup = fetched[1].decode("utf-8", "replace")
            # The program's company archive provides the product/service statement;
            # company pages provide a dedicated Founders credit section.
            founders: list[tuple[str, str]] = []
            for block in re.findall(r'<div class="founder-card\b.*?</div>\s*</div>\s*</div>', markup, re.I | re.S):
                name_hit = re.search(r'<h4[^>]*>(.*?)</h4>', block, re.I | re.S)
                if not name_hit:
                    continue
                title_hit = re.search(r'<p class="founder__title[^>]*>(.*?)</p>', block, re.I | re.S)
                name = clean(plain_text(name_hit.group(1)))
                title = clean(plain_text(title_hit.group(1))) if title_hit else ""
                if name:
                    founders.append((name, title))
            # More tolerant section parse for cards with extra nested image markup.
            if not founders:
                section = re.search(r'<h2[^>]*>\s*Founders\s*</h2>(.*?)(?:<section class="portfolio-nav"|</main>)', markup, re.I | re.S)
                if section:
                    for card_block in re.findall(r'<div class="founder-card\b.*?(?=<div class="founder-card\b|</section>)', section.group(1), re.I | re.S):
                        hit = re.search(r'<h4[^>]*>(.*?)</h4>', card_block, re.I | re.S)
                        if hit:
                            tit = re.search(r'<p class="founder__title[^>]*>(.*?)</p>', card_block, re.I | re.S)
                            founders.append((clean(plain_text(hit.group(1))), clean(plain_text(tit.group(1))) if tit else ""))
            if not founders:
                self.limitations.append(f"43North company page has no parseable named founder card: {card['url']}; the regional product work is retained without person contributions.")
            loc = card["location"]
            geo = "Rochester/Finger Lakes" if any(t in loc.lower() for t in ("rochester", "finger lakes")) else "Buffalo/WNY"
            work_date = ""
            desc = card["description"]
            product_part = desc.rstrip(".")
            title = f"{card['name']} — {product_part}" if product_part else card["name"]
            work_id = self.add_work(
                url=card["url"], title=title, kind="accelerator company product/service",
                organization="43North", geography=geo,
                evidence=f"The current 43North portfolio card lists {loc} for this cohort company; the company detail page names its Founders.",
                description=desc,
                key=card["url"], raw_url=card["url"],
            )
            for name, title_text in founders:
                role = "Founder" + (f"; {title_text}" if title_text else "")
                self.add_credit(work_id, name, role, card["url"], f"43North company page lists {name} in the Founders section" + (f" with title {title_text}" if title_text else "") + ".")
        status = "ok" if pages_ok == 7 and detail_count == len(local) else "partial"
        self.source(family, "https://43north.org/portfolio-companies/", status, sum(1 for w in self.works if w["organization"] == "43North"), f"Fetched {pages_ok}/7 archive pages (79 company cards); detail checked {detail_count}/{len(local)} entries currently listed in Buffalo/Rochester/WNY; {len(cards)} unique company records.", "Founder credit pages may omit names/roles; current location is a portfolio snapshot and does not establish each person's residence.")

    @staticmethod
    def is_likely_person_name(value: str) -> bool:
        value = clean(value)
        if len(value.split()) < 2 or re.search(r"\d", value):
            return False
        if re.search(r"\b(?:cad|model|render(?:ed|ing)?|figure|diagram|schematic|photo|image|team|students?|department|faculty|school|university|college|left to right)\b", value, re.I):
            return False
        return True

    @staticmethod
    def get_rit_team(markup: str) -> list[str]:
        found = re.search(r"Team Members\s*<br\s*/?>\s*<span[^>]*>(.*?)</span>", markup, re.I | re.S)
        if found:
            # Some RIT profile source contains an unspaced comma-separated list,
            # while others use <br>. Split both forms before whitespace cleanup.
            raw_names = re.split(r"<br\s*/?>", found.group(1), flags=re.I)
            names = [clean(plain_text(x)) for raw in raw_names for x in raw.split(",") if clean(plain_text(x))]
            return [name for name in names if Collector.is_likely_person_name(name)]
        return []

    @staticmethod
    def get_rit_description(markup: str) -> str:
        body = re.search(r'<div class="col-lg-8 pr-lg-5 lightgallery exhibit-body"[^>]*>(.*?)</div>\s*<div class="col-lg-3"', markup, re.I | re.S)
        if not body:
            body = re.search(r'<div class="col-lg-8 pr-lg-5 lightgallery exhibit-body"[^>]*>(.*?)</article>', markup, re.I | re.S)
        if body:
            paras = re.findall(r"<p\b[^>]*>(.*?)</p>", body.group(1), re.I | re.S)
            for p in paras:
                text = clean(plain_text(p))
                if text and not re.search(r"watch|video|play video|share", text, re.I):
                    return text
        return ""

    def collect_rit(self):
        family = "university capstones, design expos, and competitions"
        fetched = self.fetch(RIT_INDEX, "rit-senior-design-index")
        if not fetched:
            self.source(family, RIT_INDEX, "blocked", 0, "The index could not be fetched.", "Index fetch failed.")
            return
        parser = LinkParser()
        parser.feed(fetched[1].decode("utf-8", "replace"))
        urls = []
        for href, _ in parser.links:
            if "/engineering/seniordesign/projects/" in href:
                full = urllib.parse.urljoin(RIT_INDEX, href).split("#", 1)[0]
                if full.rstrip("/") != RIT_INDEX.rstrip("/") and full not in urls:
                    urls.append(full.rstrip("/") )
        total = len(urls)
        if total > self.max_rit:
            # Spread requests through the complete linked index instead of truncating at A-Z.
            indexes = sorted({round(i * (total - 1) / max(1, self.max_rit - 1)) for i in range(self.max_rit)})
            urls = [urls[i] for i in indexes]
        got = 0
        first = len(self.works)
        for url in urls:
            if not self.can_request():
                break
            item = self.fetch(url, "rit-project-" + url.rsplit("/", 1)[-1])
            if not item:
                continue
            got += 1
            markup = item[1].decode("utf-8", "replace")
            title_hit = re.search(r"<h1[^>]*>(.*?)</h1>", markup, re.I | re.S)
            title = clean(plain_text(title_hit.group(1))) if title_hit else url.rsplit("/", 1)[-1].replace("-", " ").title()
            desc = self.get_rit_description(markup)
            members = self.get_rit_team(markup)
            work_id = self.add_work(
                url=url, title=title, kind="multidisciplinary senior design project",
                organization="Rochester Institute of Technology", geography="Rochester/Finger Lakes",
                evidence="RIT College of Engineering project page identifies this as a senior design project; the school and project page are in Rochester, New York.",
                description=desc or "RIT College of Engineering senior design project; the page lists a project team.", key=url, raw_url=url,
            )
            if members:
                for name in members:
                    self.add_credit(work_id, name, "student project team member", url, f"RIT project page lists {name} under Team Members")
            else:
                self.limitations.append(f"No parseable Team Members list on RIT page: {url}")
        self.source(family, RIT_INDEX, "ok" if got == len(urls) and len(urls) == total else "partial", len(self.works) - first, f"Fetched {got}/{len(urls)} project pages selected from {total} links in RIT's live index; selection is evenly spaced through the linked list, not a year-complete census.", "The index does not provide a consistent project year; institutional login handles were excluded from person credits. RIT ties are labeled Rochester/Finger Lakes.")

    @staticmethod
    def is_ur_project_url(url: str, category_urls: set[str]) -> bool:
        parsed = urllib.parse.urlparse(url)
        root = "/senior-design-day/"
        if parsed.netloc != "www.hajim.rochester.edu" or not parsed.path.startswith(root):
            return False
        normalized = urllib.parse.urlunparse(parsed._replace(query="", fragment="")).rstrip("/")
        if normalized == UR_INDEX.rstrip("/") or normalized in category_urls:
            return False
        slug = parsed.path[len(root):].strip("/")
        return bool(slug) and "/" not in slug

    @staticmethod
    def get_ur_team(markup: str) -> list[str]:
        parser = SectionListParser("Team Members")
        parser.feed(markup)
        return [name for name in parser.items if Collector.is_likely_person_name(name)]

    @staticmethod
    def extract_ur_project_team(url: str, markup: str, category_urls: set[str]) -> list[str]:
        if not Collector.is_ur_project_url(url, category_urls):
            return []
        return Collector.get_ur_team(markup)

    def collect_ur(self):
        family = "university capstones, design expos, and competitions"
        fetched = self.fetch(UR_INDEX, "university-rochester-senior-design-index")
        if not fetched:
            self.source(family, UR_INDEX, "blocked", 0, "The index could not be fetched.", "Index fetch failed.")
            return
        parser = LinkParser()
        parser.feed(fetched[1].decode("utf-8", "replace"))
        categories: list[str] = []
        for href, _ in parser.links:
            full = urllib.parse.urljoin(UR_INDEX, href)
            normalized = full.rstrip("/")
            if normalized.startswith(UR_INDEX.rstrip("/") + "/") and normalized.count("/") == UR_INDEX.rstrip("/").count("/") + 1 and normalized not in categories:
                categories.append(normalized)
        category_urls = set(categories)
        project_urls: set[str] = set()
        category_fetched = 0
        category_limit = 12
        # Crawl a bounded set of discipline/topic archive pages. These pages are
        # indexes, not works; only their individual project links are retained.
        for category_url in categories[:category_limit]:
            if not self.can_request() and not (self.reuse_raw and self.cached_ur_path(category_url)):
                break
            slug = category_url.rsplit("/", 1)[-1]
            category_item = self.fetch(category_url, "ur-senior-design-" + slug)
            if not category_item:
                continue
            category_fetched += 1
            category_parser = LinkParser()
            category_parser.feed(category_item[1].decode("utf-8", "replace"))
            for href, _ in category_parser.links:
                candidate = urllib.parse.urljoin(category_url, href).split("#", 1)[0].rstrip("/")
                if self.is_ur_project_url(candidate, category_urls):
                    project_urls.add(candidate)
        # Include project links found on any previously preserved department page
        # when the run is reparsing, so repeat runs can improve extraction cheaply.
        if self.reuse_raw:
            for path in self.raw_dir.glob("ur-senior-design-*.html"):
                if path.name.startswith("ur-senior-design---"):
                    continue
                category_parser = LinkParser()
                category_parser.feed(path.read_text(encoding="utf-8", errors="replace"))
                for href, _ in category_parser.links:
                    candidate = urllib.parse.urljoin(UR_INDEX, href).split("#", 1)[0].rstrip("/")
                    if self.is_ur_project_url(candidate, category_urls):
                        project_urls.add(candidate)
        urls = sorted(project_urls)
        total = len(urls)
        if total > self.max_ur:
            indexes = sorted({round(i * (total - 1) / max(1, self.max_ur - 1)) for i in range(self.max_ur)})
            urls = [urls[i] for i in indexes]
        count = 0
        first = len(self.works)
        for url in urls:
            if not self.can_request():
                break
            item = self.fetch(url, "ur-senior-design-" + url.rsplit("/", 1)[-1])
            if not item:
                continue
            count += 1
            markup = item[1].decode("utf-8", "replace")
            title_hit = re.search(r"<h1[^>]*>(.*?)</h1>", markup, re.I | re.S)
            title = clean(plain_text(title_hit.group(1))) if title_hit else url.rsplit("/", 1)[-1].replace("-", " ").title()
            txtlines = lines(markup)
            members = self.extract_ur_project_team(url, markup, category_urls)
            desc = ""
            for marker in ("Abstract", "Project Description"):
                if marker in txtlines:
                    idx = txtlines.index(marker)
                    desc = " ".join(txtlines[idx + 1 : idx + 4])
                    if desc:
                        break
            if not desc and not members:
                self.limitations.append(f"Skipped a linked Rochester page without a project description or Team Members section: {url}")
                continue
            work_id = self.add_work(url=url, title=title, kind="senior design project", organization="University of Rochester", geography="Rochester/Finger Lakes", evidence="The University of Rochester Hajim School senior design project page identifies the capstone project and its Rochester campus context.", description=desc or "University of Rochester senior design project page lists a project team.", key=url, raw_url=url)
            for name in members:
                self.add_credit(work_id, name, "student project team member", url, f"University of Rochester page lists {name} under Team Members")
        self.source(family, UR_INDEX, "partial" if count < len(urls) or len(categories) > category_fetched else "ok", len(self.works) - first, f"Fetched {category_fetched}/{min(len(categories), category_limit)} discipline/topic index pages and {count}/{len(urls)} individual project pages selected from {total} unique project links.", "Department/topic pages were treated as indexes and excluded from works; only list items under Team Members are credited. Supervisors, customers, and project-artifact headings are excluded from person records. The project archive has no year pagination and this run is bounded by --max-ur.")

    def cached_ur_path(self, url: str) -> bool:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        key = "ur-senior-design-" + slug
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", key).strip("-")[:92] or "response"
        return (self.raw_dir / f"{safe}-{digest(url.encode())[:10]}.html").is_file()

    def collect_case_studies(self):
        family = "company case studies and technical project credits"
        fetched = self.fetch(UB_CASE_INDEX, "ub-case-studies-index")
        if not fetched:
            self.source(family, UB_CASE_INDEX, "blocked", 0, "The official case-study index could not be fetched.", "Index fetch failed.")
            return
        parser = LinkParser()
        parser.feed(fetched[1].decode("utf-8", "replace"))
        urls = []
        for href, _ in parser.links:
            full = urllib.parse.urljoin(UB_CASE_INDEX, href)
            if "/partnerships/collaborate/case-studies/" in full and full.rstrip("/") != UB_CASE_INDEX.removesuffix(".html").rstrip("/") and full not in urls:
                if full.lower().endswith((".html", "/")):
                    urls.append(full)
        count = 0
        first = len(self.works)
        for url in urls[:10]:
            if not self.can_request():
                break
            item = self.fetch(url, "ub-case-study-" + url.rsplit("/", 1)[-1])
            if not item:
                continue
            count += 1
            markup = item[1].decode("utf-8", "replace")
            h1_hits = re.findall(r"<h1[^>]*>(.*?)</h1>", markup, re.I | re.S)
            headings = [clean(plain_text(hit)) for hit in h1_hits]
            headings = [heading for heading in headings if heading and not re.search(r"privacy|cookies", heading, re.I)]
            title = headings[-1].title() if headings else url.rsplit("/", 1)[-1].split(".")[0].replace("-", " ").title()
            txt = lines(markup)
            desc = ""
            h1_positions = list(re.finditer(r"<h1[^>]*>(.*?)</h1>", markup, re.I | re.S))
            if h1_positions:
                tail = markup[h1_positions[-1].end():]
                for paragraph in re.findall(r"<p\b[^>]*>(.*?)</p>", tail, re.I | re.S):
                    candidate = clean(plain_text(paragraph))
                    if len(candidate) > 60 and not re.search(r"cookie|privacy policy|business & entrepreneur partnerships", candidate, re.I):
                        desc = candidate
                        break
            if not desc:
                desc = "University at Buffalo Business and Entrepreneur Partnerships case study describing the company's product and collaboration with UB."
            evidence = "UB's case study identifies this as a company product or development project and documents the company's collaboration with the University at Buffalo, whose campus and Business and Entrepreneur Partnerships are in Buffalo."
            work_id = self.add_work(url=url, title=title, kind="company product/development case study", organization=title, geography="Buffalo/WNY", evidence=evidence, description=desc, key=url, raw_url=url)
            # Preserve only explicit credits where a named person is adjacent to a founder/technical role.
            all_lines = txt
            for idx, line in enumerate(all_lines):
                if re.search(r"\b(founder|co-founder|CEO|CTO|president|chief technology officer|chief executive officer)\b", line, re.I):
                    roleline = line
                    if idx and re.fullmatch(r"[A-Z][A-Za-z’'\-]+(?:\s+[A-Z][A-Za-z’'\-]+){1,3}", all_lines[idx - 1]):
                        name = all_lines[idx - 1]
                        role = roleline.split(",")[0]
                        self.add_credit(work_id, name, role, url, f"UB case study names {name} immediately before the role credit {roleline}")
        self.source(family, UB_CASE_INDEX, "partial" if len(urls) > count else "ok", len(self.works) - first, f"Fetched {count}/{min(10, len(urls))} company case studies from {len(urls)} links on UB's case-study index.", "Only named credits explicit on the pages were retained; case studies that identify a company but no named worker remain works without person contributions.")

    def collect_competition_articles(self):
        family = "university capstones, design expos, and competitions"
        first = len(self.works)
        pan = self.fetch(PANASCI_2025, "ub-panasci-2025")
        if pan:
            markup = pan[1].decode("utf-8", "replace")
            evidence = "UB's Panasci competition article identifies the 2025 competition winners and states the competition develops viable businesses in Western New York."
            article_text = plain_text(markup)
            entries = [
                ("SATE (Speech Annotation and Transcription Enhancer)", "AI tool that helps speech-language pathologists reduce manual annotation work."),
                ("NIA", "Juice brand offering freshly made beverages with natural ingredients and no added sugars or artificial flavors; the team was announced as the second-place winner of the 2025 Panasci competition."),
                ("AGROWBOTICS", "Autonomous robot that targets and eliminates weeds on small-scale vegetable farms; the team received the 2025 Panasci Audience Choice Award."),
            ]
            sate = re.search(r"winning team\s*[—–-]\s*(.*?)\s+will receive.+?for their company SATE", article_text, re.I | re.S)
            second = re.search(r"In second place were\s+(.*?)\.\s+The team", article_text, re.I | re.S)
            agrobotics = re.search(r"([A-Z][A-Za-z’'-]+(?:\s+[A-Z][A-Za-z’'-]+){1,3}\s+and\s+[A-Z][A-Za-z’'-]+(?:\s+[A-Z][A-Za-z’'-]+){1,3})\s+took the Audience Choice Award", article_text, re.I)
            extracted: dict[str, list[str]] = {}
            if sate:
                extracted["SATE (Speech Annotation and Transcription Enhancer)"] = self.names_from_pair_text(sate.group(1))
            if second:
                extracted["NIA"] = self.names_from_pair_text(second.group(1))
            if agrobotics:
                extracted["AGROWBOTICS"] = self.names_from_pair_text(agrobotics.group(1))
            for title, desc in entries:
                wid = self.add_work(url=PANASCI_2025, title=title, kind="student startup competition project", organization="University at Buffalo Panasci Technology Entrepreneurship Competition", geography="Buffalo/WNY", evidence=evidence, description=desc, work_date="2025-04-24", key=title, raw_url=PANASCI_2025)
                for name in extracted.get(title, []):
                    self.add_credit(wid, name, "student venture team member", PANASCI_2025, f"UB competition article names {name} on the {title} team")
                if not extracted.get(title):
                    self.limitations.append(f"Could not parse team member names for {title} from the 2025 Panasci article.")
        self.source(family, PANASCI_2025, "ok" if pan else "blocked", len(self.works) - first, "Fetched the 2025 Panasci competition recap; captured first place, second place, and audience choice teams with named members.", "Other years and semifinalist projects are outside this article and were not inferred.")

        # Panasci 2023 winner: official UB article gives both founders and product statement.
        rhm_url = "https://www.buffalo.edu/partnerships/about/news-events/news-detail-template.host.html/content/shared/www/partnerships/news/2025/Former-Panasci-winner-RHM-Innovations-secures-100%2C000-investment.detail.html"
        rhm = self.fetch(rhm_url, "ub-rhm-panasci-2023-winner") if self.can_request() else None
        if rhm:
            txt = plain_text(rhm[1].decode("utf-8", "replace"))
            description = "RHM Innovations develops assistive bathing technologies; UB reports that the company won the top prize in the 2023 Panasci competition."
            wid = self.add_work(url=rhm_url, title="RHM Innovations — assistive bathing technology", kind="student startup competition project", organization="University at Buffalo Panasci Technology Entrepreneurship Competition", geography="Buffalo/WNY", evidence="UB reports RHM Innovations won the top prize at the Panasci competition and received UB Cultivator support; the source is a UB startup story.", description=description, key="RHM Innovations 2023 Panasci", raw_url=rhm_url)
            founder_pair = re.search(r"founded by (?:the )?(?:husband-and-wife )?team\s+([A-Z][A-Za-z’'-]+)\s+and\s+([A-Z][A-Za-z’'-]+)(?:\s+[\"'‘]([^\"'’]+)[\"'’])?\s+([A-Z][A-Za-z’'-]+)", txt, re.I)
            names = []
            if founder_pair:
                # The source supplies a surname for Courtney but only Brandon's
                # first name. Preserve what it says instead of inferring a shared surname.
                names = [founder_pair.group(1), f"{founder_pair.group(2)}" + (f' "{founder_pair.group(3)}"' if founder_pair.group(3) else "") + f" {founder_pair.group(4)}"]
            for name in names:
                self.add_credit(wid, name, "co-founder", rhm_url, f"UB story identifies {name} as an RHM Innovations co-founder")
            if not names:
                self.limitations.append("Could not parse named RHM Innovations co-founders from UB's 2023 Panasci report.")
            self.source(family, rhm_url, "ok", 1, "Fetched UB's report on the 2023 Panasci winner RHM Innovations.", "The report does not name all participants in the original student team; only explicitly credited co-founders were recorded. It gives Brandon's first name but not his surname, so no shared surname was inferred.")
        else:
            self.source(family, rhm_url, "blocked", 0, "RHM Innovations story was not fetched.", "Request budget/time exhausted or source failed.")
        self.source("UB Cultivator seed companies", rhm_url, "partial" if rhm else "blocked", 1 if rhm else 0, "One UB Cultivator-supported startup, RHM Innovations, was documented through UB's Panasci winner report.", "The general UB Cultivator company roster was not collected; this is one identified company, not a cohort census.")

    @staticmethod
    def names_from_pair_text(value: str) -> list[str]:
        value = re.sub(r"^.*?\b(?:team|winners?)\s*[—–:-]\s*", "", value, flags=re.I)
        pair = re.split(r"\s+and\s+", value, maxsplit=1, flags=re.I)
        if len(pair) != 2:
            return []
        result = []
        for part in pair:
            part = part.split(",", 1)[0].split(";", 1)[0]
            hit = re.match(r"\s*([A-Z][A-Za-z’'-]+(?:\s+[A-Z][A-Za-z’'-]+){1,2})\b", part)
            if hit:
                result.append(clean(hit.group(1)))
        return result if len(result) == 2 else []

    def collect_awards_and_manufacturing(self):
        # These separate source records make the award and manufacturing credit attempts explicit.
        award_family = "professional awards"
        first = len(self.works)
        art = self.fetch(AIA_2024, "aia-buffalo-2024-awards") if self.can_request() else None
        if art:
            markup = art[1].decode("utf-8", "replace")
            extracted = self.extract_aia_projects(markup)
            for project in extracted:
                wid = self.add_work(url=AIA_2024, title=project["title"], kind="architecture design award project", organization="AIA Buffalo/WNY Design Awards", geography="Buffalo/WNY", evidence=project["regional_evidence"], description=project["description"], key=project["title"], raw_url=AIA_2024)
                for name in project["people"]:
                    self.add_credit(wid, name, project["role"], AIA_2024, f"UB award story explicitly credits {name} as a {project['role']} on {project['title']}")
            self.source(award_family, AIA_2024, "ok", len(self.works) - first, "Fetched UB's 2024 AIA Buffalo/WNY Design Awards article and retained named student projects identifiable in its text.", "This story is a selective announcement rather than the full chapter award archive; only explicit project-level names and credits were kept.")
        else:
            self.source(award_family, AIA_2024, "blocked", 0, "The official UB awards story was not fetched.", "Request budget/time exhausted or source failed.")

        manufacturing_family = "manufacturing and fabrication project credits"
        first = len(self.works)
        cmi = self.fetch(CMI_2026, "ub-cmi-manufacturing-projects-2026") if self.can_request() else None
        if cmi:
            markup = cmi[1].decode("utf-8", "replace")
            projects = self.extract_cmi_projects(markup)
            if projects:
                for project in projects:
                    wid = self.add_work(url=CMI_2026, title=project["title"], kind="faculty-industry manufacturing R&D project", organization=project["industry_partner"], geography="Buffalo/WNY", evidence="UB identifies this as a 2025–26 Faculty-Industry Applied Research project awarded by Buffalo-based CMI; the project pairs named UB faculty with the listed industry partner.", description=project["description"], key=project["title"], raw_url=CMI_2026)
                    for name in project["faculty"]:
                        self.add_credit(wid, name, "faculty project team member", CMI_2026, f"UB CMI names {name} in the Faculty credit for the project with {project['industry_partner']}")
            else:
                self.limitations.append("UB CMI article fetched but expected project portfolio headline was not parseable.")
            self.source(manufacturing_family, CMI_2026, "ok" if projects else "partial", len(self.works) - first, f"Fetched the UB report and parsed {len(projects)} individually described 2025–26 faculty-industry applied manufacturing R&D projects.", "The source names UB faculty and industry partners but does not identify individual company staff; no names were inferred for industry teams.")
        else:
            self.source(manufacturing_family, CMI_2026, "blocked", 0, "The UB manufacturing R&D source was not fetched.", "Request budget/time exhausted or source failed.")

    @staticmethod
    def extract_aia_projects(markup: str) -> list[dict]:
        text = clean(plain_text(markup).replace("\n", " "))
        output = []
        degree = re.search(r"(?P<people>.+?)\s+received the 2024 Student Project of the Year award for\s+[“\"](?P<title>[^”\"]+)[”\"],?\s*(?P<description>[^.]+)", text)
        if degree and degree.group("people"):
            output.append({
                "title": clean(degree.group("title")).rstrip(","),
                "description": clean(degree.group("description")),
                "people": Collector.names_from_pair_text(degree.group("people")),
                "role": "student architecture project designer",
                "regional_evidence": "UB reports that the award-winning DEGREE housing concept was designed for a vacant lot on Buffalo's West Side and received an AIA Buffalo/WNY student award.",
            })
        dormer = re.search(r"Winning a Merit Award in the Unbuilt category was\s+(?P<title>[^,]+),\s+(?P<description>a bridge[^.]+)\.\s+It was designed by\s+(?P<people>[^.]+)", text, re.I)
        if dormer:
            credits = re.sub(r"^(?:UB\s+)?architecture faculty members?\s+", "", dormer.group("people"), flags=re.I)
            credits = re.sub(r"\s+of the creative practice.*$", "", credits, flags=re.I)
            output.append({
                "title": clean(dormer.group("title")),
                "description": clean(dormer.group("description")),
                "people": Collector.names_from_pair_text(credits),
                "role": "architecture co-designer",
                "regional_evidence": "UB describes the design as a bridge between multi-family dwellings in Buffalo and reports that it received an AIA Buffalo/WNY merit award.",
            })
        return [project for project in output if project["title"] and project["people"]]

    @staticmethod
    def extract_cmi_projects(markup: str) -> list[dict]:
        txtlines = lines(markup)
        start = next((i for i, line in enumerate(txtlines) if line.startswith("The faculty members awarded CMI FIAR support")), -1)
        if start < 0:
            return []
        end = next((i for i in range(start + 1, len(txtlines)) if txtlines[i].startswith("For more information on CMI")), len(txtlines))
        section = txtlines[start + 1 : end]
        projects = []
        index = 0
        while index < len(section):
            if not section[index].startswith("Faculty:"):
                index += 1
                continue
            faculty_line = section[index][len("Faculty:") :].strip()
            industry_line = section[index + 1] if index + 1 < len(section) and section[index + 1].startswith("Industry partner:") else ""
            project_line = section[index + 2] if index + 2 < len(section) and section[index + 2].startswith("Project:") else ""
            if not industry_line or not project_line:
                index += 1
                continue
            industry = industry_line[len("Industry partner:") :].strip()
            description = project_line[len("Project:") :].strip()
            faculty = [clean(name.split(",", 1)[0]) for name in re.split(r";\s*and\s+", faculty_line) if clean(name.split(",", 1)[0])]
            partner = re.split(r"\s+(?:is|are|employs|helps|invent\w*|develop\w*|deliver\w*|provides|uses|leverages)\b", industry, maxsplit=1, flags=re.I)[0].strip()
            title = f"{partner} — {description}"
            if faculty and partner and description:
                projects.append({"title": title, "description": description, "industry_partner": partner, "faculty": faculty})
            index += 3
        return projects

    def run(self):
        self.collect_43north()
        self.collect_rit()
        self.collect_ur()
        self.collect_competition_articles()
        self.collect_case_studies()
        self.collect_awards_and_manufacturing()
        # Each expected family remains visible when request/time limits prevent a fetch.
        families = {s["family"] for s in self.sources}
        for family, url in [
            ("accelerator cohorts and demo days", "https://43north.org/portfolio-companies/"),
            ("university capstones, design expos, and competitions", RIT_INDEX),
            ("company case studies and technical project credits", UB_CASE_INDEX),
            ("professional awards", AIA_2024),
            ("manufacturing and fabrication project credits", CMI_2026),
        ]:
            if family not in families:
                self.source(family, url, "blocked", 0, "Not reached before the request or time ceiling.", "Source family retained in manifest; collector stopped at its configured budget.")

        self.works.sort(key=lambda row: row["id"])
        self.contributions.sort(key=lambda row: (row["work_id"], row["name"], row["role"]))
        with (self.out / "works.jsonl").open("w", encoding="utf-8") as f:
            for row in self.works:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        with (self.out / "contributions.jsonl").open("w", encoding="utf-8") as f:
            for row in self.contributions:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        manifest = {
            "lane": "builders_business",
            "started_at": self.started,
            "completed_at": utc_now(),
            "requests": self.request_count,
            "reused_raw_responses": self.reused_raw,
            "works": len(self.works),
            "contributions": len(self.contributions),
            "complete": False,
            "scope": "Publicly credited startup products, capstone and expo projects, case studies, awards, and manufacturing research projects connected to Buffalo/WNY or labeled Rochester/Finger Lakes.",
            "sources": self.sources,
            "limitations": self.limitations + [
                "Bounded collection is not a universal census; see each source coverage field for pagination and selection.",
                "Student credits reflect only public team listings and carry the project's institution geography; no residence or continuing regional tie is inferred.",
            ],
            "errors": self.errors,
        }
        (self.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/work-discovery/2026-09-28/builders_business"))
    parser.add_argument("--max-requests", type=int, default=150)
    parser.add_argument("--minutes", type=float, default=15)
    parser.add_argument("--max-rit", type=int, default=58)
    parser.add_argument("--max-ur", type=int, default=15)
    parser.add_argument("--reuse-raw", action="store_true", help="reuse same-day raw snapshots in the output directory and request only uncached pages")
    args = parser.parse_args()
    c = Collector(args.output, args.max_requests, args.minutes, args.max_rit, args.max_ur, args.reuse_raw)
    manifest = c.run()
    print(json.dumps({"output": str(args.output), "requests": manifest["requests"], "works": manifest["works"], "contributions": manifest["contributions"], "errors": len(manifest["errors"])}, sort_keys=True))


if __name__ == "__main__":
    main()
