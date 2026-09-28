#!/usr/bin/env python3
"""Collect public research works with explicit Buffalo/WNY or Rochester ties.

This lane combines a cached OpenAlex institutional-affiliation snapshot with a
bounded live refresh, public repository metadata, Zenodo records, and a small
set of official lab/project pages. It deliberately records only authors or
members whose regional credit is explicit on the cited work/page.
"""
from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPO = Path(__file__).resolve().parents[2]
CACHE_ROOT = REPO / "data/seeds/research"
DEFAULT_OUTPUT = REPO / "data/work-discovery/2026-09-28/research_work"
UA = "BuffaloCommonsResearch/1.0 (public scholarly-work metadata collection)"
API_OPENALEX = "https://api.openalex.org/works"
API_ZENODO = "https://zenodo.org/api/records"

LAB_PAGES = [
    {
        "url": "https://ubwp.buffalo.edu/birch/our-team/",
        "organization": "University at Buffalo",
        "scope": "buffalo_western_new_york",
        "kind": "research_group",
    },
    {
        "url": "https://dsroy.lab.medicine.buffalo.edu/team",
        "organization": "University at Buffalo",
        "scope": "buffalo_western_new_york",
        "kind": "research_group",
    },
    {
        "url": "https://adams.eng.buffalo.edu/team/",
        "organization": "University at Buffalo",
        "scope": "buffalo_western_new_york",
        "kind": "research_group",
    },
    {
        "url": "https://ubwp.buffalo.edu/does-research/",
        "organization": "University at Buffalo",
        "scope": "buffalo_western_new_york",
        "kind": "research_group",
    },
    {
        "url": "https://www.buffalo.edu/ai-data-science/research/projects.html",
        "organization": "University at Buffalo",
        "scope": "buffalo_western_new_york",
        "kind": "research_project_listing",
    },
    {
        "url": "https://www.urmc.rochester.edu/labs/by-department-center",
        "organization": "University of Rochester Medicine",
        "scope": "rochester_finger_lakes_expansion",
        "kind": "research_group_listing",
    },
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def clean_html(value: str) -> str:
    return compact_text(re.sub(r"<[^>]*>", " ", value or ""))


def stable_id(source: str, identifier: str) -> str:
    return f"{source}:{identifier}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Budget:
    def __init__(self, max_requests: int, max_seconds: int):
        self.max_requests = max_requests
        self.deadline = time.monotonic() + max_seconds
        self.requests = 0

    def allow(self) -> bool:
        return self.requests < self.max_requests and time.monotonic() < self.deadline


def fetch(url: str, budget: Budget, timeout: int = 25) -> tuple[bytes, int]:
    if not budget.allow():
        raise RuntimeError("network request/time budget reached")
    budget.requests += 1
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/html,application/xml,*/*"})
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.read(), int(response.status)
    except HTTPError as exc:
        return exc.read(), int(exc.code)
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"request failed: {type(exc).__name__}: {exc}") from exc


def save_raw(out: Path, relative: str, data: bytes) -> tuple[str, str]:
    path = out / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return relative, sha256(data)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                result.append(json.loads(line))
    return result


def load_institutions(cache_root: Path) -> tuple[dict[str, dict[str, str]], int]:
    records = load_jsonl(cache_root / "researchers/institutions.jsonl")
    institutions: dict[str, dict[str, str]] = {}
    for row in records:
        identifier = str(row.get("openalex_id", "")).rsplit("/", 1)[-1]
        if not identifier:
            continue
        institutions[identifier] = {
            "name": row.get("display_name", ""),
            "scope": row.get("geography_scope", "buffalo_western_new_york"),
        }
    if not institutions:
        raise RuntimeError(f"No known regional OpenAlex institution IDs in {cache_root}/researchers/institutions.jsonl")
    people = load_jsonl(cache_root / "researchers/people.jsonl")
    known_author_ids = {
        str(row.get("external_ids", {}).get("openalex") or row.get("profile_url", "")).rstrip("/").rsplit("/", 1)[-1]
        for row in people
        if row.get("external_ids", {}).get("openalex") or str(row.get("profile_url", "")).startswith("https://openalex.org/A")
    }
    return institutions, len(known_author_ids)


def abstract_from_index(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    words: list[str] = []
    for token, positions in index.items():
        if isinstance(positions, list):
            for position in positions:
                if isinstance(position, int) and position >= 0:
                    while len(words) <= position:
                        words.append("")
                    if not words[position]:
                        words[position] = token
    return compact_text(" ".join(words))


def is_non_person_author(name: str, institutions: dict[str, dict[str, str]]) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    known_organizations = {
        re.sub(r"[^a-z0-9]+", " ", value.get("name", "").casefold()).strip()
        for value in institutions.values()
        if value.get("name")
    }
    if normalized in known_organizations:
        return True
    return bool(re.search(r"\b(consortium|collaboration|research group|working group|study group|investigator group|task force|committee|network)\b", name, re.I))


def openalex_record(work: dict[str, Any], institutions: dict[str, dict[str, str]], raw_ref: tuple[str, str], observed: str,
                    source_origin: str, work_rows: dict[str, dict[str, Any]], contrib_rows: dict[tuple[str, str], dict[str, Any]],
                    unresolved: dict[tuple[str, str], dict[str, str]] | None = None) -> tuple[bool, int]:
    work_url = work.get("id", "")
    work_key = str(work_url).rstrip("/").rsplit("/", 1)[-1]
    if not work_key:
        return False, 0
    found: dict[str, dict[str, str]] = {}
    author_records: list[tuple[dict[str, Any], list[str]]] = []
    for authorship in work.get("authorships", []) or []:
        author = authorship.get("author") or {}
        author_url = author.get("id") or ""
        institution_ids = {
            str(org.get("id", "")).rstrip("/").rsplit("/", 1)[-1]
            for org in authorship.get("institutions", []) or []
            if org.get("id")
        }
        matched = sorted(institution_ids.intersection(institutions))
        if author_url and matched:
            author_records.append((authorship, matched))
            for identifier in matched:
                found[identifier] = institutions[identifier]
    if not found:
        return False, 0
    scope = "buffalo_western_new_york" if any(v["scope"] == "buffalo_western_new_york" for v in found.values()) else "rochester_finger_lakes_expansion"
    organization_names = sorted({v["name"] for v in found.values() if v.get("name")})
    title = compact_text(str(work.get("display_name") or work.get("title") or ""))
    if not title:
        return False, 0
    year = work.get("publication_year")
    work_date = work.get("publication_date") or (str(year) if year else "")
    description = abstract_from_index(work.get("abstract_inverted_index"))
    if description:
        description = description[:2000]
    else:
        kind = compact_text(str(work.get("type") or "publication"))
        description = f"OpenAlex indexes this {kind}{f' from {year}' if year else ''}."
    evidence = "OpenAlex records author-level institutional affiliation(s) on this work: " + "; ".join(organization_names) + "."
    work_id = stable_id("openalex", work_key)
    previous = work_rows.get(work_id)
    if not previous or (source_origin == "live" and previous.get("_source_origin") != "live"):
        work_rows[work_id] = {
            "id": work_id,
            "title": title,
            "kind": work.get("type") or "publication",
            "source_url": work_url,
            "observed_at": observed,
            "work_date": work_date,
            "organization": "; ".join(organization_names),
            "regional_evidence": evidence,
            "geography_scope": scope,
            "description": description,
            "raw_path": raw_ref[0],
            "raw_sha256": raw_ref[1],
            "_source_origin": source_origin,
        }
    added = 0
    for authorship, matched_ids in author_records:
        author = authorship.get("author") or {}
        author_url = str(author.get("id") or "")
        name = compact_text(str(author.get("display_name") or ""))
        if not name:
            continue
        if is_non_person_author(name, institutions):
            if unresolved is not None:
                unresolved[(work_id, name)] = {
                    "work_id": work_id,
                    "name": name,
                    "source_url": work_url,
                    "reason": "OpenAlex author entity matches a known institution or has an explicit group/consortium label; retained as unresolved rather than projected as a person.",
                }
            continue
        author_position = compact_text(str(authorship.get("author_position") or ""))
        role = "author" + (f" ({author_position})" if author_position and author_position not in {"middle"} else "")
        institutions_text = ", ".join(institutions[i]["name"] for i in matched_ids)
        key = (work_id, author_url)
        contribution = {
            "work_id": work_id,
            "name": name,
            "role": role,
            "person_url": author_url,
            "identifiers": {k: v for k, v in {"openalex": author_url, "orcid": author.get("orcid") or ""}.items() if v},
            "evidence": f"OpenAlex authorship lists {name} with {institutions_text} on this work.",
            "source_url": work_url,
        }
        if key not in contrib_rows:
            contrib_rows[key] = contribution
            added += 1
        else:
            existing = contrib_rows[key]
            if institutions_text not in existing["evidence"]:
                existing["evidence"] += f" Also lists {institutions_text}."
            if author_position and author_position != "middle" and "(" not in existing["role"]:
                existing["role"] = role
    return True, added


def openalex_live(out: Path, budget: Budget, institutions: dict[str, dict[str, str]], pages_per_slice: int,
                  work_rows: dict[str, dict[str, Any]], contrib_rows: dict[tuple[str, str], dict[str, Any]],
                  unresolved: dict[tuple[str, str], dict[str, str]], sources: list[dict[str, Any]], errors: list[str]) -> None:
    scopes = {
        "buffalo_western_new_york": [k for k, v in institutions.items() if v["scope"] == "buffalo_western_new_york"],
        "rochester_finger_lakes_expansion": [k for k, v in institutions.items() if v["scope"] == "rochester_finger_lakes_expansion"],
    }
    bands = [(2015, 2019), (2010, 2014)]
    page_counter = 0
    for scope, ids in scopes.items():
        if not ids:
            continue
        ids_filter = "|".join(ids)
        for first, last in bands:
            cursor = "*"
            accepted = 0
            contributions = 0
            fetched = 0
            attempted = 0
            for page in range(pages_per_slice):
                if not budget.allow():
                    break
                params = {
                    "filter": f"authorships.institutions.id:{ids_filter},publication_year:{first}-{last}",
                    "sort": "publication_date:desc",
                    "per-page": 200,
                    "cursor": cursor,
                    "select": "id,display_name,type,publication_date,publication_year,authorships,abstract_inverted_index",
                }
                url = API_OPENALEX + "?" + urlencode(params)
                try:
                    body, status = fetch(url, budget)
                    attempted += 1
                    page_counter += 1
                    raw_ref = save_raw(out, f"raw/openalex-live-{page_counter:03d}.json", body)
                    if status != 200:
                        raise RuntimeError(f"OpenAlex HTTP {status}")
                    response = json.loads(body)
                    rows = response.get("results", [])
                    for item in rows:
                        matched, contribution_count = openalex_record(item, institutions, raw_ref, now(), "live", work_rows, contrib_rows, unresolved)
                        accepted += int(matched)
                        contributions += contribution_count
                    fetched += len(rows)
                    next_cursor = (response.get("meta") or {}).get("next_cursor")
                    if not rows or not next_cursor or next_cursor == cursor:
                        break
                    cursor = next_cursor
                except Exception as exc:
                    errors.append(f"OpenAlex {scope} {first}-{last}: {exc}")
                    break
            sources.append({
                "url": API_OPENALEX,
                "status": "fetched" if attempted else "budget_limited" if not errors else "failed",
                "records": accepted,
                "contributions": contributions,
                "record_unit": "works",
                "coverage": f"{scope}; publication years {first}-{last}; fetched {fetched} result rows in {attempted} newest-sorted 200-work cursor page(s) of up to {pages_per_slice} requested.",
                "limitation": "Sampled page ceiling; records kept only when an individual authorship explicitly lists a known regional institution ID.",
            })


def cached_openalex(out: Path, institutions: dict[str, dict[str, str]], cache_root: Path,
                    work_rows: dict[str, dict[str, Any]], contrib_rows: dict[tuple[str, str], dict[str, Any]],
                    unresolved: dict[tuple[str, str], dict[str, str]], sources: list[dict[str, Any]], errors: list[str]) -> None:
    path = cache_root / "researchers/raw/works-responses.jsonl"
    if not path.exists():
        sources.append({"url": "https://api.openalex.org/works", "status": "unavailable", "records": 0, "coverage": "No cached OpenAlex works file was present.", "limitation": ""})
        return
    count = 0
    cached_work_ids: set[str] = set()
    contribution_count = len(contrib_rows)
    work_raw_pages = 0
    observed_dates: set[str] = set()
    for line_no, line in enumerate(path.read_bytes().splitlines(), 1):
        if not line.strip():
            continue
        try:
            envelope = json.loads(line)
            response = envelope.get("response") or {}
            fetched_at = envelope.get("fetched_at")
            raw_rel = f"raw/cached-openalex-page-{line_no:03d}.jsonl"
            raw_ref = save_raw(out, raw_rel, line + b"\n")
            work_raw_pages += 1
            if not isinstance(fetched_at, str) or datetime.fromisoformat(fetched_at.replace('Z', '+00:00')).tzinfo is None:
                raise ValueError('Cached response requires its original timezone-aware fetched_at')
            observed_dates.add(fetched_at[:10])
            page_added = 0
            for work in response.get("results", []) or []:
                matched, contribution_added = openalex_record(work, institutions, raw_ref, fetched_at, "cache", work_rows, contrib_rows, unresolved)
                page_added += contribution_added
                if matched and work.get("id"):
                    cached_work_ids.add(stable_id("openalex", str(work["id"]).rstrip("/").rsplit("/", 1)[-1]))
            count = len(cached_work_ids)
        except Exception as exc:
            errors.append(f"cached OpenAlex line {line_no}: {exc}")
    sources.append({
        "url": "https://api.openalex.org/works",
        "status": "cached_snapshot_reused",
        "records": count,
        "contributions": len(contrib_rows) - contribution_count,
        "record_unit": "works",
        "coverage": f"Read {work_raw_pages} cached API response snapshots; accepted observation dates: {', '.join(sorted(observed_dates)) or 'none'}. Original query bounds remain in the archived envelopes.",
        "limitation": "Cached response envelopes are preserved as archived JSONL snapshots. Missing or invalid original observation times are rejected, not replaced with the current time.",
    })


def zenodo(out: Path, budget: Budget, institutions: dict[str, dict[str, str]], pages_per_query: int,
           work_rows: dict[str, dict[str, Any]], contrib_rows: dict[tuple[str, str], dict[str, Any]], sources: list[dict[str, Any]], errors: list[str]) -> None:
    targets = [
        ("University at Buffalo", "buffalo_western_new_york"),
        ("Roswell Park Comprehensive Cancer Center", "buffalo_western_new_york"),
        ("Rochester Institute of Technology", "rochester_finger_lakes_expansion"),
        ("University of Rochester", "rochester_finger_lakes_expansion"),
    ]
    raw_i = 0
    for affiliation, scope in targets:
        accepted_works: set[str] = set()
        accepted_contributions = 0
        seen_pages = 0
        for page in range(1, pages_per_query + 1):
            if not budget.allow():
                break
            q = f'creators.affiliation:"{affiliation}"'
            url = API_ZENODO + "?" + urlencode({"q": q, "size": 25, "page": page, "sort": "-mostrecent", "all_versions": "false"})
            try:
                body, status = fetch(url, budget)
                raw_i += 1
                seen_pages += 1
                raw_ref = save_raw(out, f"raw/zenodo-{raw_i:03d}.json", body)
                if status != 200:
                    raise RuntimeError(f"Zenodo HTTP {status}")
                response = json.loads(body)
                hits = (response.get("hits") or {}).get("hits") or []
                for record in hits:
                    metadata = record.get("metadata") or {}
                    creators = metadata.get("creators") or []
                    regional_creators = []
                    for creator in creators:
                        creator_aff = compact_text(str(creator.get("affiliation") or ""))
                        if creator_aff and affiliation.casefold() in creator_aff.casefold():
                            regional_creators.append((creator, creator_aff))
                    if not regional_creators:
                        continue
                    rec_id = str(record.get("id") or (record.get("links") or {}).get("self") or "")
                    title = compact_text(str(metadata.get("title") or ""))
                    if not rec_id or not title:
                        continue
                    rec_url = (record.get("links") or {}).get("html") or f"https://zenodo.org/records/{rec_id}"
                    resource_type = metadata.get("resource_type") or {}
                    kind = resource_type.get("title") or resource_type.get("type") or "Zenodo record"
                    date = metadata.get("publication_date") or ""
                    description = clean_html(str(metadata.get("description") or ""))[:2000]
                    names = [compact_text(str(c.get("name") or "")) for c, _ in regional_creators if c.get("name")]
                    evidence = "Zenodo creator metadata lists this affiliation: " + affiliation + "; named regional creator(s): " + ", ".join(names) + "."
                    work_id = stable_id("zenodo", rec_id)
                    accepted_works.add(work_id)
                    work_rows.setdefault(work_id, {
                        "id": work_id,
                        "title": title,
                        "kind": kind,
                        "source_url": rec_url,
                        "observed_at": now(),
                        "work_date": date,
                        "organization": affiliation,
                        "regional_evidence": evidence,
                        "geography_scope": scope,
                        "description": description,
                        "raw_path": raw_ref[0],
                        "raw_sha256": raw_ref[1],
                        "_source_origin": "live",
                    })
                    for creator, creator_aff in regional_creators:
                        name = compact_text(str(creator.get("name") or ""))
                        if not name:
                            continue
                        orcid = compact_text(str(creator.get("orcid") or ""))
                        person_url = f"https://orcid.org/{orcid}" if orcid else ""
                        key = (work_id, person_url or name)
                        if key not in contrib_rows:
                            accepted_contributions += 1
                        contrib_rows.setdefault(key, {
                            "work_id": work_id,
                            "name": name,
                            "role": "creator",
                            "person_url": person_url,
                            "identifiers": {"orcid": person_url} if person_url else {},
                            "evidence": f"Zenodo lists {name} as a creator with affiliation {creator_aff}.",
                            "source_url": rec_url,
                        })

                if not hits:
                    break
            except Exception as exc:
                errors.append(f"Zenodo affiliation {affiliation}: {exc}")
                break
        sources.append({
            "url": API_ZENODO,
            "status": "fetched" if seen_pages else "budget_limited" if not budget.allow() else "failed",
            "records": len(accepted_works),
            "contributions": accepted_contributions,
            "record_unit": "works",
            "coverage": f'Zenodo search creators.affiliation:"{affiliation}"; fetched {seen_pages} most-recent result page(s), 25 records per page.',
            "limitation": "Only creators with an explicit matching institutional affiliation were retained; query is a sampled API search and can miss differently worded affiliations.",
        })


class RitParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.current_tag = ""
        self.current_attrs: dict[str, str] = {}
        self.capture: list[str] | None = None
        self.capture_attrs: dict[str, str] = {}
        self.in_anchor = False
        self.title_parts: list[str] = []
        self.author_parts: list[str] = []
        self.year = ""
        self.records: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "h3" and values.get("id", "").startswith("year_"):
            self.year = values["id"].split("_", 1)[1]
        if tag == "p" and "article-listing" in values.get("class", ""):
            self.capture = []
            self.capture_attrs = {}
            self.title_parts = []
            self.author_parts = []
        if self.capture is not None and tag == "a" and not self.capture_attrs.get("href"):
            self.capture_attrs = values
            self.in_anchor = True

    def handle_data(self, data: str) -> None:
        if self.capture is not None:
            self.capture.append(data)
            (self.title_parts if self.in_anchor else self.author_parts).append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self.capture is not None:
            href = self.capture_attrs.get("href", "")
            title = compact_text(" ".join(self.title_parts))
            authors = compact_text(" ".join(self.author_parts)).lstrip(", ")
            if href and title and authors:
                self.records.append({"href": href, "title": title, "authors": authors, "year": self.year})
            self.capture = None
            self.capture_attrs = {}
            self.in_anchor = False
            self.title_parts = []
            self.author_parts = []
        elif tag == "a":
            self.in_anchor = False


class TextBlocks(HTMLParser):
    """Small HTML block reader for official lab pages; no external parser required."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self.tag = ""
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag in {"h1", "h2", "h3", "h4", "h5", "p", "li"}:
            self.tag = tag
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.tag:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if self.tag == tag:
            text = compact_text(" ".join(self.parts))
            if text:
                self.blocks.append((tag, text))
            self.tag = ""
            self.parts = []


def name_like(text: str) -> bool:
    text = compact_text(text).strip(" ,;:")
    if not text or len(text) > 100:
        return False
    low = text.casefold()
    excluded = {"team", "our team", "current members", "lab members", "lab alumni", "alumni", "students", "current students", "ph.d. students", "meet the current ph.d. students", "principal investigator", "research", "projects", "research project results", "research projects", "current members", "member profiles", "about"}
    if low in excluded or low.startswith(("meet the ", "research project", "current ", "former ", "our ")):
        return False
    words = re.findall(r"[A-Za-zÀ-ž][A-Za-zÀ-ž'’.-]*", text)
    role_words = {"student", "students", "scientist", "scientists", "researcher", "researchers", "specialist", "specialists", "coordinator", "coordinators", "professor", "professors", "faculty", "team", "group", "lab", "member", "members", "filter", "postdoctoral", "postdoc", "fellow", "fellows", "clinician", "clinicians", "intern", "interns", "director", "investigator", "instructor", "graduate", "undergraduate", "alumni", "assistant", "associate", "program", "administrator", "psychotherapy", "research", "project", "projects", "center", "centre", "department", "division", "hospital", "medical", "psychology", "clinic", "university", "college", "institute", "school", "company", "corporation", "organization", "institute"}
    if any(w.casefold().replace(".", "") in role_words for w in words):
        return False
    return 2 <= len(words) <= 7 and sum(1 for w in words if w[:1].isupper()) >= 2


def heading_names(text: str) -> list[str]:
    """Split a lab roster heading only at visible vertical-bar separators."""
    candidates = [compact_text(part).strip(" ,;:") for part in text.split("|")]
    candidates = [re.sub(r"\s*\(Class of \d{4}\)$", "", value) for value in candidates]
    return [candidate for candidate in candidates if name_like(candidate)]


def lab_contributions(blocks: list[tuple[str, str]], work_id: str, page_url: str, scope: str,
                      contrib_rows: dict[tuple[str, str], dict[str, Any]]) -> int:
    section = False
    section_role = "lab member"
    added = 0
    section_patterns = ("team", "member", "alum", "people", "personnel", "student", "research assistant", "director")
    stop_patterns = ("research", "project", "contact", "about", "publication", "join", "news", "funding", "program")
    for tag, value in blocks:
        low = value.casefold()
        if tag in {"h1", "h2", "h3", "h4", "h5"}:
            member_heading = any(term in low for term in section_patterns)
            if member_heading:
                section = True
                if any(term in low for term in ("alumni", "alum", "former", "previous", "past")):
                    section_role = "former lab member"
                elif "current" in low:
                    section_role = "current lab member"
                elif not section_role or not section:
                    section_role = "lab member"
                elif any(term in low for term in ("team", "people", "director")):
                    section_role = "lab member"
            elif any(term in low for term in stop_patterns):
                section = False
        if not section or tag not in {"h2", "h3", "h4", "h5"} or not heading_names(value):
            continue
        for name in heading_names(value):
            key = (work_id, name)
            if key not in contrib_rows:
                contrib_rows[key] = {
                    "work_id": work_id,
                    "name": name,
                    "role": section_role,
                    "person_url": "",
                    "identifiers": {},
                    "evidence": f"Official lab page lists {name} as {section_role}.",
                    "source_url": page_url,
                }
                added += 1
    return added


def repositories(out: Path, budget: Budget, work_rows: dict[str, dict[str, Any]],
                 contrib_rows: dict[tuple[str, str], dict[str, Any]], sources: list[dict[str, Any]], errors: list[str],
                 ur_pages: int, unresolved: dict[tuple[str, str], dict[str, str]]) -> None:
    # Rochester's institutional repository has a public, paginated Chemistry Ph.D. thesis collection.
    ur_records = 0
    ur_pages_fetched = 0
    for page in range(1, min(ur_pages, 3) + 1):
        if not budget.allow():
            break
        params = {
            "collectionId": "185", "contentTypeId": "-1", "currentPageNumber": str(page),
            "rowStart": str((page - 1) * 25), "selectedAlpha": "All", "sortElement": "publicationDate",
            "sortType": "desc", "startPageNumber": "1",
        }
        url = "https://urresearch.rochester.edu/browseCollectionItems.action?" + urlencode(params)
        try:
            body, status = fetch(url, budget)
            ur_pages_fetched += 1
            raw_ref = save_raw(out, f"raw/urrr-chemistry-theses-{page:02d}.html", body)
            if status != 200:
                raise RuntimeError(f"UR Research HTTP {status}")
            text = body.decode("utf-8", "replace")
            for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", text, flags=re.I | re.S):
                item = re.search(r'href="([^"]*institutionalPublicationPublicView\.action\?institutionalItemId=(\d+)[^"]*)"[^>]*>(.*?)</a>', row_html, flags=re.I | re.S)
                if not item:
                    continue
                title = clean_html(item.group(3))
                item_id = item.group(2)
                cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, flags=re.I | re.S)
                year = ""
                abstract = ""
                people: list[tuple[str, str, str]] = []
                if len(cells) >= 5:
                    abstract = clean_html(cells[1])
                    year = clean_html(cells[2])
                    for match in re.finditer(r'<a href="([^"]*viewContributorPage\.action\?[^\"]*)"[^>]*>(.*?)</a>\s*-\s*([^<]+)', cells[4], flags=re.I | re.S):
                        name = clean_html(match.group(2))
                        role = compact_text(match.group(3))
                        if name:
                            person_url = match.group(1)
                            if not person_url.startswith("https://"):
                                person_url = "https://urresearch.rochester.edu" + person_url
                            people.append((name, role, person_url))
                if not title:
                    continue
                source_url = "https://urresearch.rochester.edu" + item.group(1)
                work_id = stable_id("urresearch", item_id)
                work_rows.setdefault(work_id, {
                    "id": work_id,
                    "title": title,
                    "kind": "thesis",
                    "source_url": source_url,
                    "observed_at": now(),
                    "work_date": year if re.fullmatch(r"\d{4}", year) else "",
                    "organization": "University of Rochester",
                    "regional_evidence": "UR Research identifies this item as a University of Rochester Chemistry Department Ph.D. thesis.",
                    "geography_scope": "rochester_finger_lakes_expansion",
                    "description": abstract[:1500],
                    "raw_path": raw_ref[0],
                    "raw_sha256": raw_ref[1],
                    "_source_origin": "live",
                })
                for name, role, person_url in people:
                    contribution_key = (work_id, name + "|" + role)
                    contrib_rows.setdefault(contribution_key, {
                        "work_id": work_id,
                        "name": name,
                        "role": role,
                        "person_url": person_url,
                        "identifiers": {},
                        "evidence": f"UR Research metadata credits {name} as {role} on this thesis.",
                        "source_url": source_url,
                    })
                ur_records += 1
        except Exception as exc:
            errors.append(f"UR Research thesis page {page}: {exc}")
            break
    sources.append({
        "url": "https://urresearch.rochester.edu/browseCollectionItems.action?collectionId=185",
        "status": "fetched" if ur_pages_fetched else "budget_limited" if not budget.allow() else "failed",
        "records": ur_records,
        "contributions": sum(1 for row in contrib_rows.values() if row["source_url"].startswith("https://urresearch.rochester.edu/institutionalPublicationPublicView.action")),
        "record_unit": "works",
        "coverage": f"Chemistry Ph.D. Theses collection 185; fetched the first {ur_pages_fetched} page(s), 25 records per page, sorted by publication date descending.",
        "limitation": "One academic collection only; URRR metadata describes this collection's domain coverage, not all University of Rochester theses.",
    })

    # RIT's repository lists thesis/capstone title, creator, and year on its public browse page.
    rit_url = "https://repository.rit.edu/theses/"
    rit_records = 0
    if budget.allow():
        try:
            body, status = fetch(rit_url, budget)
            raw_ref = save_raw(out, "raw/rit-theses-index.html", body)
            if status != 200:
                raise RuntimeError(f"RIT repository HTTP {status}")
            parser = RitParser()
            parser.feed(body.decode("utf-8", "replace"))
            for row in parser.records:
                item_id_match = re.search(r"/theses/(\d+)", row.get("href", ""))
                if not item_id_match or not row.get("title"):
                    continue
                item_id = item_id_match.group(1)
                title = row["title"]
                author_text = compact_text(row.get("authors", ""))
                if ";" in author_text or re.search(r"\s+(?:and|&)\s+", author_text, re.I):
                    authors = [compact_text(v) for v in re.split(r"\s+(?:and|&)\s+|\s*;\s*", author_text) if compact_text(v)]
                elif "," in author_text:
                    unresolved[(stable_id("rit", item_id), author_text)] = {
                        "work_id": stable_id("rit", item_id),
                        "name": author_text,
                        "source_url": row["href"],
                        "reason": "RIT browse text contains comma-separated creator text whose individual name boundaries are ambiguous; preserved unresolved instead of splitting by guesswork.",
                    }
                    authors = []
                else:
                    authors = [author_text] if author_text else []
                work_id = stable_id("rit", item_id)
                source_url = row["href"] if row["href"].startswith("https://") else "https://repository.rit.edu" + row["href"]
                work_rows.setdefault(work_id, {
                    "id": work_id,
                    "title": title,
                    "kind": "thesis_or_student_project",
                    "source_url": source_url,
                    "observed_at": now(),
                    "work_date": row.get("year", ""),
                    "organization": "Rochester Institute of Technology",
                    "regional_evidence": "The Rochester Institute of Technology Digital Institutional Repository lists this item in its theses and dissertations collection.",
                    "geography_scope": "rochester_finger_lakes_expansion",
                    "description": "Public RIT repository listing classifies the item as a thesis, dissertation, capstone, or master's project.",
                    "raw_path": raw_ref[0],
                    "raw_sha256": raw_ref[1],
                    "_source_origin": "live",
                })
                for name in authors:
                    if not name:
                        continue
                    contrib_rows.setdefault((work_id, name), {
                        "work_id": work_id,
                        "name": name,
                        "role": "thesis author",
                        "person_url": "",
                        "identifiers": {},
                        "evidence": f"RIT thesis listing credits {name} as author of this item.",
                        "source_url": source_url,
                    })
                rit_records += 1
            sources.append({"url": rit_url, "status": "fetched", "records": rit_records,
                            "contributions": sum(1 for row in contrib_rows.values() if row["source_url"].startswith("https://repository.rit.edu/theses/")),
                            "record_unit": "works",
                            "coverage": f"Public RIT theses browse page; parsed {rit_records} displayed records across the years shown on this page.",
                            "limitation": "One browse page only; the repository states that its digital holdings do not cover every past thesis."})
        except Exception as exc:
            errors.append(f"RIT thesis repository: {exc}")
            sources.append({"url": rit_url, "status": "failed", "records": 0, "coverage": "One repository browse page attempted.", "limitation": str(exc)})

    # UBIR is tried directly and retained as a source outcome even when its security edge blocks the API.
    ubir_url = "https://ubir.buffalo.edu/xmlui/oai/request?verb=Identify"
    if budget.allow():
        try:
            body, status = fetch(ubir_url, budget)
            raw_ref = save_raw(out, "raw/ubir-oai-identify-response.bin", body)
            sample = body[:1500].decode("utf-8", "replace").casefold()
            blocked = b"bobcmn" in body or b"tspd" in body.lower() or b"challenge" in body.lower()
            sources.append({
                "url": ubir_url,
                "status": "blocked_by_source_edge" if blocked else "fetched_no_records",
                "records": 0,
                "contributions": 0,
                "record_unit": "works",
                "coverage": f"OAI Identify request returned HTTP {status}; preserved {len(body)} response bytes at {raw_ref[0]}.",
                "limitation": "Response was a perimeter challenge HTML page, not repository OAI metadata." if blocked else f"No item records are returned by Identify; response starts {compact_text(sample[:180])!r}.",
            })
        except Exception as exc:
            errors.append(f"UBIR OAI: {exc}")
            sources.append({"url": ubir_url, "status": "failed", "records": 0, "coverage": "OAI Identify attempted once.", "limitation": str(exc)})


def lab_pages(out: Path, budget: Budget, work_rows: dict[str, dict[str, Any]],
              contrib_rows: dict[tuple[str, str], dict[str, Any]], sources: list[dict[str, Any]], errors: list[str]) -> None:
    for index, config in enumerate(LAB_PAGES, 1):
        url = config["url"]
        try:
            body, status = fetch(url, budget)
            raw_ref = save_raw(out, f"raw/lab-page-{index:02d}.html", body)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            parser = TextBlocks()
            parser.feed(body.decode("utf-8", "replace"))
            title = next((text for tag, text in parser.blocks if tag == "h1" and "privacy" not in text.casefold()), "") or compact_text(" ".join(parser.title_parts)) or config["organization"]
            paragraphs = [text for tag, text in parser.blocks if tag == "p" and len(text) > 45]
            description = " ".join(paragraphs[:3])[:1800]
            work_id = stable_id("labpage", hashlib.sha256(url.encode()).hexdigest()[:16])
            regional_phrase = "The page is published under the official " + config["organization"] + " research domain."
            work_rows.setdefault(work_id, {
                "id": work_id,
                "title": title,
                "kind": config["kind"],
                "source_url": url,
                "observed_at": now(),
                "work_date": "",
                "organization": config["organization"],
                "regional_evidence": regional_phrase,
                "geography_scope": config["scope"],
                "description": description,
                "raw_path": raw_ref[0],
                "raw_sha256": raw_ref[1],
                "_source_origin": "live",
            })
            member_count = lab_contributions(parser.blocks, work_id, url, config["scope"], contrib_rows)
            sources.append({
                "url": url,
                "status": "fetched",
                "records": 1,
                "contributions": member_count,
                "record_unit": "works",
                "coverage": f"Fetched one official lab/project page; extracted {member_count} named member contribution(s) where a membership section and name heading were explicit.",
                "limitation": "This is a page sample, not an inventory of all labs or all current/former members; group membership reflects the page wording at observation time.",
            })
        except Exception as exc:
            errors.append(f"lab/project page {url}: {exc}")
            sources.append({"url": url, "status": "failed", "records": 0, "coverage": "One public page attempted.", "limitation": str(exc)})


def write_outputs(out: Path, work_rows: dict[str, dict[str, Any]], contrib_rows: dict[tuple[str, str], dict[str, Any]],
                  manifest: dict[str, Any]) -> None:
    for source in manifest.get("sources", []):
        source.setdefault("contributions", 0)
        source.setdefault("record_unit", "works")
    works_path = out / "works.jsonl"
    contributions_path = out / "contributions.jsonl"
    for path in (works_path, contributions_path):
        if path.exists():
            path.unlink()
    for item in sorted(work_rows.values(), key=lambda row: row["id"]):
        item = {k: v for k, v in item.items() if not k.startswith("_")}
        append_jsonl(works_path, item)
    for item in sorted(contrib_rows.values(), key=lambda row: (row["work_id"], row["name"], row["role"])):
        append_jsonl(contributions_path, item)
    manifest["works"] = len(work_rows)
    manifest["contributions"] = len(contrib_rows)
    manifest["completed_at"] = now()
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="ignored lane output directory")
    parser.add_argument("--cache-root", type=Path, default=CACHE_ROOT, help="prior public regional profile/OpenAlex cache")
    parser.add_argument("--max-requests", type=int, default=150)
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--openalex-pages", type=int, default=4, help="maximum 200-work pages per regional/date slice")
    parser.add_argument("--zenodo-pages", type=int, default=2, help="maximum 25-record pages per affiliation query")
    parser.add_argument("--ur-thesis-pages", type=int, default=3, help="UR Research Chemistry Ph.D. thesis pages (25 each)")
    args = parser.parse_args()
    started = now()
    if args.output.exists() and any(args.output.iterdir()):
        raise SystemExit('Output directory is not empty; choose a new private run directory.')
    args.output.mkdir(parents=True, exist_ok=True)
    institutions, known_profiles = load_institutions(args.cache_root)
    budget = Budget(args.max_requests, args.max_seconds)
    work_rows: dict[str, dict[str, Any]] = {}
    contrib_rows: dict[tuple[str, str], dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    errors: list[str] = []
    unresolved: dict[tuple[str, str], dict[str, str]] = {}
    cached_openalex(args.output, institutions, args.cache_root, work_rows, contrib_rows, unresolved, sources, errors)
    openalex_live(args.output, budget, institutions, max(1, args.openalex_pages), work_rows, contrib_rows, unresolved, sources, errors)
    repositories(args.output, budget, work_rows, contrib_rows, sources, errors, args.ur_thesis_pages, unresolved)
    zenodo(args.output, budget, institutions, max(1, args.zenodo_pages), work_rows, contrib_rows, sources, errors)
    lab_pages(args.output, budget, work_rows, contrib_rows, sources, errors)
    manifest = {
        "lane": "research_work",
        "started_at": started,
        "completed_at": "",
        "requests": budget.requests,
        "works": 0,
        "contributions": 0,
        "complete": False,
        "scope": "Public research publications, lab/project pages, theses/dissertations, and Zenodo datasets/tools tied by explicit professional work evidence to Buffalo/Western New York; Rochester/Finger Lakes records are labeled separately.",
        "sources": sources,
        "limitations": [
            "Collection is bounded and incomplete; OpenAlex pages are sampled by regional institutions and publication year, while institutional repositories and lab pages cover selected public collections/pages.",
            "OpenAlex contributors are retained only when their own authorship on a work lists a known regional institution. Zenodo contributors are retained only when their own creator metadata lists the matching affiliation.",
            "Cached OpenAlex query bounds and original observation dates are retained in the response envelopes; cached coverage is not a live refresh.",
            "UBIR OAI metadata could not be harvested because the endpoint returned a perimeter challenge; Rochester thesis coverage is limited to one Chemistry Ph.D. collection, and RIT coverage to its current browse page.",
        ],
        "errors": errors,
        "unresolved_names": list(unresolved.values()),
        "unresolved_name_count": len(unresolved),
        "inputs": {"known_regional_institutions": len(institutions), "existing_openalex_profiles": known_profiles},
    }
    manifest["requests"] = budget.requests
    write_outputs(args.output, work_rows, contrib_rows, manifest)
    print(json.dumps({"output": str(args.output), "requests": budget.requests, "works": len(work_rows), "contributions": len(contrib_rows), "errors": len(errors)}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"research_work collection failed: {exc}", file=sys.stderr)
        raise
