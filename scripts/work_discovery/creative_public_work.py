#!/usr/bin/env python3
"""Bounded public-work collection for Buffalo and Rochester creative communities.

Uses only the Python standard library plus the existing ``pdftotext`` command
for one public planning report. Collected source bytes and JSONL output stay in
the ignored data/work-discovery directory unless --output is supplied.
"""

from __future__ import annotations

import argparse
import email.utils
from difflib import SequenceMatcher
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


LANE = "creative_public_work"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "data/work-discovery/2026-09-28" / LANE
USER_AGENT = "BPCommonsPublicWorkCollector/1.0 (regional-talent research)"
NAME = r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]*(?:\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]*){1,4}"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
    return (value[:86] or "source").lower()


class Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.headings: list[tuple[str, str]] = []
        self.skip = 0
        self.anchor: str | None = None
        self.anchor_text: list[str] = []
        self.heading: str | None = None
        self.heading_text: list[str] = []
        self.in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.skip += 1
        attr = dict(attrs)
        if tag == "a":
            self.anchor, self.anchor_text = attr.get("href"), []
        if tag in {"h1", "h2", "h3", "h4"}:
            self.heading, self.heading_text = tag, []
        if tag == "title":
            self.in_title = True
        if tag in {"p", "li", "br", "div", "section", "article", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "template"} and self.skip:
            self.skip -= 1
        if tag == "a" and self.anchor is not None:
            self.links.append((compact("".join(self.anchor_text)), self.anchor))
            self.anchor = None
        if self.heading == tag:
            self.headings.append((tag, compact("".join(self.heading_text))))
            self.heading = None
        if tag == "title":
            self.in_title = False
        if tag in {"p", "li", "br", "div", "section", "article", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self.parts.append(data)
        if self.anchor is not None:
            self.anchor_text.append(data)
        if self.heading is not None:
            self.heading_text.append(data)
        if self.in_title:
            self.title_parts.append(data)

    @property
    def text(self) -> str:
        lines = [compact(s) for s in "".join(self.parts).splitlines()]
        return "\n".join(s for s in lines if s)

    def h1(self) -> str:
        return next((v for tag, v in self.headings if tag == "h1" and v), "")

    @property
    def title(self) -> str:
        return compact("".join(self.title_parts))


class Collector:
    def __init__(self, output: Path, max_requests: int, max_seconds: int, offline: bool = False) -> None:
        self.output = output
        self.raw_dir = output / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.max_requests = max_requests
        self.max_seconds = max_seconds
        self.offline = offline
        self.started = time.monotonic()
        self.started_at = now()
        self.requests = 0
        self.works: list[dict] = []
        self.contributions: list[dict] = []
        self.sources: list[dict] = []
        self.errors: list[str] = []
        self.unresolved: dict[tuple[str, str], dict] = {}
        self.seen_work: set[str] = set()
        self.seen_contrib: set[tuple[str, str, str]] = set()

    def remaining(self) -> bool:
        return self.requests < self.max_requests and time.monotonic() - self.started < self.max_seconds

    def fetch(self, url: str, key: str, *, binary: bool = False) -> tuple[bytes, Path] | None:
        if not self.remaining():
            return None
        # Reuse an exact prior raw snapshot when the URL, key, and byte count match.
        prior_manifest = self.output / "manifest.json"
        previous = {}
        if prior_manifest.exists():
            try:
                previous = json.loads(prior_manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        prior_results = [request for source in previous.get("sources", []) for request in source.get("request_results", [])
                         if request.get("url") == url and request.get("status") in {200, "cached"}]
        candidates = sorted(self.raw_dir.glob(f"{slug(key)}-*"))
        for candidate in candidates:
            if prior_results and candidate.is_file() and candidate.stat().st_size == prior_results[-1].get("bytes"):
                payload = candidate.read_bytes()
                self._request_result(url, "cached", len(payload), prior_results[-1].get("final_url", url))
                return payload, candidate
        if self.offline:
            prior_error = next((error for error in previous.get("errors", []) if error.startswith(url + ":")), "")
            message = prior_error or f"{url}: offline cache miss; no network request was made"
            self.errors.append(message)
            self._request_result(url, "cached miss", 0, "")
            return None
        self.requests += 1
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
                final_url = response.geturl()
                expected = response.headers.get("Content-Length")
                if expected and len(payload) != int(expected):
                    raise IOError(f"incomplete response: expected {expected} bytes, received {len(payload)}")
            extension = ".pdf" if binary or url.lower().endswith(".pdf") else (".xml" if ".rss" in url.lower() or "podcast.rss" in url.lower() else ".html")
            digest = hashlib.sha256(payload).hexdigest()
            path = self.raw_dir / f"{slug(key)}-{digest[:10]}{extension}"
            path.write_bytes(payload)
            self._request_result(url, 200, len(payload), final_url)
            return payload, path
        except Exception as exc:
            message = f"{url}: {type(exc).__name__}: {str(exc)[:180]}"
            self.errors.append(message)
            self._request_result(url, "error", 0, "")
            return None

    def _request_result(self, url: str, status: int | str, size: int, final_url: str) -> None:
        if not self.sources:
            return
        row = self.sources[-1]
        row.setdefault("request_results", []).append({"url": url, "status": status, "bytes": size, "final_url": final_url})

    def record_source(self, family: str, name: str, url: str, coverage: str, limitation: str = "") -> dict:
        source = {"family": family, "name": name, "url": url, "status": "attempted", "records": 0, "named_credits": 0, "coverage": coverage, "limitation": limitation, "request_results": []}
        self.sources.append(source)
        return source

    def parse_html(self, payload: bytes) -> Document:
        page = Document()
        page.feed(payload.decode("utf-8", "replace"))
        return page

    def add_work(self, *, title: str, kind: str, url: str, raw_path: Path, identifier: str,
                 work_date: str = "", organization: str = "", regional: str,
                 geography: str = "buffalo_western_new_york", description: str = "") -> str:
        title = compact(title)
        if not title:
            return ""
        identity = f"{url}#{identifier or title}"
        wid = "CPW-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        if wid not in self.seen_work:
            self.seen_work.add(wid)
            digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            self.works.append({
                "id": wid, "title": title, "kind": kind, "source_url": url,
                "observed_at": now(), "work_date": work_date, "organization": organization,
                "regional_evidence": compact(regional), "geography_scope": geography,
                "description": compact(description)[:1200], "raw_path": raw_path.relative_to(self.output).as_posix(),
                "raw_sha256": digest,
            })
        return wid

    def add_credit(self, wid: str, name: str, role: str, evidence: str, url: str, person_url: str = "") -> None:
        name, role, evidence = normalize_person_name(name), compact(role), compact(evidence)
        if not wid or len(name) < 5 or not role or not evidence or not is_person_name(name):
            return
        key = (wid, name.casefold(), role.casefold())
        if key in self.seen_contrib:
            return
        self.seen_contrib.add(key)
        self.contributions.append({"work_id": wid, "name": name, "role": role, "person_url": person_url,
                                   "identifiers": {}, "evidence": evidence[:600], "source_url": url})

    def add_unresolved(self, wid: str, name: str, source_url: str, reason: str, evidence: str) -> None:
        name = compact(name).strip(" ,.;:—–-")
        if wid and name:
            self.unresolved[(wid, name.casefold())] = {"work_id": wid, "name": name, "source_url": source_url,
                                                       "reason": reason, "evidence": compact(evidence)[:400]}

    def add_linked_credit(self, wid: str, name: str, role: str, evidence: str, url: str, page: Document) -> None:
        person_url = ""
        for label, href in page.links:
            if label.casefold() == compact(name).casefold() and href:
                person_url = urljoin(url, href)
                break
        self.add_credit(wid, name, role, evidence, url, person_url)


def date_from(text: str) -> str:
    text = html.unescape(text)
    for pattern in (
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+([A-Z][a-z]+ \d{1,2}, 20\d{2})",
        r"\b([A-Z][a-z]+ \d{1,2}, 20\d{2})",
    ):
        match = re.search(pattern, text)
        if match:
            try:
                return datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
            except ValueError:
                pass
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return match.group(1) if match else ""


def named_people(text: str) -> list[str]:
    result = []
    for match in re.finditer(NAME, text):
        name = normalize_person_name(match.group(0))
        if name and name not in result:
            result.append(name)
    return result


ORG_OR_SUBJECT = re.compile(
    r"\b(?:foundation|association|society|network|university|college|institute|department|"
    r"gallery|galleries|museum|center|centre|studios?|collective|committee|commission|"
    r"conservancy|ministry|office|council|coalition|organization|organisation|agency|"
    r"corporation|incorporated|llc|ltd|company|project|community|farm|festival|fest|"
    r"marathon|butterfly|pharmacies|counseling|counselling|patrol|troopers|dumplings|"
    r"trio|band|boys|victims|safety|awareness|prevention|history|adolescents|clothing|lab|wheel|"
    r"cancer|nature|hope|recipients?|volunteers?|department|production|support|staff|"
    r"resident|residents|workspace|retrospective|animation|study|service|places?)\b", re.I)
NON_NAME_WORDS = re.compile(r"\b(?:a|an|of|by|for|the|and|from|among|inside|saving|protecting|separating|how|what|when|where|why|was|are|is|with|out)\b", re.I)
NON_PERSON_PHRASES = re.compile(r"\b(?:buffalo|western new york|new york|finger lakes|los angeles|hudson valley|niagara falls|south buffalo|black history|mlk day|elder law|best self|exhibition artists)\b", re.I)
PREFIX_TITLES = re.compile(r"^(?:dr\.?|prof(?:essor)?\.?|executive director|director|chef|local historian|rock angel|facebook)\s+", re.I)
ALIAS_PREFIX = re.compile(r"^(?:the|captain|king|brother|sister|dj|b-roll)\b", re.I)


def normalize_person_name(value: str) -> str:
    value = compact(value).strip(" ,.;:—–-")
    value = re.split(r"\s+(?:of|from)\s+", value, maxsplit=1, flags=re.I)[0]
    value = PREFIX_TITLES.sub("", value)
    value = re.sub(r"\s+(?:aka|a\.k\.a\.)\s+.*$", "", value, flags=re.I)
    value = re.sub(r"[,.]?\s+(?:see|listen|learn more)\b.*$", "", value, flags=re.I)
    return value.strip(" ,.;:—–-")


def is_person_name(value: str) -> bool:
    name = normalize_person_name(value)
    if len(name) < 5 or len(name.split()) < 2 or len(name.split()) > 5:
        return False
    if not re.fullmatch(NAME, name):
        return False
    if ORG_OR_SUBJECT.search(name) or NON_PERSON_PHRASES.search(name) or NON_NAME_WORDS.search(name) or ALIAS_PREFIX.search(name):
        return False
    tokens = name.split()
    if len(tokens) == 2 and tokens[0].casefold() == tokens[1].casefold():
        return False
    if any(len(token) == 1 or (token.isupper() and len(token) <= 4) for token in tokens):
        return False
    return True


def podcast_credit_uncertainty(name: str, summary: str, title: str) -> str:
    escaped = re.escape(name)
    group_cues = re.compile(
        rf"(?:\b(?:two\s+)?(?:sisters?|boys?|members?|owner|member)\s+(?:of|from)\s+{escaped}\b|"
        rf"\b[A-Z][a-z]+\s+from\s+{escaped}\b|"
        rf"\b{escaped}\b.{{0,100}}\b(?:band|bandmates|food ?truck|duo|trio|collective|group|business)\b|"
        rf"\b(?:band|bandmates|food ?truck|duo|trio|collective|group|business)\b.{{0,100}}\b{escaped}\b)", re.I)
    if group_cues.search(summary):
        return "Show notes identify this title as a band, business, collective, or other group; no named individual credit is substituted."
    if name.casefold() not in title.casefold() and re.search(r"\b(?:stage name|alias|moniker|also known as)\b", summary, re.I):
        return "The show notes identify this as an alias without resolving it to a named individual."
    words = name.split()
    alias_markers = re.compile(r"\b(?:man|child|crazy|key|black|terror|nose|snake|goddess|saint|hush|aquarious|roll|rev|bond|veggie)\b", re.I)
    if re.search(r"[.-]", name) or any(token.isupper() and len(token) <= 4 for token in words) or (len(words) == 2 and words[0][0].casefold() == words[1][0].casefold()) or (len(words) == 2 and words[0].casefold() == words[1].casefold()):
        if not re.search(rf"\b{escaped}\b.{{0,60}}\breal name\b", summary, re.I):
            return "The feed identifies a stylized or alias-like title only; it does not provide a resolved personal name."
    if alias_markers.search(name) and not re.search(rf"\b{escaped}\b.{{0,60}}\breal name\b", summary, re.I):
        return "The feed identifies an alias-like performer name only; it does not provide a resolved personal name."
    return ""


def plain_html(text: str) -> str:
    parser = Document()
    parser.feed(text)
    return compact(parser.text)


def paragraphs(text: str) -> list[str]:
    chunks = re.findall(r"<p\b[^>]*>(.*?)</p\s*>", text, flags=re.I | re.S)
    return [plain_html(chunk) for chunk in chunks if plain_html(chunk)]


def content_title(page: Document) -> str:
    title = page.title.split(" | ", 1)[0].strip()
    for suffix in (" - UB Art Galleries", " - University at Buffalo", " - University at Buffalo Art Galleries"):
        title = title.removesuffix(suffix)
    return title or page.h1()


def parse_biff_page(c: Collector, url: str, payload: bytes, raw: Path, page: Document, year: str) -> int:
    full = page.text
    title = page.title.split(" | ", 1)[0].strip() or page.h1()
    if not title:
        return 0
    venue = ""
    for candidate in ("Hallwalls Contemporary Arts Center", "North Park Theatre", "Buffalo Toronto Public Media", "Scene One Market Arcade 8", "Buffalo & Erie County Downtown Central Library", "Buffalo AKG Art Museum", "The Screening Room"):
        if candidate.casefold() in full.casefold():
            venue = candidate
            break
    work_date = date_from(full)
    regional = f"Buffalo International Film Festival's official {year} listing names {venue + ' in Buffalo' if venue else 'a festival screening/event'} and publishes this credit."
    if year == "2025" and "Western New York" in full:
        regional += " The listing explicitly marks a Western New York premiere or connection."
    desc = re.sub(r".*?Venue Website\s+.*?(?:Free Event|RSVP)?", "", full, flags=re.S)
    desc = compact(desc)
    if not desc:
        desc = f"The official Buffalo International Film Festival {year} archive lists this program."
    added = 0
    film_pairs = re.findall(r"[“\"]([^”\"]{2,120})[”\"]\s+((?:[A-Z][\w'’.-]+\s+){1,4}[A-Z][\w'’.-]+)\s*\(\s*(?:Co-)?Dir\s*\)", full)
    for film_title, director in film_pairs:
        wid = c.add_work(title=film_title, kind="film", url=url, raw_path=raw,
                         identifier=f"film:{film_title}:{director}", work_date=work_date,
                         organization="Buffalo International Film Festival", regional=regional,
                         description=desc[:1000])
        if is_person_name(director):
            c.add_credit(wid, director, "director", f"Official BIFF listing credits {director} as director of {film_title}.", url)
            added += 1
        else:
            c.add_unresolved(wid, director, url, "Director credit did not resolve to a verified individual name.", full[:400])
    dirs = list(dict.fromkeys(re.findall(r"((?:[A-Z][\w'’.-]+\s+){1,4}[A-Z][\w'’.-]+)\s*\(\s*(?:Co-)?Dir\s*\)", full)))
    if not film_pairs and dirs:
        # Single-title pages identify a film in their document title and credit its director in the listing.
        for director in dirs[:3]:
            if not re.search(r"\b(?:film|short|documentary|screening|cinema|festival)\b", title + " " + full[:800], re.I):
                break
            wid = c.add_work(title=title, kind="film", url=url, raw_path=raw,
                             identifier=f"film:{title}:{director}", work_date=work_date,
                             organization="Buffalo International Film Festival", regional=regional,
                             description=desc[:1000])
            if is_person_name(director):
                c.add_credit(wid, director, "director", f"Official BIFF listing credits {director} as director of {title}.", url)
                added += 1
            else:
                c.add_unresolved(wid, director, url, "Director credit did not resolve to a verified individual name.", full[:400])
    # The festival publishes exact panelist roles in a comma-separated sentence.
    panel_match = re.search(r"Panelists?:\s*(.*?)(?=\s+Moderator:|\s+\*|$)", full, re.I)
    moderator_match = re.search(r"Moderator:\s*([^*\n]+)", full, re.I)
    panelists = panel_match.group(1) if panel_match else ""
    moderator = moderator_match.group(1).strip() if moderator_match else ""
    if panelists or moderator:
        wid = c.add_work(title=title, kind="talk", url=url, raw_path=raw,
                         identifier=f"talk:{title}", work_date=work_date,
                         organization="Buffalo International Film Festival", regional=regional,
                         description=desc[:1000])
        for role, person in re.findall(r"(?:^|,\s*(?:and\s+)?)(Director|Producer|Documentary Participant|Writer|Star)\s+((?:[A-Z][\w'’.-]+\s+){1,3}[A-Z][\w'’.-]+)", panelists):
            c.add_credit(wid, person, role.casefold(), f"Official BIFF panel listing identifies {person} as {role}.", url)
            added += 1
        person_match = re.match(NAME, normalize_person_name(moderator))
        if person_match and is_person_name(person_match.group(0)):
            c.add_credit(wid, person_match.group(0), "moderator", f"Official BIFF panel listing identifies {person_match.group(0)} as moderator.", url)
            added += 1
    return added


def collect_biff(c: Collector, detail_limit: int = 38) -> None:
    base = "https://www.buffalofilm.org"
    for year, listing in (("2025", f"{base}/archive/2025/"),):
        source = c.record_source("film_game_performance", f"Buffalo International Film Festival {year} official archive", listing,
                                 "Archive page plus up to 38 event detail pages from the 2025 lineup; detail pages preserve listed film directors, panelist credits, dates, and Buffalo venues.",
                                 "Limited to the first bounded set of public lineup events; group screenings may omit complete individual short-film credits.")
        result = c.fetch(listing, f"biff-{year}-archive")
        if not result:
            source["status"] = "blocked or budget exhausted"
            continue
        payload, raw = result
        page = c.parse_html(payload)
        links: list[str] = []
        for label, href in page.links:
            full = urljoin(listing, href)
            if urlparse(full).netloc.endswith("buffalofilm.org") and "/events/" in full and full not in links:
                links.append(full)
        works_before, credits_before = len(c.works), len(c.contributions)
        for i, url in enumerate(links[:detail_limit]):
            result = c.fetch(url, f"biff-{year}-{i}-{urlparse(url).path.split('/')[-2]}")
            if not result:
                break
            body, raw_detail = result
            doc = c.parse_html(body)
            parse_biff_page(c, url, body, raw_detail, doc, year)
        source["records"] = len(c.works) - works_before
        source["named_credits"] = len(c.contributions) - credits_before
        source["status"] = "partial" if len(links) > detail_limit else "fetched"
        source["coverage"] += f" Fetched {min(len(links), detail_limit)} of {len(links)} linked event pages."


def extract_short_credit_text(text: str, title: str, kind: str, url: str, page: Document, c: Collector,
                              *, organization: str, regional: str, geography: str = "buffalo_western_new_york",
                              date_hint: str = "", raw: Path) -> int:
    date = date_hint or date_from(text)
    credit_candidates: list[tuple[str, str, str]] = []
    # Explicit credit labels on gallery, arts, and program pages.
    labels = [
        ("Curated by", "curator"), ("Curator:", "curator"), ("Curators:", "curator"),
        ("Artist:", "artist"), ("Artists:", "exhibiting artist"), ("Photographers:", "photographer"),
        ("Project Coordinators:", "project coordinator"), ("Project Coordinator:", "project coordinator"),
        ("Director:", "director"), ("Directed by:", "director"), ("Facilitator:", "workshop facilitator"),
        ("Workshop leader:", "workshop leader"), ("Speaker:", "speaker"), ("Speakers:", "speaker"),
        ("Moderated by curator", "moderator"),
        ("featuring", "featured artist"),
    ]
    for label, role in labels:
        pattern = re.compile(re.escape(label) + r"\s+(.{0,260}?)(?=\.|\n|;|\s+The exhibition|\s+This exhibition|\s+Facebook|\s+About the artist|\s+Credits|$)", re.I)
        for match in pattern.finditer(text):
            for name in named_people(match.group(1)):
                credit_candidates.append((name, role, f"{label} {match.group(1).strip()}"))
    # For solo shows, a person's full name is often part of the official title.
    if kind in {"exhibition", "public art", "architecture design", "film", "performance"}:
        suffixes = re.findall(rf"(?:\bby\s+|:\s*|\|\s*)({NAME})(?=\s*(?:[|:,.;]|$))", title)
        for name in suffixes:
            credit_candidates.append((name, "featured artist" if kind in {"exhibition", "public art"} else "credited creator", f"Official page title identifies {name} in the credited work title."))
    # Use generic artist credit lists when the copy explicitly says "artists include".
    for match in re.finditer(rf"(?:artists?|filmmakers?|performers?)\s+(?:include|are)\s+(.{{0,320}}?)(?:\.|\n)", text, re.I):
        for name in named_people(match.group(1)):
            credit_candidates.append((name, "exhibiting artist" if kind == "exhibition" else "credited creator", f"The official page lists {name} among the {kind} credits."))
    wid = c.add_work(title=title, kind=kind, url=url, raw_path=raw,
                     identifier=f"{kind}:{title}", work_date=date, organization=organization,
                     regional=regional, geography=geography, description=text[:1000])
    before = len(c.contributions)
    title_names = {
        normalize_person_name(name)
        for name, _, evidence in credit_candidates
        if evidence.startswith("Official page title identifies")
    }
    spelling_conflicts: dict[str, str] = {}
    text_names = [name for name in named_people(text) if is_person_name(name)]
    for candidate, _, _ in credit_candidates:
        for title_name in title_names:
            if candidate.casefold() != title_name.casefold() and SequenceMatcher(None, candidate.casefold(), title_name.casefold()).ratio() >= 0.88:
                spelling_conflicts[candidate] = title_name
                spelling_conflicts[title_name] = candidate
        for text_name in text_names:
            if candidate.casefold() != text_name.casefold() and SequenceMatcher(None, candidate.casefold(), text_name.casefold()).ratio() >= 0.93:
                spelling_conflicts[candidate] = text_name
                spelling_conflicts[text_name] = candidate
    for name, role, evidence in credit_candidates:
        if name in spelling_conflicts:
            c.add_unresolved(wid, f"{name} / {spelling_conflicts[name]}", url,
                             "This source spells the same apparent credit differently in its title and body; retained unresolved without choosing a spelling.",
                             evidence)
            continue
        if re.search(rf"\b{re.escape(name)}\s*,\s*(?:[A-Za-z .'-]+,\s*)?[A-Z]{{2}}\b", evidence):
            c.add_unresolved(wid, name, url,
                             "The candidate appears in a parenthetical city/state location field, not as a credited person.", evidence)
            continue
        if is_person_name(name):
            c.add_linked_credit(wid, name, role, evidence, url, page)
        else:
            c.add_unresolved(wid, name, url,
                             "The source presents a credit-like phrase, but it is an organization, collective, event, subject, or incomplete alias rather than a verified individual name.",
                             evidence)
    return len(c.contributions) - before


def collect_ub_exhibitions(c: Collector, detail_limit: int = 30) -> None:
    listing = "https://www.buffalo.edu/art-galleries/exhibitions.html"
    source = c.record_source("exhibitions_public_art", "UB Art Galleries exhibitions archive", listing,
                             "Current archive and linked detail pages, initially 2025-26 and 2024-25 pages; archive spans 1994 onward.",
                             "The listing is a work-in-progress archive; only detail pages with explicit named credits are emitted.")
    first = c.fetch(listing, "ub-art-galleries-exhibitions")
    if not first:
        source["status"] = "blocked or budget exhausted"
        return
    payload, raw = first
    doc = c.parse_html(payload)
    seasons = [urljoin(listing, h.split("#", 1)[0]) for label, h in doc.links if re.search(r"2025-26|2024-25|2023-24", h) and h.lower().split("#", 1)[0].endswith(".html")]
    detail_links: list[str] = []
    for season in seasons[:3]:
        result = c.fetch(season, f"ub-gallery-season-{season.rsplit('/',1)[-1]}")
        if not result:
            break
        body, raw_season = result
        d = c.parse_html(body)
        for label, href in d.links:
            url = urljoin(season, href)
            if "/art-galleries/exhibitions/" in url and url.lower().split("#", 1)[0].endswith(".html") and url not in detail_links and not re.search(r"/exhibitions/(?:\d{4}(?:-\d{2})?|cravens-world)\.html", url):
                detail_links.append(url)
    works_before, credits_before = len(c.works), len(c.contributions)
    for i, url in enumerate(detail_links[:detail_limit]):
        result = c.fetch(url, f"ub-gallery-detail-{i}-{urlparse(url).path.split('/')[-1]}")
        if not result:
            break
        body, raw_detail = result
        d = c.parse_html(body)
        title = content_title(d) or next((t for k,t in d.headings if k == "h2" and t), "")
        text = d.text
        region = "UB Art Galleries page lists the University at Buffalo galleries at 1 Martha Jackson Place or the Center for the Arts in Buffalo, NY."
        extract_short_credit_text(text, title, "exhibition", url, d, c,
                                  organization="UB Art Galleries, University at Buffalo", regional=region, raw=raw_detail)
    source["records"] = len(c.works) - works_before
    source["named_credits"] = len(c.contributions) - credits_before
    source["status"] = "partial" if len(detail_links) > detail_limit else "fetched"
    source["coverage"] += f" Fetched {min(len(detail_links), detail_limit)} of {len(detail_links)} detail pages from the 2025-26 through 2023-24 indexes."


def collect_public_art(c: Collector, detail_limit: int = 16) -> None:
    listing = "https://buffaloakg.org/community/ak-public-art"
    source = c.record_source("exhibitions_public_art", "Buffalo AKG Public Art Initiative", listing,
                             "Public Art Initiative project index and up to 16 individual project pages.",
                             "Only pages with explicit individual artist credits are emitted; project map itself is not crawled.")
    result = c.fetch(listing, "akg-public-art-index")
    if not result:
        source["status"] = "blocked or budget exhausted"
        return
    payload, _ = result
    page = c.parse_html(payload)
    links: list[str] = []
    for _, href in page.links:
        url = urljoin(listing, href)
        if "/community/ak-public-art/" in url and url not in links:
            links.append(url)
    works_before, credits_before = len(c.works), len(c.contributions)
    for i, url in enumerate(links[:detail_limit]):
        result = c.fetch(url, f"akg-public-art-{i}-{urlparse(url).path.split('/')[-1]}")
        if not result:
            break
        body, raw = result
        doc = c.parse_html(body)
        title = content_title(doc)
        region = f"Buffalo AKG's Public Art Initiative page documents this project in Buffalo or Western New York; the source identifies the museum's partnerships with Erie County and the City of Buffalo."
        extract_short_credit_text(doc.text, title, "public art", url, doc, c,
                                  organization="Buffalo AKG Art Museum Public Art Initiative", regional=region, raw=raw)
    source["records"] = len(c.works) - works_before
    source["named_credits"] = len(c.contributions) - credits_before
    source["status"] = "partial" if len(links) > detail_limit else "fetched"
    source["coverage"] += f" Fetched {min(len(links), detail_limit)} of {len(links)} linked projects."


def collect_squeaky(c: Collector, detail_limit: int = 16) -> None:
    listing = "https://squeaky.org/exhibitions-eventsarchive/archive/"
    source = c.record_source("exhibitions_public_art/film_game_performance/talks_workshops", "Squeaky Wheel exhibitions and events archive", listing,
                             "Historical events archive listing, starting at its newest archived year, and up to 16 linked event detail pages.",
                             "Archive landing page reported 2023 and prior content; event listing endpoint for the current season returned HTTP 500.")
    result = c.fetch(listing, "squeaky-archive")
    if not result:
        source["status"] = "blocked or budget exhausted"
        return
    payload, _ = result
    page = c.parse_html(payload)
    links: list[tuple[str,str]] = []
    for label, href in page.links:
        url = urljoin(listing, href)
        if "/event/" in url and url not in [u for _,u in links]:
            links.append((label, url))
    works_before, credits_before = len(c.works), len(c.contributions)
    for i, (label, url) in enumerate(links[:detail_limit]):
        result = c.fetch(url, f"squeaky-{i}-{urlparse(url).path.split('/')[-2]}")
        if not result:
            break
        body, raw = result
        doc = c.parse_html(body)
        title = content_title(doc) or label
        text = doc.text
        kind = "workshop" if re.search(r"workshop|orientation|class|lab", title, re.I) else ("talk" if re.search(r"talk|conversation|in conversation|reading series", title, re.I) else ("film" if re.search(r"film|screening|animation", title, re.I) else "exhibition"))
        region = "Squeaky Wheel identifies itself as a Buffalo film and media art center; the event archive lists these works and programs under its Buffalo exhibition and events calendar."
        extract_short_credit_text(text, title, kind, url, doc, c,
                                  organization="Squeaky Wheel Film & Media Art Center", regional=region, raw=raw)
    source["records"] = len(c.works) - works_before
    source["named_credits"] = len(c.contributions) - credits_before
    source["status"] = "partial" if len(links) > detail_limit else "fetched"
    source["coverage"] += f" Found {len(links)} archive links and fetched {min(len(links), detail_limit)} event details."


def collect_cepa(c: Collector, detail_limit: int = 14) -> None:
    listing = "https://www.cepagallery.org/exhibits-events/archive/"
    source = c.record_source("exhibitions_public_art/film_game_performance/talks_workshops", "CEPA Gallery past exhibitions and events", listing,
                             "Past exhibitions/events index and up to 14 named exhibit or program details.",
                             "The page includes non-work items; only detail pages with explicit human credits are emitted.")
    result = c.fetch(listing, "cepa-exhibitions-archive")
    if not result:
        source["status"] = "blocked or budget exhausted"
        return
    payload, _ = result
    page = c.parse_html(payload)
    links: list[tuple[str,str]] = []
    for label, href in page.links:
        url = urljoin(listing, href)
        if "/exhibit-event/" in url and url not in [u for _,u in links]:
            links.append((label, url))
    works_before, credits_before = len(c.works), len(c.contributions)
    for i,(label,url) in enumerate(links[:detail_limit]):
        result = c.fetch(url, f"cepa-{i}-{urlparse(url).path.split('/')[-2]}")
        if not result: break
        body, raw = result
        doc = c.parse_html(body)
        title = content_title(doc) or label
        kind = "workshop" if re.search(r"workshop|class|talk|roundtable", title, re.I) else ("film" if re.search(r"film|screening|video", title, re.I) else "exhibition")
        region = "CEPA Gallery identifies its exhibition and event work with its Buffalo, New York gallery; the page lists this program as presented by CEPA."
        extract_short_credit_text(doc.text, title, kind, url, doc, c,
                                  organization="CEPA Gallery", regional=region, raw=raw)
    source["records"] = len(c.works) - works_before
    source["named_credits"] = len(c.contributions) - credits_before
    source["status"] = "partial" if len(links) > detail_limit else "fetched"
    source["coverage"] += f" Found {len(links)} exhibit/event links and fetched {min(len(links), detail_limit)} detail pages."


def collect_podcast(c: Collector, feed_url: str, name: str, regional: str, limit: int = 100) -> None:
    source = c.record_source("podcasts_public_show_notes", name, feed_url,
                             f"One current public RSS snapshot; up to {limit} episode descriptions and publication dates.",
                             "Guest credits are emitted only where show notes or episode title give a full name; abbreviated/alias-only titles remain unresolved.")
    result = c.fetch(feed_url, slug(name))
    if not result:
        source["status"] = "blocked or budget exhausted"
        return
    payload, raw = result
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        source["status"] = "error"
        source["limitation"] += f" Feed XML could not be parsed: {exc}."
        return
    channel = root.find("./channel")
    channel_desc = ""
    if channel is not None:
        d = channel.find("description")
        channel_desc = compact(plain_html(d.text or "")) if d is not None else ""
    items = root.findall(".//item")[:limit]
    works_before, credits_before = len(c.works), len(c.contributions)
    for item in items:
        def first_tag(names: set[str]) -> str:
            for child in list(item):
                if child.tag.rsplit("}",1)[-1] in names:
                    return "".join(child.itertext())
            return ""
        title = compact(first_tag({"title"}))
        url = compact(first_tag({"link"})) or feed_url
        summary_raw = first_tag({"encoded", "description"})
        summary = compact(plain_html(summary_raw))[:1400]
        guid = compact(first_tag({"guid"})) or title
        if not title:
            continue
        date = ""
        pubdate = first_tag({"pubDate"})
        if pubdate:
            try:
                date = email.utils.parsedate_to_datetime(pubdate).date().isoformat()
            except Exception:
                date = date_from(pubdate)
        work_title = re.sub(r"^\[[^]]+\]\s*", "", title)
        wid = c.add_work(title=work_title, kind="podcast episode", url=url, raw_path=raw,
                         identifier=f"episode:{guid}", work_date=date, organization=name,
                         regional=regional, description=summary or f"Public episode listing for {work_title}.")
        guest_patterns = (
            rf"\b(?:our guest is|guest is|interviews? with|talks? with|speaks? with|welcomes?|features?)\s+(?:host\s+)?({NAME})\b",
            rf"\b(?:guest|artist|filmmaker|musician|speaker)\s+(?:is\s+)?({NAME})\b",
        )
        guest = ""
        for pattern in guest_patterns:
            m = re.search(pattern, summary)
            if m:
                guest = normalize_person_name(m.group(1))
                break
        resolved_alias = re.search(rf"\b(?:also known as|real name is|legal name is)\s+({NAME})\b", summary, re.I)
        if resolved_alias:
            guest = normalize_person_name(resolved_alias.group(1))
        if not guest and "Buffalo Music Players" in name:
            # A full name after the final episode-title delimiter is often the credited guest.
            guest_title = re.split(r"\s+(?:of|from)\s+", work_title, maxsplit=1, flags=re.I)[0]
            m = re.search(rf"(?:Episode\s+\d+|BREAKING EPISODE|Podcast BREAKING EPISODE):\s*({NAME})(?=\s*(?:$|\band\b|&|:))", guest_title, re.I)
            if m:
                guest = normalize_person_name(m.group(1))
        if guest:
            quote_match = next((m.group(0) for p in paragraphs(summary_raw) if (m := re.search(rf".{{0,80}}\b{re.escape(guest)}\b.{{0,160}}", p, re.I))), "")
            evidence = f"The public show notes identify {guest} as the episode's interview subject/guest: {quote_match or summary[:220]}"
            uncertainty = "" if resolved_alias else podcast_credit_uncertainty(guest, summary, work_title)
            if is_person_name(guest) and not uncertainty:
                c.add_credit(wid, guest, "interview guest", evidence, url)
            else:
                c.add_unresolved(wid, guest, url,
                                 uncertainty or "Podcast title/show-note guest candidate is an alias or organization/event/topic phrase without enough evidence to verify an individual identity.",
                                 evidence)
    source["records"] = len(c.works) - works_before
    source["named_credits"] = len(c.contributions) - credits_before
    source["status"] = "fetched"
    source["coverage"] += f" Parsed {min(len(items), limit)} newest episode records; {source['named_credits']} have verified full-name guest credits."
    if channel_desc:
        source["channel_description"] = channel_desc[:500]


def collect_architecture(c: Collector) -> None:
    url = "https://www.buffalo.edu/campaign/impact/bold-stories.host.html/content/shared/ap/articles/news/2024/aiabuffalowny-designawards-2024.detail.html"
    source = c.record_source("architecture_design_credits", "UB article on AIA Buffalo/WNY 2024 Design Awards", url,
                             "One official University at Buffalo article describing named architecture student projects and award recipients.",
                             "Only directly named authors/recipients and project titles are emitted; professional firm project pages are not treated as individual credits.")
    result = c.fetch(url, "ub-aia-design-awards-2024")
    if not result:
        source["status"] = "blocked or budget exhausted"
        return
    payload, raw = result
    doc = c.parse_html(payload)
    text = doc.text
    works_before, credits_before = len(c.works), len(c.contributions)
    # Source prose explicitly pairs an award with student-authored work.
    person = r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]*(?:\s+[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]*){1,2}"
    pattern = re.compile(rf"(?:^|\n)\s*({person})\s+and\s+({person})\s+were presented with the 2024 Student Design Project of the Year by AIA Buffalo/WNY for\s+[\"“]([^\"”]+)[\"”]")
    for m in pattern.finditer(text):
        p1, p2, title = m.group(1), m.group(2), compact(m.group(3))
        work_date = "2024"
        regional = "The University at Buffalo article says the AIA Buffalo/WNY 2024 awards gala was held at the Admiral Room in downtown Buffalo."
        snippet = compact(text[max(0,m.start()-200):m.end()+420])
        wid = c.add_work(title=title, kind="architecture design", url=url, raw_path=raw,
                         identifier=f"student-project:{title}", work_date=work_date,
                         organization="AIA Buffalo/WNY; University at Buffalo School of Architecture and Planning",
                         regional=regional, description=snippet)
        for person in (p1, p2):
            c.add_credit(wid, person, "student design author", f"The UB article says {person} was presented with the 2024 Student Design Project of the Year award for {title}.", url)
    source["records"] = len(c.works) - works_before
    source["named_credits"] = len(c.contributions) - credits_before
    source["status"] = "fetched" if source["named_credits"] else "fetched; no named project pair parsed"
    if not source["named_credits"]:
        source["limitation"] += " The page was fetched, but the expected student-award sentence did not match; retained as a source attempt only."


def collect_community_report(c: Collector) -> None:
    url = "https://www.tpl.org/wp-content/uploads/2022/10/020122_Buffalo-Parks-Master-Plan_Final_singles.pdf"
    source = c.record_source("community_project_reports", "City of Buffalo Parks Master Plan", url,
                             "Full public plan PDF; extracted project staff names from the acknowledgments section using the installed pdftotext utility.",
                             "The source acknowledges 1,000+ community participants but individual participant names are not listed; they are not inferred or added.")
    result = c.fetch(url, "buffalo-parks-master-plan", binary=True)
    if not result:
        source["status"] = "blocked or budget exhausted"
        return
    payload, raw = result
    if not shutil.which("pdftotext"):
        source["status"] = "fetched; PDF text extraction unavailable"
        source["limitation"] += " pdftotext is not installed; the raw report is retained without extracting individual credits."
        return
    try:
        output = subprocess.run(["pdftotext", "-layout", str(raw), "-"], check=True, capture_output=True, text=True, timeout=60).stdout
    except Exception as exc:
        source["status"] = "fetched; PDF extraction failed"
        source["limitation"] += f" PDF extraction error: {type(exc).__name__}."
        return
    text = output.replace("\f", "\n")
    start = text.find("Project Staff")
    stop = text.find("Project Partners", start if start >= 0 else 0)
    if start < 0 or stop < 0:
        source["status"] = "fetched; project staff section not parsed"
        source["limitation"] += " Could not find the Project Staff section in extracted text."
        return
    staff = text[start:stop]
    wid = c.add_work(title="Parks Master Plan: City of Buffalo", kind="community planning report", url=url, raw_path=raw,
                     identifier="buffalo-parks-master-plan-final", work_date="2022",
                     organization="The Trust for Public Land; City of Buffalo Division of Parks and Recreation; New City Parks",
                     regional="The report is explicitly titled Parks Master Plan: City of Buffalo and describes the City of Buffalo park system and Western New York regional green space.",
                     description="The plan sets a shared vision for the City of Buffalo parks system and records community engagement, park access analysis, and implementation strategies.")
    role_heading = "project staff"
    for line in staff.splitlines():
        line = compact(line)
        if not line:
            continue
        if re.fullmatch(r"[A-Z][A-Z &'.-]{3,}", line) and not re.search(r"\b[A-Z]{2,3}\b", line):
            role_heading = line.title()
            continue
        # Report names are set in uppercase; retain the source spelling in evidence and normalize display case.
        m = re.match(r"^([A-Z][A-Z'-]+(?:\s+[A-Z][A-Z'-]+){1,3}),\s+(.+)$", line)
        if not m:
            continue
        raw_name, job = compact(m.group(1)).strip(" ,"), compact(m.group(2))
        if not is_person_name(raw_name.title()):
            continue
        name = raw_name.title()
        role = f"project staff — {role_heading}: {job[:140]}"
        c.add_credit(wid, name, role, f"Buffalo Parks Master Plan acknowledgments list {raw_name}, {job}, under {role_heading}.", url)
    source["records"] = 1 if wid else 0
    source["named_credits"] = sum(1 for credit in c.contributions if credit["work_id"] == wid)
    source["status"] = "fetched"
    source["coverage"] += f" Full response contains {len(payload):,} bytes; extracted {source['named_credits']} named project-staff credits."


def run(args: argparse.Namespace) -> int:
    c = Collector(Path(args.output).expanduser().resolve(), args.max_requests, args.max_seconds, args.offline)
    # Fast sources with exact names are first, so a short run still emits useful rows.
    collect_architecture(c)
    collect_community_report(c)
    collect_podcast(c, "https://feeds.buzzsprout.com/2524657.rss", "BMP (Buffalo Music Players) Podcast",
                    "The public feed describes this show as covering artists and organizers in greater Buffalo and Western New York.")
    collect_podcast(c, "https://omny.fm/shows/716-together/playlists/podcast.rss", "716 Together",
                    "The public show description states that 716 Together covers issues affecting Buffalo and Western New York.")
    collect_ub_exhibitions(c)
    collect_public_art(c)
    collect_squeaky(c)
    collect_cepa(c)
    collect_biff(c)

    c.output.mkdir(parents=True, exist_ok=True)
    with (c.output / "works.jsonl").open("w", encoding="utf-8") as f:
        for row in c.works:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with (c.output / "contributions.jsonl").open("w", encoding="utf-8") as f:
        for row in c.contributions:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    for source in c.sources:
        source["request_results"] = source.pop("request_results", [])
    manifest = {
        "lane": LANE, "started_at": c.started_at, "completed_at": now(), "requests": c.requests,
        "works": len(c.works), "contributions": len(c.contributions), "complete": False,
        "scope": "Creative public work with documented Buffalo/Western New York ties: one AIA Buffalo/WNY student design award article; UB and AKG gallery/public art pages; Squeaky Wheel events; BIFF 2025 film/panel listings; two current public podcast RSS feeds; and the City of Buffalo Parks Master Plan. No residence is inferred. Rochester/Finger Lakes expansion was not collected.",
        "family_status": [
            {"family": "architecture/design", "status": "collected", "detail": "One 2024 AIA Buffalo/WNY student design award article; portfolio/firm project catalogs were not collected."},
            {"family": "exhibitions/public art", "status": "collected", "detail": "UB Art Galleries current archive/detail pages, Buffalo AKG Public Art Initiative, and Squeaky Wheel historical event details; CEPA archive request returned 403."},
            {"family": "film", "status": "collected", "detail": "Buffalo International Film Festival 2025 official archive and up to 38 linked event pages; grouped shorts were emitted by exact director credit."},
            {"family": "game", "status": "attempted; group-only", "detail": "Squeaky Wheel's Amatryx Gaming Lab event page was fetched; the page gave a collective/event credit without a verified individual creator, so no person contribution was emitted."},
            {"family": "performance", "status": "not collected", "detail": "No separate performing-arts production/credits dataset was fetched; the BIFF listings are film and talk credits, not stage-performance credits."},
            {"family": "talks/workshops", "status": "collected; bounded", "detail": "Squeaky Wheel archived workshops/talks and BIFF 2025 panel roles were attempted; standalone conference schedules and a comprehensive regional talk calendar were not collected."},
            {"family": "podcasts", "status": "collected", "detail": "One public RSS snapshot each for BMP and 716 Together, retaining episode/date records and only verified individual guest names."},
            {"family": "community project reports", "status": "collected", "detail": "Full Buffalo Parks Master Plan PDF and its named project staff; the report does not name individual participants in its broader community engagement."},
            {"family": "Rochester/Finger Lakes", "status": "not collected", "detail": "No Rochester-specific creative-work source was attempted in this run; all emitted work uses Buffalo/WNY scope."}
        ],
        "sources": c.sources,
        "unresolved_names": list(c.unresolved.values()),
        "unresolved_name_count": len(c.unresolved),
        "limitations": [
            "Bounded collection; this is not a complete inventory of regional creative work.",
            "Only explicit named credits were emitted. Group/team credits without a named individual and alias-only podcast subjects remain unresolved.",
            "Current/future public listings can describe scheduled programs; descriptions retain their published date and venue without asserting attendance or completion.",
            "The Rochester/Finger Lakes source family remains uncollected in this run.",
            "No performing-arts production roster, standalone conference schedule, or comprehensive talk calendar was collected. Squeaky Wheel and BIFF provide only bounded workshop, artist-talk, and panel evidence.",
        ],
        "errors": c.errors,
    }
    (c.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(c.output), "requests": c.requests, "works": len(c.works), "contributions": len(c.contributions), "sources": [{"name": s["name"], "status": s["status"], "records": s["records"]} for s in c.sources]}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="output directory (default: ignored data/work-discovery/2026-09-28/creative_public_work)")
    parser.add_argument("--max-requests", type=int, default=140)
    parser.add_argument("--max-seconds", type=int, default=840)
    parser.add_argument("--offline", action="store_true", help="reuse exact raw responses already in --output and make no network requests")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
