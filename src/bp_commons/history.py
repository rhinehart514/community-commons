"""Transactional, retained history for complete regional-talent export refreshes."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid

from .importer import build_snapshot, dump

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions(snapshot_id TEXT NOT NULL REFERENCES snapshots(id), kind TEXT NOT NULL,
    position INTEGER NOT NULL, profile_id TEXT, payload TEXT NOT NULL,
    PRIMARY KEY(snapshot_id,kind,position));
CREATE INDEX IF NOT EXISTS versions_profile ON versions(snapshot_id,kind,profile_id);
CREATE TABLE IF NOT EXISTS changes(snapshot_id TEXT NOT NULL REFERENCES snapshots(id), kind TEXT NOT NULL,
    record_key TEXT NOT NULL, change_type TEXT NOT NULL, before_payload TEXT NOT NULL, after_payload TEXT NOT NULL,
    PRIMARY KEY(snapshot_id,kind,record_key));
"""
KINDS = ('profiles', 'observations', 'relationships')


def capture(db):
    result = {}
    for kind in KINDS:
        column = 'id' if kind == 'profiles' else 'profile_id'
        result[kind] = [(row[0], json.loads(row[1])) for row in db.execute(
            f'SELECT {column},payload FROM {kind} ORDER BY id')]
    return result


def record_key(kind, row):
    if kind == 'profiles':
        identity = [row['record_id']]
    elif kind == 'observations':
        identity = [row['record_id'], row.get('profile_url'), row['source_url'], row.get('collector')]
    else:
        identity = [row.get('subject_url') or row['subject_name'], row['relation'],
                    row.get('object_url') or row.get('object_name'), row['source_url'], row.get('evidence_date')]
    return hashlib.sha256(dump(identity).encode()).hexdigest()


def grouped(kind, rows):
    groups = defaultdict(list)
    for profile_id, row in rows:
        groups[record_key(kind, row)].append({'profile_id': profile_id, 'record': row})
    return groups


def comparable(items):
    # Acquisition time alone is not a change to the underlying evidence.
    return sorted(dump({'profile_id': item['profile_id'], 'record': {
        k: v for k, v in item['record'].items() if k != 'observed_at'}}) for item in items)


def save_version(db, records, metadata):
    snapshot_id = uuid.uuid4().hex
    db.execute('INSERT INTO snapshots VALUES(?,?,?)',
               (snapshot_id, datetime.now(timezone.utc).isoformat(), dump(metadata)))
    for kind, rows in records.items():
        db.executemany('INSERT INTO versions VALUES(?,?,?,?,?)',
            ((snapshot_id, kind, pos, profile_id, dump(row))
             for pos, (profile_id, row) in enumerate(rows)))
    return snapshot_id


def refresh_snapshot(source, database):
    """Refresh a complete export. Missing rows mean absent from this export only."""
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.bp-refresh-', dir=database.parent)
    os.close(fd)
    candidate = Path(temporary)
    old = new = None
    try:
        metadata = build_snapshot(source, candidate)
        new = sqlite3.connect(candidate)
        new.executescript(HISTORY_SCHEMA)
        previous = {kind: [] for kind in KINDS}
        if database.exists():
            old = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
            previous_metadata = json.loads(old.execute('SELECT payload FROM metadata').fetchone()[0])
            if previous_metadata['files'] == metadata['files']:
                return {'status': 'unchanged', 'snapshot_id': previous_metadata['snapshot_id'], 'changes': {}}
            previous = capture(old)
            # Copy retained history into the validated candidate; never mutate the live database.
            for table, width in [('snapshots', 3), ('versions', 5), ('changes', 6)]:
                new.executemany(f"INSERT INTO {table} VALUES({','.join('?' for _ in range(width))})",
                                old.execute(f'SELECT * FROM {table}'))
        current = capture(new)
        snapshot_id = save_version(new, current, metadata)
        counts = {}
        for kind in KINDS:
            before, after = grouped(kind, previous[kind]), grouped(kind, current[kind])
            counts[kind] = {'added': 0, 'changed': 0, 'removed': 0}
            for key in sorted(before.keys() | after.keys()):
                left, right = before.get(key, []), after.get(key, [])
                if comparable(left) == comparable(right):
                    continue
                change = 'added' if not left else 'removed' if not right else 'changed'
                counts[kind][change] += 1
                new.execute('INSERT INTO changes VALUES(?,?,?,?,?,?)',
                            (snapshot_id, kind, key, change, dump(left), dump(right)))
        metadata['snapshot_id'] = snapshot_id
        new.execute('UPDATE metadata SET payload=?', (dump(metadata),))
        new.commit()
        if new.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('History integrity check failed')
        new.close()
        new = None
        if old:
            old.close()
            old = None
        os.replace(candidate, database)
        return {'status': 'refreshed', 'snapshot_id': snapshot_id, 'changes': counts}
    finally:
        if old:
            old.close()
        if new:
            new.close()
        candidate.unlink(missing_ok=True)
