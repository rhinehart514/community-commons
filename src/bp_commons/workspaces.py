"""Private local consumer annotations, kept separate from public evidence."""
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

FLAGS = ('known', 'contacted', 'participated')


class Workspaces:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY,name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS annotations(workspace_id TEXT,profile_id TEXT,payload TEXT NOT NULL,
          PRIMARY KEY(workspace_id,profile_id));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,workspace_id TEXT,profile_id TEXT,
          created REAL NOT NULL,payload TEXT NOT NULL);
        ''')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO workspaces VALUES('personal','Personal workspace')")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def require(self, workspace):
        if not self.db.execute('SELECT 1 FROM workspaces WHERE id=?', (workspace,)).fetchone():
            raise KeyError(workspace)

    def create(self, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('Workspace name must be 1–100 characters')
        value = {'id': uuid.uuid4().hex, 'name': name.strip()}
        with self.db:
            self.db.execute('INSERT INTO workspaces VALUES(:id,:name)', value)
        return value

    def list(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM workspaces ORDER BY rowid')]

    def records(self, workspace):
        self.require(workspace)
        return {r['profile_id']: json.loads(r['payload']) for r in self.db.execute('SELECT profile_id,payload FROM annotations WHERE workspace_id=?', (workspace,))}

    def save(self, workspace, profile_id, data):
        self.require(workspace)
        if not isinstance(data, dict):
            raise ValueError('Expected an annotation object')
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError('Provide a source profile ID')
        if any(data.get(k) not in ('yes', 'no', 'unknown') for k in FLAGS):
            raise ValueError('Each history marker must be yes, no, or unknown')
        if data.get('selection') not in ('none', 'shortlisted', 'dismissed'):
            raise ValueError('Invalid selection')
        if any(not isinstance(data.get(k), str) or not data[k].strip() or len(data[k]) > 5000 for k in ('reviewer', 'reason')):
            raise ValueError('Provide your name and a supporting note (up to 5000 characters each)')
        value = {k: data[k] for k in (*FLAGS, 'selection', 'reviewer', 'reason')}
        value['updated_at'] = time.time()
        with self.db:
            self.db.execute('INSERT INTO annotations VALUES(?,?,?) ON CONFLICT(workspace_id,profile_id) DO UPDATE SET payload=excluded.payload', (workspace, profile_id, json.dumps(value)))
            self.db.execute('INSERT INTO events(workspace_id,profile_id,created,payload) VALUES(?,?,?,?)', (workspace, profile_id, value['updated_at'], json.dumps(value)))
        return value

    def history(self, workspace, profile_id):
        self.require(workspace)
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM events WHERE workspace_id=? AND profile_id=? ORDER BY id DESC', (workspace, profile_id))]


def discover(store, evidence, annotations, query='', collector=None, mode='all', offset=0):
    """Search imported text and latest enriched claims; filter before pagination."""
    if mode not in ('all', 'new', 'shortlisted', 'dismissed') or offset < 0:
        raise ValueError('Invalid discovery filter')
    tokens = re.findall(r'\w+', query, re.UNICODE)
    matches = {}
    if tokens:
        conditions = ' AND '.join('instr(lower(c.value),?)>0' for _ in tokens)
        rows = evidence.db.execute('''SELECT l.profile_id,c.predicate,c.value,r.url,r.observed
          FROM links l JOIN jobs j ON j.provider=l.provider AND j.subject=l.subject
          JOIN responses r ON r.job_id=j.id JOIN claims c ON c.response_id=r.id
          WHERE r.id=(SELECT max(r2.id) FROM responses r2 WHERE r2.job_id=j.id
          AND EXISTS(SELECT 1 FROM claims c2 WHERE c2.response_id=r2.id)) AND ''' + conditions, [t.lower() for t in tokens])
        for row in rows:
            matches.setdefault(row['profile_id'], []).append({'predicate': row['predicate'], 'value': json.loads(row['value']), 'source': row['url'], 'observed': row['observed']})
    found = {}
    start = 0
    while True:
        batch = store.browse(query, collector=collector, offset=start, limit=100)
        for item in batch['profiles']:
            found[item['id']] = {**item, 'match_basis': 'Imported profile or supporting evidence'}
        start += 100
        if start >= batch['total']:
            break
    for profile_id in matches:
        if profile_id in found:
            continue
        row = store.connection.execute('SELECT payload FROM profiles WHERE id=? AND (? IS NULL OR collector=?)', (profile_id, collector, collector)).fetchone()
        if row:
            found[profile_id] = {'id': profile_id, 'record': json.loads(row[0]), 'match_basis': 'Enriched source claim'}
    results = []
    for item in found.values():
        state = annotations.get(item['id'], {})
        if mode == 'new' and (any(state.get(k) == 'yes' for k in FLAGS) or state.get('selection') == 'dismissed'):
            continue
        if mode in ('shortlisted', 'dismissed') and state.get('selection') != mode:
            continue
        item['workspace'] = state
        item['enriched_matches'] = matches.get(item['id'], [])[:5]
        results.append(item)
    return {'total': len(results), 'profiles': results[offset:offset + 30], 'meaning': 'New means no affirmative known/contacted/participated marker in this workspace; unknown history is not proof of novelty.'}
