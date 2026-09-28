"""Consumer-owned saved discovery queries, durable result changes, and enrichment planning."""
from contextlib import contextmanager
import json
import re
from pathlib import Path
import sqlite3
import time
import uuid
from .datasets import canonical, digest
from .enrichment import endpoint
from .workspaces import discover


@contextmanager
def read_snapshot(connection):
    connection.execute('SAVEPOINT subscription_read')
    try:
        yield
    finally:
        connection.execute('RELEASE subscription_read')


def validate_query(query):
    if not isinstance(query, dict) or set(query) - {'query', 'collector', 'mode', 'institutions'}:
        raise ValueError('Query accepts query, collector, mode, and institutions only')
    text = query.get('query', '')
    collector = query.get('collector')
    mode = query.get('mode', 'all')
    institutions = query.get('institutions', [])
    if not isinstance(text, str) or len(text) > 500 or (collector is not None and not isinstance(collector, str)):
        raise ValueError('Invalid query text or collector')
    if mode not in ('all', 'new', 'shortlisted', 'dismissed') or not isinstance(institutions, list):
        raise ValueError('Invalid mode or institution list')
    for institution in institutions:
        if not isinstance(institution, str):
            raise ValueError('Institution IDs must be strings')
        endpoint('openalex-institution', institution)
    if not text.strip() and not collector and not institutions:
        raise ValueError('Specify a query, collector, or origin institutions')
    return {'query': text.strip(), 'collector': collector, 'mode': mode, 'institutions': sorted(set(institutions))}


