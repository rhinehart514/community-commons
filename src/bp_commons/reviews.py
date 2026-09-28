"""Separate reconciliation audit store: evidence refresh cannot erase human decisions."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid
from .datasets import canonical, digest, validate_dataset
from .reconcile import reconcile

SCHEMA = '''
CREATE TABLE IF NOT EXISTS datasets(digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
    left_digest TEXT NOT NULL REFERENCES datasets(digest), right_digest TEXT NOT NULL REFERENCES datasets(digest), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL);
'''


class Reconciliation:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def compare(self, left, right):
        validate_dataset(left)
        validate_dataset(right)
        reviews = [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM reviews ORDER BY sequence')]
        result = reconcile(left, right, reviews)
        run_id = uuid.uuid4().hex
        result['run_id'] = run_id
        with self.db:
            for data in (left, right):
                self.db.execute('INSERT OR IGNORE INTO datasets VALUES(?,?)', (digest(data), canonical(data)))
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',
                            (run_id, datetime.now(timezone.utc).isoformat(), digest(left), digest(right), canonical(result)))
        return result

    def run(self, run_id):
        row = self.db.execute('SELECT payload FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return json.loads(row[0])

    def pair(self, run_id, left_id, right_id):
        row = self.db.execute('SELECT left_digest,right_digest FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        left, right = [json.loads(self.db.execute('SELECT payload FROM datasets WHERE digest=?', (d,)).fetchone()[0]) for d in row]
        a = next((r for r in left['records'] if r['record_id'] == left_id), None)
        b = next((r for r in right['records'] if r['record_id'] == right_id), None)
        if a is None or b is None:
            raise KeyError('Records must exist in the specified run')
        return {'left_dataset': left['dataset_id'], 'right_dataset': right['dataset_id'], 'left': a, 'right': b,
                'coverage': {'left': left['coverage'], 'right': right['coverage']}}

    def review(self, run_id, left_id, right_id, verdict, reviewer, reason):
        if verdict not in ('same', 'different', 'unsure') or not reviewer.strip() or not reason.strip():
            raise ValueError('Review requires verdict same/different/unsure, reviewer, and reason')
        pair = self.pair(run_id, left_id, right_id)
        a, b = pair['left'], pair['right']
        value = {'review_id': uuid.uuid4().hex, 'run_id': run_id,
                 'left': [pair['left_dataset'], left_id], 'right': [pair['right_dataset'], right_id],
                 'left_fingerprint': digest(a), 'right_fingerprint': digest(b),
                 'verdict': verdict, 'reviewer': reviewer, 'reason': reason,
                 'created_at': datetime.now(timezone.utc).isoformat()}
        with self.db:
            self.db.execute('INSERT INTO reviews(payload) VALUES(?)', (canonical(value),))
        return value

    def dataset(self, fingerprint):
        row = self.db.execute('SELECT payload FROM datasets WHERE digest=?', (fingerprint,)).fetchone()
        if not row:
            raise KeyError(fingerprint)
        return json.loads(row[0])

    def catalog(self):
        datasets = []
        for fingerprint, payload in self.db.execute('SELECT digest,payload FROM datasets ORDER BY rowid DESC'):
            value = json.loads(payload)
            datasets.append({'digest': fingerprint, 'dataset_id': value['dataset_id'], 'entity_type': value['entity_type'], 'records': len(value['records']), 'observed_at': value['observed_at'], 'coverage': value['coverage']})
        runs = []
        for run_id, created_at, payload in self.db.execute('SELECT id,created_at,payload FROM runs ORDER BY rowid DESC'):
            value = json.loads(payload)
            runs.append({'run_id': run_id, 'created_at': created_at, 'left_dataset': value['left_dataset'], 'right_dataset': value['right_dataset'], 'summary': value['summary']})
        return {'datasets': datasets, 'runs': runs}
