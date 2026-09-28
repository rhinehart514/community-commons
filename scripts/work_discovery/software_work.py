#!/usr/bin/env python3
"""Collect public software work linked to documented Buffalo/WNY accounts.

Regional identities come from the 2026-09-25 public developer snapshot and the
local enrichment database. The collector never treats repository ownership as
authorship: GitHub contributor, release-author, or event metadata must name the
account on the work itself.
"""
from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


REPO = Path(__file__).resolve().parents[2]
DEFAULT_SEED = REPO / "data/seeds/developers"
DEFAULT_ENRICHMENT = REPO / "data/enrichment.sqlite"
USER_AGENT = "bp-commons-public-work-discovery/1.0 (public professional metadata)"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def identifier(source: str, external_id: str) -> str:
    return hashlib.sha256((source + "\0" + external_id).encode("utf-8")).hexdigest()[:24]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def github_handle(value: str) -> str:
    return value.strip().removeprefix("https://github.com/").removeprefix("http://github.com/").strip("/").split("/")[0]


class Collector:
    def __init__(self, output: Path, budget: int, deadline_seconds: int):
        self.output = output
        self.raw = output / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.budget = min(max(budget, 1), 145)
        self.deadline = time.monotonic() + max(30, deadline_seconds)
        self.started = now()
        self.requests = 0
        self.serial = 0
        self.works: dict[str, dict] = {}
        self.contributions: dict[tuple[str, str, str], dict] = {}
        self.sources: list[dict] = []
        self.errors: list[str] = []
        self.attempted: set[str] = set()
        self.stats: dict[str, int] = {}

    def can_request(self) -> bool:
        return self.requests < self.budget and time.monotonic() < self.deadline

    def fetch(self, family: str, url: str, *, github_path: str | None = None) -> tuple[object | None, bytes | None, int, str]:
        if not self.can_request():
            return None, None, 0, "budget_exhausted"
        self.requests += 1
        self.attempted.add(family)
        self.serial += 1
        status = 0
        body = b""
        content_type = ""
        try:
            if github_path is not None:
                # gh uses its existing credential store; no token is read or printed.
                proc = subprocess.run(["gh", "api", github_path], capture_output=True, timeout=30)
                if proc.returncode:
                    return None, None, 0, "github_api_error"
                body = proc.stdout
                status = 200
            else:
                req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html;q=0.9,*/*;q=0.5"})
                with urlopen(req, timeout=25) as response:
                    status = int(response.status)
                    content_type = response.headers.get("Content-Type", "")
                    body = response.read()
            value = json.loads(body) if "json" in url.lower() or github_path is not None or "json" in content_type.lower() else body
            self.stats[family] = self.stats.get(family, 0) + (len(value) if isinstance(value, list) else 1)
            return value, body, status, "ok"
        except Exception as exc:
            # Do not expose headers, environment, or credential-bearing exception text.
            label = type(exc).__name__
            self.errors.append(f"{family}: {label}")
            return None, None, status, label

    def save_raw(self, label: str, body: bytes) -> tuple[str, str]:
        name = f"{self.serial:04d}-{label}.json"
        path = self.raw / name
        path.write_bytes(body)
        return str(Path("raw") / name), hashlib.sha256(body).hexdigest()

    def source(self, url: str, status: int | str, records: int, coverage: str, limitation: str = "") -> None:
        self.sources.append({"url": url, "status": status, "records": records, "coverage": coverage, "limitation": limitation})

    def add_work(self, work: dict) -> None:
        self.works[work["id"]] = work

    def add_contribution(self, row: dict) -> None:
        key = (row["work_id"], row["name"], row["role"])
        self.contributions[key] = row

    def finish(self, lane: str, scope: str, limitations: list[str]) -> None:
        (self.output / "works.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self.works.values()), encoding="utf-8")
        (self.output / "contributions.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self.contributions.values()), encoding="utf-8")
        manifest = {
            "lane": lane,
            "started_at": self.started,
            "completed_at": now(),
            "requests": self.requests,
            "works": len(self.works),
            "contributions": len(self.contributions),
            "complete": False,
            "scope": scope,
            "sources": self.sources,
            "limitations": limitations,
            "errors": self.errors,
        }
        (self.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_enrichment(path: Path) -> dict[str, dict]:
    """Load exact GitHub identities already joined in the local evidence store."""
    if not path.exists():
        return {}
    import sqlite3
    result: dict[str, dict] = {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    query = """
        SELECT l.subject AS login, c.predicate, c.value
        FROM links AS l
        JOIN jobs AS j ON j.provider = l.provider AND j.subject = l.subject
        JOIN responses AS r ON r.job_id = j.id
        JOIN claims AS c ON c.response_id = r.id
        WHERE l.provider = 'github-profile'
        ORDER BY r.observed
    """
    for row in db.execute(query):
        info = result.setdefault(row["login"].lower(), {"claims": {}})
        info["claims"][row["predicate"]] = row["value"]
    db.close()
    return result


def load_regional(seed_dir: Path, enrichment_path: Path, output: Path) -> tuple[dict[str, dict], list[dict]]:
    people_path = seed_dir / "people.jsonl"
    profiles = read_jsonl(people_path)
    enrichment = load_enrichment(enrichment_path)
    people: dict[str, dict] = {}
    eligible_rows = []
    for row in profiles:
        scope = row.get("geography_scope", "")
        if scope not in {"Buffalo/WNY", "Rochester expansion"}:
            continue
        login = (row.get("external_ids") or {}).get("github") or github_handle(row.get("profile_url", ""))
        if not login:
            continue
        location = row.get("location") or ""
        observed = row.get("observed_at") or ""
        region_source = (
            f"Cached public GitHub profile from {seed_dir}/people.jsonl, originally observed {observed}; "
            f"the source-reported location field was {location!r}. The field is user-supplied and may be stale."
        )
        evidence_record = {
            "login": login,
            "name": row.get("name") or f"@{login}",
            "person_url": row.get("profile_url") or f"https://github.com/{login}",
            "location": location,
            "geography_scope": scope,
            "regional_evidence": region_source,
            "cached_observed_at": observed,
            "cached_origin": str(people_path),
            "enrichment_claims": enrichment.get(login.lower(), {}).get("claims", {}),
        }
        people[login.lower()] = evidence_record
        eligible_rows.append(evidence_record)
    seed_raw = output / "raw" / "seed"
    seed_raw.mkdir(parents=True, exist_ok=True)
    seed_file = seed_raw / "regional-github-profiles.jsonl"
    seed_file.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in eligible_rows), encoding="utf-8")
    return people, eligible_rows


def load_repositories(seed_dir: Path, people: dict[str, dict]) -> list[dict]:
    repos: dict[str, dict] = {}
    raw_dir = seed_dir / "raw"
    for path in sorted(raw_dir.glob("graphql-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for user in ((payload.get("data") or {}).get("nodes") or []):
            if not user:
                continue
            login = (user.get("login") or "").lower()
            person = people.get(login)
            if not person:
                continue
            for repo in (((user.get("repositories") or {}).get("nodes")) or []):
                if not repo or repo.get("isFork") or not repo.get("url"):
                    continue
                full = "/".join(urlparse(repo["url"]).path.strip("/").split("/")[:2])
                if full.count("/") != 1:
                    continue
                repos.setdefault(full.lower(), {
                    "full": full,
                    "url": repo["url"],
                    "name": repo.get("name") or full.split("/")[-1],
                    "description": (repo.get("description") or "").strip(),
                    "homepage": (repo.get("homepageUrl") or "").strip(),
                    "updated_at": repo.get("updatedAt") or "",
                    "owner_login": login,
                    "owner_person": person,
                })
    for row in read_jsonl(seed_dir / "edges.jsonl"):
        if row.get("relation") != "public_repository_owner":
            continue
        login = github_handle(row.get("subject_url", "")).lower()
        person = people.get(login)
        if not person:
            continue
        full = "/".join(urlparse(row.get("object_url", "")).path.strip("/").split("/")[:2])
        if full.count("/") != 1:
            continue
        evidence = row.get("evidence", "")
        desc_match = re.search(r"Description:\s*(.*?)(?:\s+Homepage:\s*|$)", evidence, re.I)
        homepage_match = re.search(r"Homepage:\s*(https?://\S+)", evidence, re.I)
        repo_name = full.split("/")[-1]
        repos.setdefault(full.lower(), {
            "full": full,
            "url": row.get("object_url"),
            "name": repo_name,
            "description": desc_match.group(1).strip() if desc_match else "",
            "homepage": homepage_match.group(1).rstrip(".,)") if homepage_match else "",
            "updated_at": "",
            "owner_login": login,
            "owner_person": person,
        })
    def rank(repo: dict) -> tuple[int, int, str]:
        homepage = repo.get("homepage", "").lower()
        package_link = any(domain in homepage for domain in ("pypi.org/project/", "npmjs.com/package/", "npmjs.org/package/"))
        software_signal = bool(re.search(r"\b(?:library|package|framework|sdk|tool|plugin|extension|software|api|cli)\b", repo.get("description", ""), re.I))
        return (int(package_link), int(software_signal), repo.get("updated_at", ""))
    # Exact package links and software descriptions receive first coverage; ownership only seeds discovery.
    return sorted(repos.values(), key=rank, reverse=True)


def event_credit(event: dict, login: str) -> dict | None:
    """Return only a public work role established by the event's actual payload."""
    if ((event.get("actor") or {}).get("login") or "").lower() != login.lower():
        return None
    if (event.get("actor") or {}).get("type") in {"Bot", "Organization"}:
        return None
    event_type = event.get("type") or ""
    payload = event.get("payload") or {}
    repo_full = (event.get("repo") or {}).get("name") or ""
    work_date = (event.get("created_at") or "")[:10]
    event_id = str(event.get("id") or "")
    if event_type == "PushEvent":
        commits = payload.get("commits") or []
        head = payload.get("head") or (commits[0].get("sha") if commits else "")
        if not head or not repo_full:
            return None
        branch = (payload.get("ref") or "").removeprefix("refs/heads/")
        message = re.sub(r"\s+", " ", commits[0].get("message") or "").strip() if commits else ""
        title = f"{repo_full}: {message[:140]}" if message else f"Public push to {repo_full}" + (f" ({branch})" if branch else "")
        return {
            "event_id": event_id or str(payload.get("push_id") or head),
            "title": title,
            "kind": "public repository push",
            "source_url": f"https://github.com/{repo_full}/commit/{head}",
            "work_date": work_date,
            "role": "push actor; commit authorship is not established by this event",
            "description": f"GitHub public event reports @{login} pushed to {repo_full}. The event names head commit {head}; it does not expose the commit author." + (f" First commit message shown by the event: {message[:300]}" if message else ""),
        }
    if event_type in {"PullRequestEvent", "IssuesEvent"}:
        key = "pull_request" if event_type == "PullRequestEvent" else "issue"
        item = payload.get(key) or {}
        number = item.get("number") or payload.get("number")
        if not repo_full or not number:
            return None
        item_url = item.get("html_url") or f"https://github.com/{repo_full}/{'pull' if key == 'pull_request' else 'issues'}/{number}"
        actual_author = ((item.get("user") or {}).get("login") or "").lower()
        role = ("pull request author" if key == "pull_request" else "issue author") if actual_author == login.lower() else ("pull request event actor" if key == "pull_request" else "issue event actor")
        activity = "pull request" if key == "pull_request" else "issue"
        title = item.get("title") or f"{activity.title()} #{number} in {repo_full}"
        action = payload.get("action") or ""
        return {
            "event_id": event_id or f"{key}:{repo_full}#{number}",
            "title": title,
            "kind": activity,
            "source_url": item_url,
            "work_date": work_date,
            "role": role,
            "description": f"GitHub public event reports @{login} as the actor for this {activity} event in {repo_full}." + (f" Event action: {action}." if action else ""),
        }
    if event_type == "ReleaseEvent":
        release = payload.get("release") or {}
        release_author = release.get("author") or {}
        author = (release_author.get("login") or "").lower()
        if release_author.get("type") in {"Bot", "Organization"}:
            return None
        if not release.get("html_url") or author != login.lower():
            return None
        tag = release.get("tag_name") or ""
        return {
            "event_id": event_id or "release:" + str(release.get("id") or release.get("html_url")),
            "title": release.get("name") or tag or f"Release from {repo_full}",
            "kind": "software release",
            "source_url": release["html_url"],
            "work_date": (release.get("published_at") or work_date)[:10],
            "role": "release author",
            "description": f"GitHub public event identifies @{login} as the author of a release for {repo_full}.",
        }
    # Stars, watches, forks, comments, reviews, and repository creation are not authorship.
    return None


def regional_contributors(users: list[dict], people: dict[str, dict]) -> list[dict]:
    """Select accounts explicitly named by GitHub's contributors endpoint."""
    return [
        user for user in users
        if user.get("login")
        and user["login"].lower() in people
        and user.get("type", "User") not in {"Bot", "Organization"}
    ]


def contributor_account_credit(user: dict, people: dict[str, dict]) -> tuple[str, str] | None:
    """Describe a named GitHub contributor account without turning bots/orgs into people."""
    login = user.get("login") or ""
    if not login or user.get("type", "User") in {"Bot", "Organization"}:
        return None
    count = user.get("contributions", 0)
    person = people.get(login.lower())
    if person:
        return person["name"], f"repository contributor ({count} public contribution(s) reported)"
    return f"GitHub account @{login}", f"repository contributor account (real-world identity unverified; {count} public contribution(s) reported)"


def region_evidence(person: dict) -> str:
    return f"{person['regional_evidence']} Regional scope: {person['geography_scope']}."


def github_source(c: Collector, family: str, path: str) -> tuple[object | None, str, str, str]:
    url = "https://api.github.com/" + path
    value, body, status, error = c.fetch(family, url, github_path=path)
    if body is None:
        c.source(url, error, 0, f"One public endpoint requested: {path}.", "No response body was retained.")
        return None, "", "", error
    raw_path, sha = c.save_raw(family, body)
    records = len(value) if isinstance(value, list) else 1
    c.source(url, status, records, f"Fetched {path}; one API response. Records are limited to this endpoint's returned page.")
    return value, raw_path, sha, "ok"


def collect_events(c: Collector, people: dict[str, dict], profile_counts: dict[str, int], limit: int) -> None:
    family = "github-public-events"
    # Higher public-repository counts prioritize accounts with a larger observable software footprint.
    candidates = sorted(people.values(), key=lambda p: (profile_counts.get(p["login"].lower(), 0), p["login"].lower()), reverse=True)[:limit]
    event_count = 0
    for person in candidates:
        if not c.can_request():
            break
        login = person["login"]
        path = f"users/{quote(login, safe='')}/events/public?per_page=100"
        events, raw_path, raw_sha, status = github_source(c, family, path)
        if not isinstance(events, list):
            continue
        event_count += len(events)
        for event in events:
            credit = event_credit(event, login)
            if not credit:
                continue
            work_id = identifier("github-event", credit["event_id"])
            c.add_work({
                "id": work_id,
                "title": credit["title"],
                "kind": credit["kind"],
                "source_url": credit["source_url"],
                "observed_at": now(),
                "work_date": credit["work_date"],
                "organization": ((event.get("repo") or {}).get("name") or "").split("/", 1)[0],
                "regional_evidence": region_evidence(person) + f" GitHub public event response records @{login} as the event actor; retained seed link: {person['cached_origin']}.",
                "geography_scope": person["geography_scope"],
                "description": credit["description"],
                "raw_path": raw_path,
                "raw_sha256": raw_sha,
            })
            c.add_contribution({
                "work_id": work_id,
                "name": person["name"],
                "role": credit["role"],
                "person_url": person["person_url"],
                "identifiers": {"github": login},
                "evidence": f"GitHub {event.get('type')} response names @{login} as event actor. Role is limited to the payload's reported activity.",
                "source_url": f"https://api.github.com/users/{quote(login, safe='')}/events/public?per_page=100",
            })
    c.sources.append({
        "url": "https://api.github.com/users/{login}/events/public?per_page=100",
        "status": "attempted",
        "records": event_count,
        "coverage": f"Fetched public event pages for {len(candidates)} highest-public-repository-count Buffalo/WNY/Rochester-seeded GitHub accounts; at most 100 events per account and GitHub's public feed window (typically 90 days).",
        "limitation": "This is a recency sample, not a complete contribution history; push actor is not asserted to be commit author.",
    })


def collect_contributors_and_releases(c: Collector, people: dict[str, dict], repos: list[dict], repo_limit: int, release_limit: int) -> dict[str, list[dict]]:
    family = "github-repository-contributors"
    matched: dict[str, list[dict]] = {}
    scanned = 0
    excluded_bots = 0
    excluded_organizations = 0
    for repo in repos:
        if scanned >= repo_limit or not c.can_request():
            break
        scanned += 1
        full = repo["full"]
        path = f"repos/{quote(full, safe='/')}/contributors?per_page=100"
        users, raw_path, raw_sha, status = github_source(c, family, path)
        if not isinstance(users, list):
            continue
        local = regional_contributors(users, people)
        if not local:
            continue
        # The contributor response names every listed public account; include only non-anonymous credited accounts.
        work_id = identifier("github-repository", full.lower())
        title = f"{repo['owner_login']}/{repo['name']}"
        description = repo.get("description", "")
        c.add_work({
            "id": work_id,
            "title": title,
            "kind": "public software repository",
            "source_url": repo["url"],
            "observed_at": now(),
            "work_date": "",
            "organization": full.split("/", 1)[0],
            "regional_evidence": "The GitHub public contributors endpoint names a seeded regional account on this repository. " + " ".join(region_evidence(people[u["login"].lower()]) for u in local),
            "geography_scope": "; ".join(sorted({people[u["login"].lower()]["geography_scope"] for u in local})),
            "description": description or "Public software repository listed by GitHub; the repository's top contributor accounts are retained from its public contributors endpoint.",
            "raw_path": raw_path,
            "raw_sha256": raw_sha,
        })
        matched[full.lower()] = users
        for user in users:
            login = user.get("login") or ""
            if user.get("type") == "Bot":
                excluded_bots += 1
                continue
            if user.get("type") == "Organization":
                excluded_organizations += 1
                continue
            credit = contributor_account_credit(user, people)
            if not credit:
                continue
            person = people.get(login.lower())
            name, role = credit
            c.add_contribution({
                "work_id": work_id,
                "name": name,
                "role": role,
                "person_url": (person or {}).get("person_url") or user.get("html_url") or f"https://github.com/{login}",
                "identifiers": {"github": login},
                "evidence": f"GitHub public /contributors endpoint lists @{login} ({user.get('type', 'User')}) with {user.get('contributions', 0)} contribution(s) for {full}; this endpoint does not identify individual authored commits.",
                "source_url": f"https://api.github.com/repos/{full}/contributors?per_page=100",
            })
    c.sources.append({
        "url": "https://api.github.com/repos/{owner}/{repo}/contributors?per_page=100",
        "status": "attempted",
        "records": sum(len(v) for v in matched.values()),
        "coverage": f"Fetched one top-contributors page for {scanned} public repositories seeded from cached regional-profile repository lists; only repositories whose response named a seeded regional account became works.",
        "limitation": f"GitHub returns contribution counts, not commit-level names; up to 100 credited accounts per repository were retained. Repository owner alone was never credited. Known Bot accounts ({excluded_bots}) and Organization accounts ({excluded_organizations}) remain only in raw snapshots and are excluded from person contributions.",
    })
    release_family = "github-releases"
    release_records = 0
    requested = 0
    for repo in repos:
        if requested >= release_limit or not c.can_request():
            break
        if repo["full"].lower() not in matched:
            continue
        requested += 1
        full = repo["full"]
        path = f"repos/{quote(full, safe='/')}/releases?per_page=20"
        releases, raw_path, raw_sha, status = github_source(c, release_family, path)
        if not isinstance(releases, list):
            continue
        regional = [people[u["login"].lower()] for u in matched[full.lower()] if u.get("login", "").lower() in people]
        for release in releases:
            author_login = ((release.get("author") or {}).get("login") or "").lower()
            author_type = (release.get("author") or {}).get("type", "User")
            if author_type in {"Bot", "Organization"}:
                continue
            person = people.get(author_login)
            if not person or not release.get("html_url"):
                continue
            release_records += 1
            tag = release.get("tag_name") or ""
            title = release.get("name") or tag or f"Release of {repo['name']}"
            work_id = identifier("github-release", str(release.get("id") or release["html_url"]))
            c.add_work({
                "id": work_id,
                "title": title,
                "kind": "software release",
                "source_url": release["html_url"],
                "observed_at": now(),
                "work_date": (release.get("published_at") or "")[:10],
                "organization": full.split("/", 1)[0],
                "regional_evidence": region_evidence(person) + f" GitHub release metadata names @{author_login} as the release author for {full}.",
                "geography_scope": person["geography_scope"],
                "description": re.sub(r"\s+", " ", html.unescape(release.get("body") or "")).strip()[:800] or f"GitHub release {tag} for {full}.",
                "raw_path": raw_path,
                "raw_sha256": raw_sha,
            })
            c.add_contribution({
                "work_id": work_id,
                "name": person["name"],
                "role": "release author",
                "person_url": person["person_url"],
                "identifiers": {"github": author_login},
                "evidence": f"GitHub release API author field names @{author_login}.",
                "source_url": release["html_url"],
            })
    c.sources.append({
        "url": "https://api.github.com/repos/{owner}/{repo}/releases?per_page=20",
        "status": "attempted",
        "records": release_records,
        "coverage": f"Fetched up to 20 latest releases for {requested} repositories whose contributors page first verified a regional contributor.",
        "limitation": "Only named regional release authors are credited; repositories with no listed releases and releases credited to other accounts produce no regional release records.",
    })
    return matched


def parse_github_repo(text: str) -> str:
    value = (text or "").replace("git+", "").removesuffix(".git")
    match = re.search(r"github\.com[:/]([^/\s]+)/([^/#?\s]+)", value, re.I)
    return f"{match.group(1)}/{match.group(2)}".lower() if match else ""


def verified_package_repository(repository_url: str, expected_repo: str, regional_repositories: dict[str, list[dict]]) -> str:
    """Return the linked repo only when metadata and contributor response both establish it."""
    actual = parse_github_repo(repository_url)
    expected = expected_repo.lower()
    return actual if actual == expected and actual in regional_repositories else ""


def pypi_author_credit(work_id: str, author: str, source_url: str) -> dict:
    """Keep PyPI name strings separate until a source-backed identity link exists."""
    return {
        "work_id": work_id,
        "name": author,
        "role": "package author/maintainer (PyPI metadata field; identity unlinked)",
        "person_url": "",
        "identifiers": {},
        "evidence": f"PyPI metadata names {author!r}; no independent source establishes that this name belongs to a regional GitHub account.",
        "source_url": source_url,
    }


def huggingface_account_credit(work_id: str, hf_user: str, source_url: str) -> dict:
    """Represent the linked Hugging Face account without asserting account ownership."""
    return {
        "work_id": work_id,
        "name": f"@{hf_user}",
        "role": "Hugging Face account linked from GitHub profile bio (ownership unverified)",
        "person_url": "",
        "identifiers": {"huggingface": hf_user},
        "evidence": f"Hugging Face API lists this item under @{hf_user}; the regional GitHub bio contains a link to that Hugging Face profile, but the account owner is unverified.",
        "source_url": source_url,
    }


def collect_npm(c: Collector, people: dict[str, dict], repos: list[dict], matched: dict[str, list[dict]], limit: int = 5) -> None:
    family = "npm-packages"
    # Follow explicit package URLs from cached GitHub repository metadata. Account names are never matched by guess.
    candidates = []
    for repo in repos:
        match = re.search(r"npmjs?\.com/package/(@?[^/\s?#]+(?:/[^/\s?#]+)?)", repo.get("homepage", ""), re.I)
        if match:
            candidates.append((repo, match.group(1)))
    candidates = candidates[:limit]
    records = 0
    for repo, package_name in candidates:
        if not c.can_request():
            break
        url = f"https://registry.npmjs.org/{quote(package_name, safe='@')}"
        value, body, status, error = c.fetch(family, url)
        if body is None or not isinstance(value, dict):
            c.source(url, error, 0, f"One npm registry project lookup for package named by {repo['homepage']}.", "Package endpoint unavailable or returned a non-JSON response.")
            continue
        raw_path, raw_sha = c.save_raw("npm-package", body)
        repository = value.get("repository") or ""
        if isinstance(repository, dict):
            repository = repository.get("url") or ""
        repo_full = verified_package_repository(repository, repo["full"], matched)
        if not repo_full:
            actual_repo = parse_github_repo(repository)
            reason = "Registry repository URL did not confirm the cached GitHub project." if actual_repo != repo["full"].lower() else "No regional GitHub contributor was found for the linked repository."
            c.source(url, status, 0, f"Fetched npm metadata for {package_name}.", reason + " No regional package work was asserted.")
            continue
        local = [people[u["login"].lower()] for u in matched[repo_full] if u.get("login", "").lower() in people]
        maintainers = value.get("maintainers") or []
        records += 1
        c.source(url, status, 1, f"Fetched npm package {package_name}; registry repository URL exactly matches contributor-verified GitHub project {repo['full']}.")
        latest = value.get("dist-tags", {}).get("latest", "")
        work_date = ((value.get("time") or {}).get(latest) or "")[:10]
        work_id = identifier("npm-package", value.get("name") or package_name)
        package_url = f"https://www.npmjs.com/package/{quote(value.get('name') or package_name, safe='@/')}"
        c.add_work({
            "id": work_id,
            "title": value.get("name") or package_name,
            "kind": "npm package",
            "source_url": package_url,
            "observed_at": now(),
            "work_date": work_date,
            "organization": "",
            "regional_evidence": f"npm metadata's repository URL links to {repo['full']}; the separately fetched GitHub contributors response names regional account(s): " + " ".join(region_evidence(p) for p in local),
            "geography_scope": "; ".join(sorted({p["geography_scope"] for p in local})),
            "description": value.get("description") or "Public npm package whose registry metadata links to a repository with a regionally documented contributor.",
            "raw_path": raw_path,
            "raw_sha256": raw_sha,
        })
        for maintainer in maintainers:
            account = maintainer.get("username") or maintainer.get("name") or ""
            if not account:
                continue
            c.add_contribution({
                "work_id": work_id,
                "name": f"npm account @{account}",
                "role": "npm package maintainer (registry account)",
                "person_url": f"https://www.npmjs.com/~{quote(account, safe='')}",
                "identifiers": {"npm": account},
                "evidence": f"npm registry package metadata lists account @{account} as a maintainer; the account is not treated as a verified real-world identity.",
                "source_url": package_url,
            })
    c.sources.append({
        "url": "https://registry.npmjs.org/{package}",
        "status": "attempted",
        "records": records,
        "coverage": f"Looked up {len(candidates)} package names from explicit npm URLs on cached repositories that had a fetched regional GitHub contributor list.",
        "limitation": "Only registry maintainer accounts are named; no npm account is merged to a GitHub person without a direct identity link.",
    })


def collect_pypi(c: Collector, people: dict[str, dict], repos: list[dict], matched: dict[str, list[dict]], limit: int = 6) -> None:
    family = "pypi-packages"
    candidates = []
    for repo in repos:
        homepage = (repo.get("homepage") or "").strip()
        match = re.search(r"pypi\.org/project/([^/\s?#]+)", homepage, re.I)
        if match:
            candidates.append((repo, match.group(1)))
    candidates = candidates[:limit]
    seen: set[str] = set()
    records = 0
    for repo, package_name in candidates:
        package_name = package_name.lower()
        if package_name in seen or not c.can_request():
            continue
        seen.add(package_name)
        url = f"https://pypi.org/pypi/{quote(package_name, safe='')}/json"
        value, body, status, error = c.fetch(family, url)
        if body is None or not isinstance(value, dict):
            c.source(url, error, 0, f"One public PyPI JSON project lookup for {package_name}.", "The guessed package name may not exist; no inference from absence is made.")
            continue
        raw_path, raw_sha = c.save_raw("pypi-project", body)
        info = value.get("info") or {}
        urls = info.get("project_urls") or {}
        github_repos = {parse_github_repo(u) for u in urls.values()}
        github_repos.add(parse_github_repo(info.get("home_page") or ""))
        if repo["full"].lower() not in github_repos:
            c.source(url, status, 0, f"Fetched PyPI project JSON for {package_name}.", "Project metadata did not link back to the sampled GitHub repository, so no regional work was asserted.")
            continue
        if repo["full"].lower() not in matched:
            c.source(url, status, 0, f"Fetched PyPI project JSON for {package_name}; metadata links to {repo['full']}.", "The linked repository's fetched contributors page did not establish a regional contributor, so no regional package work was asserted.")
            continue
        regional = [people[u["login"].lower()] for u in matched[repo["full"].lower()] if u.get("login", "").lower() in people]
        records += 1
        c.source(url, status, 1, f"Fetched public metadata for {package_name}; its source/home page matches {repo['full']}.")
        release_dates = [f.get("upload_time_iso_8601", "") for files in (value.get("releases") or {}).values() for f in files]
        latest_date = max((d[:10] for d in release_dates if d), default="")
        work_id = identifier("pypi-package", value.get("info", {}).get("name") or package_name)
        c.add_work({
            "id": work_id,
            "title": info.get("name") or package_name,
            "kind": "PyPI package",
            "source_url": f"https://pypi.org/project/{quote(info.get('name') or package_name, safe='')}/",
            "observed_at": now(),
            "work_date": latest_date,
            "organization": "",
            "regional_evidence": f"PyPI metadata's project URLs link to {repo['full']}; its GitHub contributors response names regional account(s): " + " ".join(region_evidence(p) for p in regional),
            "geography_scope": "; ".join(sorted({p["geography_scope"] for p in regional})),
            "description": info.get("summary") or "Public Python package metadata linked to a GitHub repository with a regionally documented contributor.",
            "raw_path": raw_path,
            "raw_sha256": raw_sha,
        })
        maintainer = info.get("maintainer") or info.get("author") or ""
        if maintainer:
            c.add_contribution(pypi_author_credit(work_id, maintainer, url))
    c.sources.append({
        "url": "https://pypi.org/pypi/{project}/json",
        "status": "attempted",
        "records": records,
        "coverage": f"Looked up at most {limit} package names derived from repository homepage links/descriptions for repositories with a confirmed regional contributor.",
        "limitation": "PyPI has no complete public maintainer search in this adapter. A package is accepted only when its metadata links back to the same contributor-verified GitHub repository.",
    })


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []
    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)
    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(" ".join(self._text).split())))
            self._href = ""


class TeamProfileLinkParser(HTMLParser):
    """Capture GitHub profile links only inside explicit team/member markup."""
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.links: list[str] = []

    @staticmethod
    def _team_marker(attrs: list[tuple[str, str | None]]) -> bool:
        marker = " ".join(str(value or "") for key, value in attrs if key.lower() in {"id", "class", "aria-label", "data-role", "data-testid"})
        return bool(re.search(r"(?:^|[\s_-])(?:team|team-members?|members?|participants?|project-members|contributor-list)(?:$|[\s_-])", marker, re.I))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        team = self._team_marker(attrs)
        if tag == "a" and any(frame[1] for frame in self.stack) or tag == "a" and team:
            href = dict(attrs).get("href") or ""
            if re.fullmatch(r"https?://(?:www\.)?github\.com/[A-Za-z0-9-]+/?", href, re.I):
                self.links.append(href.rstrip("/"))
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, team))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def collect_huggingface(c: Collector, people: dict[str, dict], cached_bios: dict[str, str], limit: int = 6) -> None:
    family_names = ["huggingface-models", "huggingface-datasets", "huggingface-spaces"]
    # Only use account names for which cached GitHub profile metadata included an explicit HF profile URL.
    linked: list[tuple[dict, str]] = []
    for person in people.values():
        bio = cached_bios.get(person["login"].lower(), "")
        match = re.search(r"https?://(?:www\.)?huggingface\.co/([A-Za-z0-9_.-]+)", bio, re.I)
        if match:
            linked.append((person, match.group(1)))
    linked = linked[:limit]
    if not linked:
        # Still attempt one public discovery query in each required HF family; query matches are never treated as people.
        for family, kind in zip(family_names, ["models", "datasets", "spaces"]):
            url = f"https://huggingface.co/api/{kind}?{urlencode({'search': 'Buffalo', 'limit': 20})}"
            value, body, status, error = c.fetch(family, url)
            if body is None or not isinstance(value, list):
                c.source(url, error, 0, "One public search query for the term Buffalo.", "No regional tie inferred from search terms or names.")
                continue
            raw_path, raw_sha = c.save_raw(f"hf-{kind}", body)
            c.source(url, status, len(value), "One public API search for Buffalo-labeled " + kind + "; returned results were not linked to regional accounts without explicit profile evidence.", "No result is credited solely because its name or description matches Buffalo.")
        return
    for person, hf_user in linked:
        for family, kind in zip(family_names, ["models", "datasets", "spaces"]):
            if not c.can_request():
                break
            url = f"https://huggingface.co/api/{kind}?{urlencode({'author': hf_user, 'limit': 100})}"
            value, body, status, error = c.fetch(family, url)
            if body is None or not isinstance(value, list):
                c.source(url, error, 0, f"One public {kind} listing for linked Hugging Face profile {hf_user}.", "No response available.")
                continue
            raw_path, raw_sha = c.save_raw(f"hf-{kind}", body)
            count = 0
            for item in value:
                repo_id = item.get("id") or ""
                author = item.get("author") or ""
                if not repo_id or author.lower() != hf_user.lower():
                    continue
                count += 1
                source_url = f"https://huggingface.co/{repo_id}"
                work_id = identifier(f"huggingface-{kind}", repo_id)
                title = item.get("modelId") or repo_id
                c.add_work({
                    "id": work_id,
                    "title": title,
                    "kind": {"models": "Hugging Face model", "datasets": "Hugging Face dataset", "spaces": "Hugging Face Space"}[kind],
                    "source_url": source_url,
                    "observed_at": now(),
                    "work_date": (item.get("lastModified") or "")[:10],
                    "organization": "",
                    "regional_evidence": f"A GitHub profile with source-reported regional location includes a link to https://huggingface.co/{hf_user}; the Hugging Face API lists this work under that account. The link does not establish account ownership. {region_evidence(person)}",
                    "geography_scope": person["geography_scope"],
                    "description": f"Public Hugging Face {kind[:-1] if kind.endswith('s') else kind} repository {repo_id}; downloads={item.get('downloads', 0)}, likes={item.get('likes', 0)}.",
                    "raw_path": raw_path,
                    "raw_sha256": raw_sha,
                })
                c.add_contribution(huggingface_account_credit(work_id, hf_user, source_url))
            c.source(url, status, count, f"Fetched one page (up to 100) for linked profile {hf_user}; only exact author field matches retained.")


def collect_devpost(c: Collector, people: dict[str, dict], limit: int = 4) -> None:
    family = "devpost-hackathon-submissions"
    search_url = "https://devpost.com/software/search?" + urlencode({"query": "Buffalo"})
    value, body, status, error = c.fetch(family, search_url)
    if body is None:
        c.source(search_url, error, 0, "One public Devpost search request for Buffalo submissions.", "Search endpoint unavailable; no names inferred.")
        return
    raw_path, raw_sha = c.save_raw("devpost-search", body)
    page = LinkParser()
    try:
        page.feed(body.decode("utf-8", errors="replace"))
    except Exception:
        pass
    projects = []
    for href, label in page.links:
        if re.search(r"/software/[^/?#]+", href) and "devpost.com" in href and href not in [p[0] for p in projects]:
            projects.append((href, label))
    extracted = 0
    for href, label in projects[:limit]:
        if not c.can_request():
            break
        if href.startswith("/"):
            href = "https://devpost.com" + href
        detail, detail_body, detail_status, detail_error = c.fetch(family, href)
        if detail_body is None:
            c.source(href, detail_error, 0, "One public Devpost project page requested.", "Page unavailable or blocked.")
            continue
        detail_raw, detail_sha = c.save_raw("devpost-project", detail_body)
        parser = TeamProfileLinkParser()
        parser.feed(detail_body.decode("utf-8", errors="replace"))
        linked_people: list[tuple[dict, str]] = []
        for link in parser.links:
            match = re.search(r"github\.com/([A-Za-z0-9-]+)$", link, re.I)
            if match:
                handle = match.group(1)
                if handle.lower() in people:
                    linked_people.append((people[handle.lower()], link))
        if not linked_people:
            c.source(href, detail_status, 0, "Fetched one public Devpost project page.", "No explicit link to a seeded regional GitHub profile was found; name-only matches were not used.")
            continue
        extracted += 1
        work_id = identifier("devpost-project", href)
        desc_match = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', detail_body.decode("utf-8", errors="replace"), re.I)
        description = html.unescape(desc_match.group(1)) if desc_match else "Public Devpost hackathon submission with linked regional developer profile(s)."
        title = html.unescape(label.strip()) or href.rsplit("/", 1)[-1]
        c.add_work({
            "id": work_id,
            "title": title,
            "kind": "public hackathon submission",
            "source_url": href,
            "observed_at": now(),
            "work_date": "",
            "organization": "",
            "regional_evidence": "Devpost submission page contains an explicit GitHub profile link matching a cached regional GitHub profile: " + " ".join(region_evidence(p) for p, _ in linked_people),
            "geography_scope": "; ".join(sorted({p["geography_scope"] for p, _ in linked_people})),
            "description": description,
            "raw_path": detail_raw,
            "raw_sha256": detail_sha,
        })
        for person, profile_link in linked_people:
            c.add_contribution({
                "work_id": work_id,
                "name": person["name"],
                "role": "team member (Devpost project page links to this GitHub account)",
                "person_url": person["person_url"],
                "identifiers": {"github": person["login"]},
                "evidence": f"Public project page includes explicit profile link {profile_link}; this lane does not infer organizer or attendee status.",
                "source_url": href,
            })
    if status == 202 and b"awsWaf" in body:
        limitation = "Devpost returned an AWS WAF challenge page (HTTP 202); no project links could be extracted and no access-control bypass was attempted."
    else:
        limitation = "Search result coverage and pagination are opaque; only exact GitHub profile links inside explicit team-member markup count."
    c.source(search_url, status, extracted, f"Fetched one Devpost search page and examined up to {limit} project pages.", limitation)


def load_bios(seed_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted((seed_dir / "raw").glob("graphql-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for user in ((payload.get("data") or {}).get("nodes") or []):
            if user and user.get("login"):
                result[user["login"].lower()] = user.get("bio") or ""
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO / "data/work-discovery/2026-09-28/software_work")
    parser.add_argument("--seed-dir", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--enrichment-db", type=Path, default=DEFAULT_ENRICHMENT)
    parser.add_argument("--budget", type=int, default=140, help="Maximum network requests (hard ceiling 145).")
    parser.add_argument("--deadline-seconds", type=int, default=780)
    parser.add_argument("--event-accounts", type=int, default=15)
    parser.add_argument("--repositories", type=int, default=55)
    parser.add_argument("--release-repositories", type=int, default=35)
    parser.add_argument("--npm-packages", type=int, default=5)
    args = parser.parse_args()

    people, regional_rows = load_regional(args.seed_dir, args.enrichment_db, args.output)
    repos = load_repositories(args.seed_dir, people)
    bios = load_bios(args.seed_dir)
    profile_counts: dict[str, int] = {}
    for path in sorted((args.seed_dir / "raw").glob("graphql-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for user in ((payload.get("data") or {}).get("nodes") or []):
            if user and user.get("login"):
                profile_counts[user["login"].lower()] = int(((user.get("repositories") or {}).get("totalCount")) or 0)

    c = Collector(args.output, args.budget, args.deadline_seconds)
    collect_events(c, people, profile_counts, args.event_accounts)
    matched = collect_contributors_and_releases(c, people, repos, args.repositories, args.release_repositories)
    collect_npm(c, people, repos, matched, args.npm_packages)
    collect_pypi(c, people, repos, matched)
    collect_huggingface(c, people, bios)
    collect_devpost(c, people)
    limitations = [
        f"Seed identities are public GitHub profiles collected on 2026-09-25; {len(regional_rows)} profiles with source-reported Buffalo/WNY or Rochester scope were available. GitHub locations are user supplied and may be stale.",
        "The work set is bounded by request and time ceilings. GitHub events expose a short recent public window; contributor endpoint counts do not expose all underlying commits.",
        "No repository owner was credited as author unless a separate contributor, release-author, pull-request-author, or event field named that account.",
        "Software package, Hugging Face, and hackathon sources require explicit public metadata links before cross-source regional identity or project ties are accepted; unsupported or ambiguous account matches remain unlinked.",
    ]
    if c.requests >= c.budget:
        limitations.append("Configured network request budget was reached.")
    if time.monotonic() >= c.deadline:
        limitations.append("Configured time budget was reached.")
    c.finish("software_work", "Public GitHub contributions/releases, package maintainer metadata, Hugging Face models/datasets/Spaces, and public Devpost hackathon submissions linked to documented regional developer accounts.", limitations)
    print(json.dumps({"requests": c.requests, "works": len(c.works), "contributions": len(c.contributions), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