class Subscriptions:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,consumer TEXT NOT NULL,name TEXT NOT NULL,query TEXT NOT NULL,paused INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS members(subscription TEXT,profile_id TEXT,fingerprint TEXT,payload TEXT,PRIMARY KEY(subscription,profile_id));
        CREATE TABLE IF NOT EXISTS notifications(sequence INTEGER PRIMARY KEY,subscription TEXT NOT NULL,consumer TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,subscription TEXT NOT NULL,created REAL NOT NULL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS notification_consumer ON notifications(consumer,sequence);
        ''')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def create(self, consumer, name, query):
        if any(not isinstance(v, str) or not 1 <= len(v.strip()) <= 100 for v in (consumer, name)):
            raise ValueError('Consumer and name must be 1–100 characters')
        value = {'id': uuid.uuid4().hex, 'consumer': consumer, 'name': name.strip(), 'query': validate_query(query)}
        with self.db:
            self.db.execute('INSERT INTO subscriptions(id,consumer,name,query) VALUES(?,?,?,?)', (value['id'], consumer, value['name'], canonical(value['query'])))
        return value

    def get(self, consumer, subscription):
        row = self.db.execute('SELECT * FROM subscriptions WHERE id=? AND consumer=?', (subscription, consumer)).fetchone()
        if row is None:
            raise KeyError(subscription)
        return {**dict(row), 'query': json.loads(row['query']), 'paused': bool(row['paused'])}

    def list(self, consumer):
        return [self.get(consumer, r[0]) for r in self.db.execute('SELECT id FROM subscriptions WHERE consumer=? ORDER BY rowid', (consumer,))]

    def pause(self, consumer, subscription, paused):
        self.get(consumer, subscription)
        if type(paused) is not bool:
            raise ValueError('paused must be boolean')
        with self.db:
            self.db.execute('UPDATE subscriptions SET paused=? WHERE id=?', (paused, subscription))
        return self.get(consumer, subscription)

    def evaluate(self, consumer, subscription, store, evidence, annotations=None):
        if annotations is not None and (not isinstance(annotations, dict) or any(not isinstance(value, dict) for value in annotations.values())):
            raise ValueError('Annotations must map profile IDs to objects')
        # Serialize evaluations so concurrent runners cannot publish duplicate membership transitions.
        with self.db, read_snapshot(store.connection), read_snapshot(evidence.db):
            self.db.execute('BEGIN IMMEDIATE')
            saved = self.get(consumer, subscription)
            if saved['paused']:
                return {'subscription': subscription, 'paused': True, 'emitted': 0}
            q = saved['query']
            if q['mode'] != 'all' and annotations is None:
                raise ValueError('This query requires an explicit consumer annotation mapping')
            result = discover(store, evidence, annotations or {}, q['query'], q['collector'], q['mode'], limit=100000)
            if result['total'] > 100000:
                raise ValueError('Subscription exceeds 100000 records; narrow the query')
            transitions = {}
            if q['institutions']:
                for item in evidence.transitions(q['institutions']):
                    for row in evidence.db.execute("SELECT profile_id FROM links WHERE provider='openalex-author' AND subject=?", (item['subject'],)):
                        transitions.setdefault(row[0], []).append(item)
            current = {}
            for item in result['profiles']:
                if q['institutions'] and item['id'] not in transitions:
                    continue
                item['movement_candidates'] = transitions.get(item['id'], [])
                # Re-fetch timestamps alone must not generate duplicate notifications.
                semantic = {k: v for k,v in item.items() if k != 'record'}
                semantic['record'] = {k:v for k,v in item['record'].items() if k != 'observed_at'}
                semantic['enriched_matches'] = sorted([{k:v for k,v in c.items() if k != 'observed'} for c in item['enriched_matches']], key=canonical)
                semantic['workspace'] = {k:v for k,v in item['workspace'].items() if k != 'updated_at'}
                semantic['movement_candidates'] = [{k:v for k,v in c.items() if k != 'source'} for c in item['movement_candidates']]
                current[item['id']] = (digest(semantic), item)
            previous = {r['profile_id']: r for r in self.db.execute('SELECT * FROM members WHERE subscription=?', (subscription,))}
            run_id, emitted = uuid.uuid4().hex, 0
            for profile_id in sorted(previous.keys() | current.keys()):
                old, new = previous.get(profile_id), current.get(profile_id)
                if old and new and old['fingerprint'] == new[0]:
                    # Refresh provenance without emitting a content-change event.
                    self.db.execute('UPDATE members SET payload=? WHERE subscription=? AND profile_id=?', (canonical(new[1]), subscription, profile_id))
                    continue
                kind = 'entered' if old is None else 'exited' if new is None else 'updated'
                event = {'run_id': run_id, 'subscription': subscription, 'kind': kind, 'profile_id': profile_id,
                         'before': json.loads(old['payload']) if old else None, 'after': new[1] if new else None,
                         'reason': 'Qualification under the saved query changed; not proof of a real-world move or departure.', 'query': q}
                self.db.execute('INSERT INTO notifications(subscription,consumer,payload) VALUES(?,?,?)', (subscription, consumer, canonical(event)))
                emitted += 1
                if new:
                    self.db.execute('INSERT INTO members VALUES(?,?,?,?) ON CONFLICT(subscription,profile_id) DO UPDATE SET fingerprint=excluded.fingerprint,payload=excluded.payload', (subscription, profile_id, new[0], canonical(new[1])))
                else:
                    self.db.execute('DELETE FROM members WHERE subscription=? AND profile_id=?', (subscription, profile_id))
            report = {'run_id': run_id, 'subscription': subscription, 'matched': len(current), 'emitted': emitted,
                      'source_snapshot': store.stats()['snapshot'], 'evidence_response_high_water': evidence.db.execute('SELECT coalesce(max(id),0) FROM responses').fetchone()[0]}
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?)', (run_id, subscription, time.time(), canonical(report)))
        return report

    def events(self, consumer, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Use nonnegative cursor and limit 1–1000')
        rows = self.db.execute('SELECT sequence,payload FROM notifications WHERE consumer=? AND sequence>? ORDER BY sequence LIMIT ?', (consumer, after, limit)).fetchall()
        cursor = rows[-1]['sequence'] if rows else after
        return {'events': [{'sequence': r['sequence'], **json.loads(r['payload'])} for r in rows], 'cursor': cursor,
                'has_more': bool(self.db.execute('SELECT 1 FROM notifications WHERE consumer=? AND sequence>?', (consumer, cursor)).fetchone())}

    def plan(self, consumer, subscription, store, evidence, limit=25, apply=False):
        """Queue missing identifiers in the relevant cohort; never bypass cooldowns or refresh schedules."""
        if not 1 <= limit <= 1000 or type(apply) is not bool:
            raise ValueError('Use limit 1–1000 and a boolean apply flag')
        saved = self.get(consumer, subscription)
        if saved['paused']:
            return {'paused': True, 'tasks': [], 'queued': 0}
        q = saved['query']
        # Movement subscriptions need enrichment before they can qualify; start from their origin cohort.
        records = store.connection.execute('SELECT id,payload FROM profiles WHERE (? IS NULL OR collector=?) ORDER BY id', (q['collector'], q['collector']))
        tasks, seen, missing, invalid = [], set(), 0, 0
        qualifying = None
        if not q['institutions']:
            candidates = discover(store, evidence, {}, q['query'], q['collector'], limit=100000)
            qualifying = {r['id'] for r in candidates['profiles']}
        for row in records:
            profile = json.loads(row['payload'])
            if qualifying is not None and row['id'] not in qualifying:
                continue
            if q['institutions'] and not set(q['institutions']).intersection(re.findall(r'\bI\d+\b', canonical(profile))):
                continue
            try:
                ids = json.loads(profile.get('external_ids') or '{}')
                if not isinstance(ids, dict):
                    raise ValueError('Invalid identifiers')
                sources = []
                if ids.get('openalex'):
                    sources.append(('openalex-author', ids['openalex'].rsplit('/', 1)[-1]))
                if ids.get('github'):
                    sources.extend((p, ids['github'].casefold()) for p in ('github-profile', 'github-repos'))
                if not sources:
                    missing += 1
                for provider, subject in sources:
                    endpoint(provider, subject)
                    if (provider,subject) in seen:
                        continue
                    seen.add((provider,subject))
                    job = evidence.db.execute('SELECT status,due,error FROM jobs WHERE provider=? AND subject=?', (provider,subject)).fetchone()
                    linked = evidence.db.execute('SELECT 1 FROM links WHERE profile_id=? AND provider=? AND subject=?', (row['id'],provider,subject)).fetchone()
                    if job and linked and (job['status'] != 'pending' or job['due'] > time.time()):
                        continue
                    tasks.append({'profile_id': row['id'], 'provider': provider, 'subject': subject, 'reason': 'Existing eligible job in the subscription cohort' if job else 'Origin cohort needs affiliation evidence' if q['institutions'] else 'Matching source record has no enrichment job'})
                    if len(tasks) >= limit:
                        break
            except (ValueError, TypeError, AttributeError):
                invalid += 1
            if len(tasks) >= limit:
                break
        queued = sum(evidence.enqueue(t['provider'], t['subject'], t['profile_id']) for t in tasks) if apply else 0
        return {'subscription': subscription, 'tasks': tasks, 'queued': queued, 'records_without_supported_ids': missing, 'invalid_identifier_records': invalid,
                'scope': 'Bounded existing-record cohort; does not discover people outside the current collection. Existing jobs keep their due times and failure state.'}
