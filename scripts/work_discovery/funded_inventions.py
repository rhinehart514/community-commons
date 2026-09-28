#!/usr/bin/env python3
"""Collect public NIH, NSF, SBIR/STTR, and USPTO PatentsView work records.

The collector uses only agency/public bulk files and APIs. Its geography is
based on the funding recipient or performance location recorded by the source;
it does not infer anyone's residence from a project credit.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote_plus
from urllib.request import Request, urlopen


REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "data/work-discovery/2026-09-28/funded_inventions"
USER_AGENT = "Buffalo-Projects-work-discovery/1.0 (public research collection)"

GEOGRAPHIES = {
    "Buffalo / Western New York": {
        "cities": [
            "ALBION", "ALLEGANY", "AMHERST", "ATTICA", "BATAVIA", "BUFFALO",
            "CATTARAUGUS", "CHEEKTOWAGA", "DEPEW", "DUNKIRK", "EAST AURORA",
            "EAST AMHERST", "ELLICOTTVILLE", "FREDONIA", "HAMBURG", "JAMESTOWN",
            "KENMORE", "LANCASTER", "LEWISTON", "LOCKPORT", "MEDINA", "NIAGARA FALLS",
            "NORTH TONAWANDA", "OLEAN", "ORCHARD PARK", "PORTVILLE", "SALAMANCA",
            "TONAWANDA", "WELLSVILLE", "WEST SENECA", "WILLIAMSVILLE", "WARSAW",
        ],
        "nsf_cities": [
            "Buffalo", "Amherst", "Fredonia", "Batavia", "Olean", "Jamestown",
            "Niagara Falls", "Lockport", "East Aurora", "Alfred", "Wellsville",
        ],
    },
    "Rochester / Finger Lakes": {
        "cities": [
            "AUBURN", "BROCKPORT", "CANANDAIGUA", "DANSVILLE", "FAIRPORT", "GENEVA",
            "GENESEO", "GREECE", "HENRIETTA", "LIMA", "MOUNT MORRIS", "NEWARK",
            "PENN YAN", "PENFIELD", "PITTSFORD", "ROCHESTER", "SENECA FALLS",
            "SODUS", "VICTOR", "WATERLOO", "WEBSTER",
        ],
        "nsf_cities": [
            "Rochester", "Henrietta", "Geneseo", "Brockport", "Canandaigua", "Geneva",
            "Fairport", "Auburn", "Penn Yan", "Victor", "Webster",
        ],
    },
}

NIH_URL = "https://api.reporter.nih.gov/v2/projects/search"
NSF_URL = "https://api.nsf.gov/services/v1/awards.json"
SBIR_CSV_URL = "https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/award_data_no_abstract.csv"
USPTO_PRODUCTS_SEARCH = "https://api.uspto.gov/api/v1/datasets/products/search?productTitle=PatentsView"
LEGACY_PATENT_BULK = "https://s3.amazonaws.com/data.patentsview.org/download/g_inventor_disambiguated.tsv.zip"
USPTO_PATENTVIEW_HUB = "https://www.uspto.gov/ip-policy/economic-research/patentsview"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return str(value).strip()


def iso_date(value: Any) -> str:
    """Normalize common source dates to YYYY-MM-DD; preserve unknowns as empty."""
    value = clean(value)
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for pattern in ("%m/%d/%Y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return ""


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


class Collector:
    def __init__(self, out: Path, max_requests: int, max_seconds: int):
        self.out = out
        self.raw = out / "raw"
        self.out.mkdir(parents=True, exist_ok=True)
        self.raw.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.started_at = now()
        self.max_requests = max_requests
        self.max_seconds = max_seconds
        self.reuse_existing = False
        self.requests = 0
        self.previous_requests = 0
        self.works: dict[str, dict[str, Any]] = {}
        self.contributions: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.sources: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.limitations: list[str] = []
        self.last_nih_request = 0.0

    def can_request(self) -> bool:
        return self.requests < self.max_requests and time.monotonic() - self.started < self.max_seconds

    def remaining(self) -> float:
        return max(0.0, self.max_seconds - (time.monotonic() - self.started))

    def save_raw(self, filename: str, data: bytes) -> tuple[str, str]:
        path = self.raw / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path.relative_to(self.out)), hashlib.sha256(data).hexdigest()

    def request_bytes(self, url: str, *, data: bytes | None = None,
                      headers: dict[str, str] | None = None,
                      raw_name: str | None = None, timeout: int = 45,
                      nih_throttle: bool = False) -> tuple[bytes | None, int | str, str | None, str | None, str | None]:
        if not self.can_request():
            return None, "not-run", None, None, "request/time budget exhausted"
        if nih_throttle:
            wait = 1.05 - (time.monotonic() - self.last_nih_request)
            if self.last_nih_request and wait > 0:
                time.sleep(min(wait, self.remaining()))
            self.last_nih_request = time.monotonic()
        self.requests += 1
        request = Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            with urlopen(request, timeout=min(timeout, max(2, int(self.remaining()) or 2))) as response:
                body = response.read()
                status = response.status
                final_url = response.geturl()
        except HTTPError as exc:
            body = exc.read()
            status = exc.code
            final_url = exc.geturl()
            raw_path, digest = self.save_raw(raw_name, body) if raw_name else (None, None)
            return None, status, raw_path, digest, f"HTTP {status} for {url}"
        except (URLError, TimeoutError, OSError) as exc:
            return None, "error", None, None, f"{type(exc).__name__} for {url}: {exc}"
        raw_path, digest = self.save_raw(raw_name, body) if raw_name else (None, None)
        return body, status, raw_path, digest, None

    def add_work(self, record: dict[str, Any]) -> None:
        self.works[record["id"]] = record

    def add_contribution(self, work_id: str, name: str, role: str, source_url: str,
                         evidence: str, identifiers: dict[str, Any] | None = None,
                         person_url: str = "") -> None:
        name = clean(name)
        if not name or name.casefold() in {"unknown", "not specified", "n/a", "none"}:
            return
        key = (work_id, name.casefold(), role)
        self.contributions[key] = {
            "work_id": work_id,
            "name": name,
            "role": role,
            "person_url": person_url,
            "identifiers": identifiers or {},
            "evidence": evidence,
            "source_url": source_url,
        }

    def do_nih(self) -> None:
        source_url = NIH_URL
        for scope, geography in GEOGRAPHIES.items():
            cities = geography["cities"]
            # Keep the source query below RePORTER's 15,000-offset ceiling by
            # querying small, disjoint city groups; all years are included.
            for group_i in range(0, len(cities), 5):
                city_group = cities[group_i:group_i + 5]
                stem = f"nih-{slug(scope)}-group-{group_i // 5 + 1}"
                offset = 0
                expected = None
                group_records = 0
                while self.can_request() and self.requests < self.max_requests - 3 and offset <= 14999:
                    if offset > 0 and expected is not None and offset >= expected:
                        break
                    payload = {
                        "criteria": {"org_states": ["NY"], "org_cities": city_group},
                        "offset": offset,
                        "limit": 500,
                        "sort_field": "project_start_date",
                        "sort_order": "desc",
                    }
                    data = json.dumps(payload).encode("utf-8")
                    raw_name = f"{stem}-offset-{offset}.json"
                    body, status, raw_path, digest, error = self.request_bytes(
                        source_url, data=data,
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                        raw_name=raw_name, nih_throttle=True,
                    )
                    if error:
                        self.errors.append(error)
                        self.sources.append({"url": source_url, "status": status, "records": group_records,
                                             "coverage": f"NY organization cities {city_group}; all fiscal years; offset {offset}",
                                             "limitation": error})
                        break
                    try:
                        response = json.loads(body or b"{}")
                    except json.JSONDecodeError as exc:
                        msg = f"NIH JSON decode error: {exc}"
                        self.errors.append(msg)
                        break
                    meta = response.get("meta", {})
                    expected = int(meta.get("total", 0) or 0)
                    results = response.get("results", [])
                    for item in results:
                        organization = item.get("organization") or {}
                        city = clean(organization.get("org_city") or organization.get("city")).upper()
                        state = clean(organization.get("org_state") or item.get("org_state")).upper()
                        if state != "NY" or city not in cities:
                            continue
                        title = clean(item.get("project_title") or item.get("title"))
                        appl_id = clean(item.get("appl_id") or item.get("application_id"))
                        project_num = clean(item.get("project_num") or item.get("project_number"))
                        subproject = clean(item.get("subproject_id"))
                        if not title or not appl_id:
                            continue
                        work_id = "nih:" + (appl_id + (":" + subproject if subproject else ""))
                        abstract = clean(item.get("abstract_text") or item.get("abstract"))
                        year = clean(item.get("fiscal_year"))
                        org_name = clean(organization.get("org_name") or organization.get("name"))
                        project_start = clean(item.get("project_start_date"))
                        project_end = clean(item.get("project_end_date"))
                        page_path, page_sha = raw_path or "", digest or ""
                        detail_url = clean(item.get("project_detail_url")) or f"https://reporter.nih.gov/project-details/{appl_id}"
                        regional = f"NIH RePORTER organization field lists {org_name or 'the funded organization'} in {city}, NY (org_city={city}; org_state=NY)."
                        desc = f"NIH RePORTER lists this as a FY{year} project funding record. Abstract: {abstract[:1600]}" if abstract else f"NIH RePORTER lists a FY{year} project funding record titled {title}. The record has no abstract text."
                        self.add_work({
                            "id": work_id, "title": title, "kind": "NIH-funded research project",
                            "source_url": detail_url, "observed_at": now(),
                            "work_date": iso_date(project_start or item.get("award_notice_date")),
                            "organization": org_name, "regional_evidence": regional,
                            "geography_scope": scope, "description": desc,
                            "raw_path": page_path, "raw_sha256": page_sha,
                        })
                        investigators = item.get("principal_investigators") or item.get("principal_investigator") or []
                        if isinstance(investigators, dict):
                            investigators = [investigators]
                        for investigator in investigators:
                            if isinstance(investigator, str):
                                name = investigator
                                profile_id = ""
                                is_contact = False
                            else:
                                name = clean(investigator.get("full_name") or investigator.get("name"))
                                if not name:
                                    name = clean(" ".join(filter(None, [investigator.get("first_name"), investigator.get("middle_name"), investigator.get("last_name")])))
                                profile_id = clean(investigator.get("profile_id") or investigator.get("profileId"))
                                is_contact = bool(investigator.get("is_contact_pi") or investigator.get("isContactPi"))
                            role = "principal investigator (contact PI)" if is_contact else "principal investigator"
                            evidence = f"NIH RePORTER lists {name} in the principal_investigators field for application {appl_id} (project {project_num}, FY{year})."
                            self.add_contribution(work_id, name, role, detail_url, evidence,
                                                  {"nih_profile_id": profile_id} if profile_id else {})
                    group_records += len(results)
                    if not results or offset + len(results) >= expected or len(results) < 500:
                        break
                    offset += len(results)
                incomplete = expected is not None and group_records < expected
                self.sources.append({
                    "url": source_url, "status": "partial" if incomplete else ("ok" if group_records or expected == 0 else "partial"),
                    "records": group_records,
                    "coverage": f"NIH RePORTER project API, NY funded-organization cities {city_group}, all fiscal years; fetched {group_records} records of API total {expected if expected is not None else 'unknown'} (last request offset {max(0, offset)}).",
                    "limitation": ((f"The RePORTER offset ceiling is 14,999; this city group has {expected} results, so only the first 15,000 could be fetched. " if expected and expected > 15000 else f"The request/time ceiling stopped pagination at {group_records} of {expected} records. " if incomplete else "") + "Search is bounded to named organization cities; annual application records can repeat multi-year project titles.")
                })
                if not self.can_request() or self.requests >= self.max_requests - 3:
                    return

    def do_nsf(self) -> None:
        fields = "id,title,abstractText,pdPIName,coPDPI,awardee,awardeeName,awardeeCity,awardeeStateCode,perfCity,perfStateCode,perfLocation,startDate,date,expDate,piId"
        for scope, geography in GEOGRAPHIES.items():
            for city_index, city in enumerate(geography["nsf_cities"]):
                if not self.can_request() or self.requests >= self.max_requests - 3:
                    all_scopes = list(GEOGRAPHIES.items())
                    current_scope_index = [label for label, _ in all_scopes].index(scope)
                    for pending_scope, pending_geo in all_scopes[current_scope_index:]:
                        start_index = city_index if pending_scope == scope else 0
                        for unqueried_city in pending_geo["nsf_cities"][start_index:]:
                            self.sources.append({"url": NSF_URL, "status": "not attempted", "records": 0,
                                                 "coverage": f"NSF Awards API performance city {unqueried_city}, NY; source sublane assigned.",
                                                 "limitation": "Request ceiling reserved three calls for the SBIR bulk download and two patent access probes."})
                    return
                city_records = 0
                offset = 0
                total = None
                while self.can_request() and self.requests < self.max_requests - 3:
                    params = {
                        "perfCity": city, "perfStateCode": "NY", "rpp": 25,
                        "offset": offset, "printFields": fields,
                    }
                    url = NSF_URL + "?" + urlencode(params)
                    raw_name = f"nsf-{slug(scope)}-{slug(city)}-offset-{offset}.json"
                    body, status, raw_path, digest, error = self.request_bytes(url, raw_name=raw_name)
                    if error:
                        self.errors.append(error)
                        self.sources.append({"url": url, "status": status, "records": city_records,
                                             "coverage": f"NSF awards performance city {city}, NY, offsets through {offset}",
                                             "limitation": error})
                        break
                    try:
                        response = json.loads(body or b"{}").get("response", {})
                    except json.JSONDecodeError as exc:
                        self.errors.append(f"NSF JSON decode error for {city}: {exc}")
                        break
                    metadata = response.get("metadata") or {}
                    if total is None:
                        try:
                            total = int(metadata.get("totalCount", 0))
                        except (TypeError, ValueError):
                            total = 0
                    records = response.get("award") or []
                    for item in records:
                        perf_city = clean(item.get("perfCity"))
                        perf_state = clean(item.get("perfStateCode")).upper()
                        if perf_state != "NY" or perf_city.casefold() != city.casefold():
                            continue
                        award_id = clean(item.get("id"))
                        title = clean(item.get("title"))
                        if not award_id or not title:
                            continue
                        work_id = f"nsf:{award_id}"
                        awardee = clean(item.get("awardeeName") or item.get("awardee"))
                        start_date = iso_date(item.get("startDate") or item.get("date"))
                        abstract = clean(item.get("abstractText"))
                        detail_url = f"https://api.nsf.gov/services/v1/awards/{award_id}.json"
                        evidence = f"NSF award record lists performance location {clean(item.get('perfLocation')) or awardee or 'the award site'}, {perf_city}, NY (perfCity={perf_city}; perfStateCode=NY)."
                        desc = f"NSF award abstract: {abstract[:1600]}" if abstract else f"NSF records an award titled {title}; no abstract text was returned."
                        self.add_work({
                            "id": work_id, "title": title, "kind": "NSF research award",
                            "source_url": detail_url, "observed_at": now(), "work_date": start_date,
                            "organization": awardee, "regional_evidence": evidence,
                            "geography_scope": scope, "description": desc,
                            "raw_path": raw_path or "", "raw_sha256": digest or "",
                        })
                        pi = clean(item.get("pdPIName"))
                        if pi:
                            identifiers = {"nsf_pi_id": clean(item.get("piId"))} if item.get("piId") else {}
                            self.add_contribution(work_id, pi, "principal investigator", detail_url,
                                                  f"NSF award {award_id} lists {pi} as project director/principal investigator (pdPIName).", identifiers)
                        co_pis = item.get("coPDPI") or []
                        if isinstance(co_pis, str):
                            co_pis = re.split(r"\s*;\s*|\s*\|\s*", co_pis)
                        for co_pi in co_pis:
                            self.add_contribution(work_id, clean(co_pi), "co-principal investigator", detail_url,
                                                  f"NSF award {award_id} lists {clean(co_pi)} in the coPDPI field.")
                    city_records += len(records)
                    if not records or offset + len(records) >= total or len(records) < 25:
                        break
                    offset += len(records)
                partial = total is not None and city_records < total
                self.sources.append({
                    "url": NSF_URL, "status": "partial" if partial else ("ok" if city_records else "no records"),
                    "records": city_records,
                    "coverage": f"NSF Awards API performance city {city}, NY, all available years; fetched {city_records} records of {total if total is not None else 'unknown'} total results (last request offset {max(0, offset)}).",
                    "limitation": ("Request ceiling stopped pagination before the complete result set was fetched. " if partial else "") + "NSF API returns at most 3,000 results for a search; API search is restricted to enumerated cities and performance locations."
                })

    def do_sbir(self) -> None:
        raw_name = "sbir-awards-no-abstract.csv"
        cached = self.raw / raw_name
        if self.reuse_existing and cached.exists():
            body = cached.read_bytes()
            status = 200
            raw_path = str(cached.relative_to(self.out))
            digest = hashlib.sha256(body).hexdigest()
            error = None
        else:
            body, status, raw_path, digest, error = self.request_bytes(
                SBIR_CSV_URL, raw_name=raw_name, timeout=120,
            )
        if error or body is None:
            self.errors.append(error or "SBIR award CSV request failed")
            self.sources.append({"url": SBIR_CSV_URL, "status": status, "records": 0,
                                 "coverage": "SBIR.gov downloadable award CSV (no abstract file)",
                                 "limitation": error or "No response body"})
            return
        found = 0
        try:
            text = body.decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(text.splitlines())
            duplicate_counts: dict[tuple[str, ...], int] = {}
            for row in reader:
                state = clean(row.get("State")).upper()
                city = clean(row.get("City")).upper()
                if state.casefold() not in {"ny", "new york"}:
                    continue
                scope = next((label for label, geo in GEOGRAPHIES.items() if city in geo["cities"]), None)
                if not scope:
                    continue
                title = clean(row.get("Award Title"))
                company = clean(row.get("Company"))
                if not title or not company:
                    continue
                agency = clean(row.get("Agency"))
                branch = clean(row.get("Branch"))
                phase = clean(row.get("Phase"))
                program = clean(row.get("Program"))
                tracking = clean(row.get("Agency Tracking Number"))
                contract = clean(row.get("Contract"))
                proposal_award_date = clean(row.get("Proposal Award Date"))
                identity = (agency, branch, company, tracking, contract, program, phase, proposal_award_date, title)
                occurrence = duplicate_counts.get(identity, 0) + 1
                duplicate_counts[identity] = occurrence
                identity_digest = hashlib.sha256(("\0".join(identity) + f"\0duplicate:{occurrence}").encode("utf-8")).hexdigest()[:24]
                identifier = tracking or contract or "no-award-number"
                work_id = f"sbir:{identity_digest}"
                source_url = SBIR_CSV_URL
                phase_label = f"Phase {phase}" if phase and not phase.casefold().startswith("phase") else phase
                kind = f"SBIR/STTR {program or 'award'} {phase_label} funding award".strip()
                regional = f"SBIR.gov award CSV lists the recipient company address as {city}, NY (city={city}; state=NY)."
                desc = f"SBIR.gov lists an {agency}{(' / ' + branch) if branch else ''} {phase_label} {program} award titled {title} to {company}. The downloadable file does not include abstracts, so this record does not assert project results."
                self.add_work({
                    "id": work_id, "title": title, "kind": kind, "source_url": source_url,
                    "observed_at": now(), "work_date": iso_date(proposal_award_date),
                    "organization": company, "regional_evidence": regional,
                    "geography_scope": scope, "description": desc,
                    "raw_path": raw_path or "", "raw_sha256": digest or "",
                })
                pi_name = clean(row.get("PI Name"))
                pi_title = clean(row.get("PI Title"))
                if pi_name:
                    self.add_contribution(work_id, pi_name, "principal investigator", source_url,
                                          f"The SBIR.gov award CSV names {pi_name} in its PI Name field for agency tracking number {tracking or contract or identifier}{(' (PI Title: ' + pi_title + ')') if pi_title else ''}.")
                found += 1
        except Exception as exc:
            self.errors.append(f"SBIR CSV parse error: {type(exc).__name__}: {exc}")
        sbir_source = {
            "url": SBIR_CSV_URL, "status": status, "records": found,
            "coverage": "Full current downloadable SBIR.gov award_data_no_abstract.csv; scanned all rows for New York recipient addresses in enumerated Western NY and Rochester/Finger Lakes cities; all award years included.",
            "limitation": "The SBIR award API page reported maintenance and API calls returned 403 during live checks; bulk CSV omits project abstracts and does not expose unique SBIR.gov page IDs in this file."
        }
        existing_index = next((i for i, source in enumerate(self.sources) if source.get("url") == SBIR_CSV_URL), None)
        if existing_index is None:
            self.sources.append(sbir_source)
        else:
            self.sources[existing_index] = sbir_source
        api_source = {
            "url": "https://www.sbir.gov/api", "status": "maintenance notice",
            "records": 0,
            "coverage": "SBIR.gov API documentation page was checked; it reports the Award API is under maintenance. The full downloadable award CSV was used instead.",
            "limitation": "Award API unavailable during this run; award CSV does not include abstracts or page IDs."
        }
        api_index = next((i for i, source in enumerate(self.sources) if source.get("url") == api_source["url"]), None)
        if api_index is None:
            self.sources.append(api_source)
        else:
            self.sources[api_index] = api_source

    def do_patentsview(self) -> None:
        # PatentsView moved to USPTO's ODP. Public USPTO documentation says the
        # ODP data API requires a key; preserve evidence of the no-key result.
        for label, url, filename in [
            ("USPTO ODP PatentsView bulk search", USPTO_PRODUCTS_SEARCH, "patentsview-odp-no-key.txt"),
            ("Legacy PatentsView bulk inventor table", LEGACY_PATENT_BULK, "patentsview-legacy-bulk-403.txt"),
        ]:
            if not self.can_request():
                break
            body, status, raw_path, digest, error = self.request_bytes(url, raw_name=filename, timeout=25)
            if error:
                self.errors.append(f"{label}: {error}")
            else:
                # A successful search response still does not guarantee that
                # the required inventor, patent, and location tables are public.
                try:
                    content = json.loads(body or b"{}")
                    if content.get("products") or content.get("results"):
                        self.sources.append({"url": url, "status": status, "records": 0,
                                             "coverage": "ODP PatentsView catalog probe only; no inventor/location work rows extracted.",
                                             "limitation": "No query credential configured; this catalog response is not a regional inventor extract."})
                        continue
                except Exception:
                    pass
            self.sources.append({
                "url": url, "status": status, "records": 0,
                "coverage": f"Attempted {label} once using public unauthenticated access.",
                "limitation": error or "The public endpoint did not return a usable PatentsView inventor/location dataset; no access-control bypass attempted.",
            })
        self.sources.append({
            "url": USPTO_PATENTVIEW_HUB, "status": "documented source; dataset access blocked",
            "records": 0,
            "coverage": "USPTO PatentsView hub identifies disambiguated granted patent data through 2025-12-31 in the USPTO Open Data Portal.",
            "limitation": "The current ODP API rejected a no-key search (HTTP 401) and the retired legacy inventor bulk URL returned HTTP 403. USPTO docs state the ODP API requires an API key; no credential was present. No patent records were emitted."
        })
        self.limitations.append("Patents/inventors are not represented in this run: the current USPTO ODP endpoint required an API key (HTTP 401) and the legacy PatentsView S3 bulk URL returned HTTP 403. The official USPTO hub reports Q4 2025 disambiguated tables are hosted at ODP; this lane made no authenticated request or access-control bypass.")

    def write_outputs(self) -> None:
        works_path = self.out / "works.jsonl"
        contrib_path = self.out / "contributions.jsonl"
        for item in self.works.values():
            item["work_date"] = iso_date(item.get("work_date"))
        with works_path.open("w", encoding="utf-8") as f:
            for item in sorted(self.works.values(), key=lambda x: (x["geography_scope"], x["work_date"], x["id"])):
                f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        with contrib_path.open("w", encoding="utf-8") as f:
            for item in sorted(self.contributions.values(), key=lambda x: (x["work_id"], x["name"].casefold(), x["role"])):
                f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        manifest = {
            "lane": "funded_inventions",
            "started_at": self.started_at,
            "completed_at": now(),
            "requests": self.previous_requests + self.requests,
            "works": len(self.works),
            "contributions": len(self.contributions),
            "complete": False,
            "scope": "Public NIH RePORTER grants, NSF awards, SBIR/STTR awards, and PatentsView/USPTO inventor credits for Buffalo/Western New York and separately labeled Rochester/Finger Lakes. Regional relevance is supported by source organization, recipient, or performance location; people are credited only in named PI/inventor fields.",
            "sources": self.sources,
            "limitations": self.limitations,
            "errors": self.errors,
        }
        (self.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT, help=f"Output directory (default: {DEFAULT_OUT})")
    parser.add_argument("--max-requests", type=int, default=150, help="Network request ceiling (default: 150)")
    parser.add_argument("--max-seconds", type=int, default=900, help="Elapsed time ceiling (default: 900)")
    parser.add_argument("--lanes", default="nih,nsf,sbir,patents", help="Comma-separated sublanes: nih, nsf, sbir, patents")
    parser.add_argument("--resume-existing", action="store_true", help="Load and extend existing outputs in --output")
    args = parser.parse_args()
    collector = Collector(args.output, args.max_requests, args.max_seconds)
    if args.resume_existing:
        collector.reuse_existing = True
        manifest_path = args.output / "manifest.json"
        if manifest_path.exists():
            prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            collector.started_at = prior.get("started_at", collector.started_at)
            collector.previous_requests = int(prior.get("requests", 0) or 0)
            collector.sources.extend(prior.get("sources", []))
            collector.errors.extend(prior.get("errors", []))
            collector.limitations.extend(x for x in prior.get("limitations", []) if "PatentsView was not attempted" not in x)
            # Replace stale budget-exhausted patent entries when the follow-up
            # run below performs the two live access probes.
            collector.sources = [s for s in collector.sources if not (s.get("url") == USPTO_PATENTVIEW_HUB and s.get("status") == "not attempted")]
        for source in collector.sources:
            coverage = source.get("coverage", "")
            if "API result total 21374" in coverage:
                source["status"] = "partial"
                source["limitation"] = "Only the first 15,000 of 21,374 NIH records were fetched because RePORTER caps offsets at 14,999; other NIH city groups were fetched as listed. Annual application records can repeat multi-year project titles."
                source["coverage"] = "NIH RePORTER project API, NY funded-organization cities ['ROCHESTER', 'SENECA FALLS', 'SODUS', 'VICTOR', 'WATERLOO'], all fiscal years; fetched 15,000 of 21,374 records (last page offset 14,500; 500 per page)."
            if "NSF Awards API performance city Rochester" in coverage and "of 1875 total results" in coverage:
                source["status"] = "partial"
                source["limitation"] = "The request ceiling stopped this search after its first three pages (75 of 1,875); other NSF cities were not queried. The NSF API allows up to 3,000 results per search."
                source["coverage"] = "NSF Awards API performance city Rochester, NY, all available years; fetched 75 of 1,875 records (three pages at offsets 0, 25, 50; 25 per page)."
        if args.resume_existing:
            covered = {source.get("coverage", "") for source in collector.sources}
            for city in GEOGRAPHIES["Rochester / Finger Lakes"]["nsf_cities"][1:]:
                if not any(f"performance city {city}, NY" in coverage for coverage in covered):
                    collector.sources.append({
                        "url": NSF_URL, "status": "not attempted", "records": 0,
                        "coverage": f"NSF Awards API performance city {city}, NY; assigned locality, not queried in the first run.",
                        "limitation": "The request ceiling was reached while paging the Rochester search; city-specific NSF coverage is incomplete."
                    })
        for line in (args.output / "works.jsonl").read_text(encoding="utf-8").splitlines() if (args.output / "works.jsonl").exists() else []:
            if line.strip():
                item = json.loads(line)
                collector.works[item["id"]] = item
        for line in (args.output / "contributions.jsonl").read_text(encoding="utf-8").splitlines() if (args.output / "contributions.jsonl").exists() else []:
            if line.strip():
                item = json.loads(line)
                collector.contributions[(item["work_id"], item["name"].casefold(), item["role"])] = item
    steps = {"nih": collector.do_nih, "nsf": collector.do_nsf, "sbir": collector.do_sbir, "patents": collector.do_patentsview}
    selected = [name.strip().casefold() for name in args.lanes.split(",") if name.strip()]
    for name in selected:
        step = steps.get(name)
        if step is None:
            parser.error(f"Unknown lane {name!r}; choose from {', '.join(steps)}")
        if not collector.can_request() and name not in {"sbir", "patents"}:
            break
        try:
            step()
        except Exception as exc:
            collector.errors.append(f"{step.__name__}: {type(exc).__name__}: {exc}")
    if "patents" not in selected and not any("PatentsView" in source.get("url", "") or "PatentsView" in source.get("coverage", "") for source in collector.sources):
        collector.sources.append({"url": USPTO_PATENTVIEW_HUB, "status": "not attempted", "records": 0,
                                 "coverage": "PatentsView/USPTO inventor sublane assigned.",
                                 "limitation": "Request/time budget was exhausted before the patent source attempt."})
        collector.limitations.append("PatentsView was not attempted because the request/time ceiling was exhausted.")
    collector.write_outputs()
    print(json.dumps({"output": str(args.output), "requests": collector.requests,
                      "works": len(collector.works), "contributions": len(collector.contributions),
                      "errors": len(collector.errors)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
