"""Durable, bounded public-evidence worker. No identity merges or residence inference."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .datasets import canonical
from .store import Commons

SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, provider TEXT NOT NULL, subject TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 due REAL NOT NULL DEFAULT 0, lease REAL NOT NULL DEFAULT 0, error TEXT,
 UNIQUE(provider,subject));
CREATE TABLE IF NOT EXISTS links (profile_id TEXT,provider TEXT,subject TEXT,
 PRIMARY KEY(profile_id,provider,subject));
CREATE TABLE IF NOT EXISTS responses (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, observed REAL NOT NULL,
 url TEXT NOT NULL, sha256 TEXT NOT NULL, raw BLOB NOT NULL, parser_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
 response_id INTEGER NOT NULL REFERENCES responses(id), subject TEXT NOT NULL,
 predicate TEXT NOT NULL, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS responses_job ON responses(job_id,id);
CREATE INDEX IF NOT EXISTS claims_response ON claims(response_id);
CREATE INDEX IF NOT EXISTS claims_subject ON claims(subject,predicate);
'''
PROVIDERS = ('openalex-author', 'openalex-institution', 'github-profile', 'github-repos')


def endpoint(provider, subject):
    if provider in ('openalex-author', 'openalex-institution'):
        prefix = 'A' if provider == 'openalex-author' else 'I'
        if not re.fullmatch(prefix + r'\d+', subject):
            raise ValueError('Invalid OpenAlex identifier')
        return f'https://api.openalex.org/{"authors" if prefix == "A" else "institutions"}/{subject}'
    if provider in ('github-profile', 'github-repos') and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})', subject):
        return f'https://api.github.com/users/{subject}' + ('/repos?type=owner&sort=pushed&per_page=100' if provider == 'github-repos' else '')
    raise ValueError('Unsupported provider or identifier')


def fetch(provider, subject):
    url = endpoint(provider, subject)
    headers = {'Accept': 'application/json', 'User-Agent': 'Community-Commons/0.2'}
    token = os.environ.get('OPENALEX_API_KEY' if provider.startswith('openalex') else 'GITHUB_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with urlopen(Request(url, headers=headers), timeout=25) as response:
        raw = response.read(10_000_001)
    if len(raw) > 10_000_000:
        raise ValueError('Response exceeds 10MB')
    return raw


def extract(provider, subject, payload):
    """Typed claims retain provider semantics, dates, and links to original artifacts."""
    claims, followups = [], []
    def add(predicate, value):
        claims.append((predicate, value))
    if provider == 'openalex-author':
        if not isinstance(payload, dict) or payload.get('id', '').rsplit('/', 1)[-1] != subject:
            raise ValueError('Author identity mismatch')
        add('name', payload.get('display_name'))
        if payload.get('orcid'):
            add('provider_orcid', payload['orcid'])
        for affiliation in (payload.get('affiliations') or []):
            institution = affiliation['institution']
            institution_id = institution['id'].rsplit('/', 1)[-1]
            add('publication_affiliation', {'institution_id': institution_id, 'name': institution.get('display_name'), 'years': affiliation.get('years', [])})
        for institution in (payload.get('last_known_institutions') or []):
            institution_id = institution['id'].rsplit('/', 1)[-1]
            add('latest_publication_institution', {'institution_id': institution_id, 'name': institution.get('display_name')})
            followups.append(('openalex-institution', institution_id))
        for topic in (payload.get('topics') or []):
            add('research_topic', {'name': topic.get('display_name'), 'id': topic.get('id'), 'work_count': topic.get('count'), 'basis': 'Provider-assigned publication topic; not a verified skill'})
    elif provider == 'openalex-institution':
        if not isinstance(payload, dict) or payload.get('id', '').rsplit('/', 1)[-1] != subject:
            raise ValueError('Institution identity mismatch')
        add('institution_location', {'name': payload.get('display_name'), 'ror': payload.get('ror'), 'geo': payload.get('geo', {})})
    elif provider == 'github-profile':
        if not isinstance(payload, dict) or payload.get('login', '').casefold() != subject.casefold():
            raise ValueError('GitHub identity mismatch; renamed accounts require review')
        for field in ('name', 'bio', 'company', 'location', 'blog'):
            if payload.get(field):
                add('self_reported_' + field, payload[field])
        add('github_id', payload['id'])
    else:
        if not isinstance(payload, list):
            raise ValueError('Expected public repository list')
        add('repository_coverage', {'limit': 100, 'received': len(payload), 'complete': len(payload) < 100, 'scope': 'Most recently pushed owned public repositories'})
        for repo in payload:
            if repo.get('owner', {}).get('login', '').casefold() != subject.casefold():
                raise ValueError('Repository owner mismatch')
            if repo.get('fork'):
                continue
            add('public_repository', {k: repo.get(k) for k in ('html_url', 'name', 'description', 'language', 'topics', 'pushed_at', 'archived')})
    return claims, followups


class Enrichment:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def enqueue(self, provider, subject, profile_id=None):
        endpoint(provider, subject)
        if provider.startswith('github'):
            subject = subject.casefold()
        with self.db:
            added = self.db.execute('INSERT OR IGNORE INTO jobs(provider,subject) VALUES(?,?)', (provider, subject)).rowcount
            if profile_id:
                self.db.execute('INSERT OR IGNORE INTO links VALUES(?,?,?)', (profile_id, provider, subject))
        return added

    def seed(self, database, limit=100):
        if not 1 <= limit <= 100000:
            raise ValueError('Seed limit must be 1–100000')
        added = skipped = 0
        with Commons(database) as source:
            # Iterate past already-queued records so later calls make progress.
            for row in source.connection.execute('SELECT id,payload FROM profiles ORDER BY id'):
                profile = json.loads(row['payload'])
                try:
                    ids = json.loads(profile.get('external_ids') or '{}')
                    if not isinstance(ids, dict):
                        raise ValueError('Invalid identifier object')
                    tasks = []
                    if ids.get('openalex'):
                        tasks.append(('openalex-author', ids['openalex'].rsplit('/', 1)[-1]))
                    if ids.get('github'):
                        tasks.extend((p, ids['github']) for p in ('github-profile', 'github-repos'))
                    for provider, subject in tasks:
                        if added >= limit:
                            return {'queued': added, 'invalid_identifiers': skipped}
                        added += self.enqueue(provider, subject, row['id'])
                except (ValueError, TypeError, AttributeError):
                    skipped += 1
        return {'queued': added, 'invalid_identifiers': skipped}

    def claim_job(self, now, targets=None):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            # A dead worker's lease eventually becomes eligible again.
            self.db.execute("UPDATE jobs SET status='pending' WHERE status='running' AND lease<=?", (now,))
            self.db.execute("UPDATE jobs SET status='failed',error='LeaseAttemptsExhausted' WHERE status='pending' AND attempts>=5")
            scope, parameters = '', [now]
            if targets is not None:
                if not targets:
                    return None
                if len(targets) > 1000:
                    raise ValueError('At most 1000 target jobs per run')
                scope = " AND (provider || ':' || subject) IN (" + ','.join('?' for _ in targets) + ')'
                parameters.extend(provider + ':' + subject for provider,subject in targets)
            row = self.db.execute("SELECT * FROM jobs WHERE status='pending' AND due<=? AND attempts<5" + scope + " ORDER BY id LIMIT 1", parameters).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE jobs SET status='running',lease=?,attempts=attempts+1 WHERE id=?", (now + 120, row['id']))
            return dict(row)

    def step(self, fetcher=fetch, now=None, refresh_days=30, targets=None):
        now = time.time() if now is None else now
        if refresh_days < 1:
            raise ValueError('Refresh interval must be at least one day')
        job = self.claim_job(now, targets)
        if job is None:
            return None
        try:
            raw = fetcher(job['provider'], job['subject'])
            # Preserve even an invalid provider response for inspection.
            with self.db:
                response_id = self.db.execute('INSERT INTO responses(job_id,observed,url,sha256,raw,parser_version) VALUES(?,?,?,?,?,?)', (job['id'], now, endpoint(job['provider'], job['subject']), hashlib.sha256(raw).hexdigest(), raw, '1')).lastrowid
            claims, followups = extract(job['provider'], job['subject'], json.loads(raw))
            for provider, subject in followups:
                endpoint(provider, subject)
            with self.db:
                for provider, subject in followups:
                    self.db.execute('INSERT OR IGNORE INTO jobs(provider,subject) VALUES(?,?)', (provider, subject))
                for predicate, value in claims:
                    self.db.execute('INSERT INTO claims VALUES(?,?,?,?)', (response_id, job['subject'], predicate, canonical(value)))
                self.db.execute("UPDATE jobs SET status='pending',attempts=0,due=?,lease=0,error=NULL WHERE id=?", (now + refresh_days * 86400, job['id']))
            return {'job_id': job['id'], 'status': 'fetched', 'claims': len(claims)}
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
            attempt = job['attempts'] + 1
            status = 'failed' if attempt >= 5 else 'pending'
            delay = min(86400, 60 * 2 ** attempt)
            # Rate-limit/auth failures pause subsequent requests in this process.
            throttled = isinstance(error, HTTPError) and error.code in (401, 403, 429)
            if isinstance(error, HTTPError):
                if error.code in (400, 404, 410, 422):
                    status = 'failed'
                try:
                    delay = max(delay, float(error.headers.get('Retry-After', 0)), float(error.headers.get('X-RateLimit-Reset', 0)) - now)
                except (ValueError, TypeError):
                    pass
            # Never store exception text: provider URLs can contain credentials.
            message = f'HTTP {error.code}' if isinstance(error, HTTPError) else type(error).__name__
            with self.db:
                self.db.execute('UPDATE jobs SET status=?,due=?,lease=0,error=? WHERE id=?', (status, now + delay, message, job['id']))
                if throttled:
                    prefix = 'openalex%' if job['provider'].startswith('openalex') else 'github%'
                    self.db.execute("UPDATE jobs SET due=max(due,?) WHERE provider LIKE ? AND status='pending'", (now + delay, prefix))
            if isinstance(error, HTTPError):
                error.close()
            return {'job_id': job['id'], 'status': status, 'error': message, 'stop': throttled}

    def run(self, max_requests=10, max_seconds=300, interval=2, watch=False, targets=None):
        if not 1 <= max_requests <= 100000 or not 1 <= max_seconds <= 86400 or interval < 1:
            raise ValueError('Use 1–100000 requests, 1–86400 seconds, and interval >=1 second')
        deadline = time.monotonic() + max_seconds
        results = []
        while len(results) < max_requests and time.monotonic() < deadline:
            result = self.step(targets=targets)
            if result is None and not watch:
                break
            if result:
                results.append(result)
                if result.get('stop'):
                    break
            if len(results) < max_requests:
                time.sleep(min(interval, max(0, deadline - time.monotonic())))
        return {'requests_attempted': len(results), 'results': results, 'stats': self.stats()}

    def stats(self):
        return {'jobs': {r[0]: r[1] for r in self.db.execute('SELECT status,count(*) FROM jobs GROUP BY status')}, 'due': self.db.execute("SELECT count(*) FROM jobs WHERE status='pending' AND due<=?", (time.time(),)).fetchone()[0], 'responses': self.db.execute('SELECT count(*) FROM responses').fetchone()[0], 'claims': self.db.execute('SELECT count(*) FROM claims').fetchone()[0], 'errors': [dict(r) for r in self.db.execute('SELECT provider,subject,status,error FROM jobs WHERE error IS NOT NULL ORDER BY id LIMIT 20')]}

    def claims(self, query='', limit=50, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError('Invalid pagination')
        # Latest successful extraction per job; raw failures never erase valid claims.
        sql = '''FROM claims c JOIN responses r ON r.id=c.response_id
                 WHERE r.id=(SELECT max(r2.id) FROM responses r2 WHERE r2.job_id=r.job_id
                 AND EXISTS(SELECT 1 FROM claims c2 WHERE c2.response_id=r2.id))
                 AND (?='' OR instr(lower(c.value),lower(?))>0 OR instr(lower(c.subject),lower(?))>0)'''
        args = (query, query, query)
        total = self.db.execute('SELECT count(*) ' + sql, args).fetchone()[0]
        rows = self.db.execute('SELECT c.*,r.url,r.observed,r.sha256 ' + sql + ' ORDER BY r.id DESC,c.rowid LIMIT ? OFFSET ?', (*args, limit, offset))
        return {'total': total, 'claims': [{**dict(r), 'value': json.loads(r['value'])} for r in rows]}

    def transitions(self, institution_ids):
        """Candidates with a strictly later publication affiliation outside a supplied set.

        This is institutional change, not a relocation or current employment claim.
        """
        origins = set(institution_ids)
        if not origins or any(not re.fullmatch(r'I\d+', i) for i in origins):
            raise ValueError('Supply at least one origin OpenAlex institution ID')
        result = []
        for job in self.db.execute("SELECT id,subject FROM jobs WHERE provider='openalex-author'"):
            row = self.db.execute('SELECT max(r.id) FROM responses r WHERE job_id=? AND EXISTS(SELECT 1 FROM claims c WHERE c.response_id=r.id)', (job['id'],)).fetchone()
            claims = [(r[0], json.loads(r[1])) for r in self.db.execute('SELECT predicate,value FROM claims WHERE response_id=?', (row[0],))]
            affiliations = [v for p, v in claims if p == 'publication_affiliation']
            prior = [v for v in affiliations if v['institution_id'] in origins and v['years']]
            if not prior:
                continue
            last_origin = max(y for v in prior for y in v['years'])
            latest = [v for p,v in claims if p == 'latest_publication_institution']
            if any(v['institution_id'] in origins for v in latest):
                continue
            later = [v for v in affiliations if v['institution_id'] in {i['institution_id'] for i in latest} and v['years'] and max(v['years']) > last_origin]
            if later:
                source = self.db.execute('SELECT url,observed,sha256 FROM responses WHERE id=?', (row[0],)).fetchone()
                result.append({'subject': job['subject'], 'name': next((v for p,v in claims if p == 'name'), job['subject']), 'status': 'later_affiliation_candidate', 'prior': prior, 'later': later, 'source': dict(source), 'limitation': 'Later publication affiliation outside the supplied institution set; not proof of leaving a city, residence, or current employment.'})
        return result
