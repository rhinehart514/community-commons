"""Durable pull-based claim changes. Source absence is not real-world retraction."""
import json
from .datasets import canonical


def claim_groups(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row['predicate'], set()).add(row['value'])
    return {key: [json.loads(value) for value in sorted(values)] for key, values in groups.items()}


class ChangeFeed:
    def __init__(self, evidence):
        self.evidence = evidence
        self.db = evidence.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS feed_heads(job_id INTEGER PRIMARY KEY,response_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS claim_events(sequence INTEGER PRIMARY KEY,job_id INTEGER NOT NULL,
          response_id INTEGER NOT NULL,payload TEXT NOT NULL);
        ''')

    def sync(self):
        """Atomically diff committed extractions; failed/in-flight responses are excluded."""
        count = 0
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rows = self.db.execute('''SELECT r.*,j.provider,j.subject FROM responses r JOIN jobs j ON j.id=r.job_id
              LEFT JOIN feed_heads h ON h.job_id=r.job_id
              WHERE r.id>coalesce(h.response_id,0) AND EXISTS(SELECT 1 FROM claims c WHERE c.response_id=r.id)
              ORDER BY r.id''').fetchall()
            for row in rows:
                head = self.db.execute('SELECT response_id FROM feed_heads WHERE job_id=?', (row['job_id'],)).fetchone()
                before = claim_groups(self.db.execute('SELECT predicate,value FROM claims WHERE response_id=?', (head[0] if head else -1,)))
                after = claim_groups(self.db.execute('SELECT predicate,value FROM claims WHERE response_id=?', (row['id'],)))
                for predicate in sorted(before.keys() | after.keys()):
                    if before.get(predicate) == after.get(predicate):
                        continue
                    event = {'kind': 'added' if predicate not in before else 'absent_from_latest' if predicate not in after else 'changed',
                             'provider': row['provider'], 'subject': row['subject'], 'predicate': predicate,
                             'before': before.get(predicate, []), 'after': after.get(predicate, []),
                             'source': row['url'], 'observed_at': row['observed'], 'response_id': row['id'],
                             'previous_response_id': head[0] if head else None, 'sha256': row['sha256'],
                             'meaning': 'Difference between fetched provider claims, not verified real-world change or withdrawal.'}
                    self.db.execute('INSERT INTO claim_events(job_id,response_id,payload) VALUES(?,?,?)', (row['job_id'], row['id'], canonical(event)))
                    count += 1
                self.db.execute('INSERT INTO feed_heads VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET response_id=excluded.response_id', (row['job_id'], row['id']))
        return count

    def read(self, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Use a nonnegative cursor and limit 1–1000')
        self.sync()
        rows = self.db.execute('SELECT sequence,payload FROM claim_events WHERE sequence>? ORDER BY sequence LIMIT ?', (after, limit)).fetchall()
        cursor = rows[-1]['sequence'] if rows else after
        return {'events': [{'sequence': r['sequence'], **json.loads(r['payload'])} for r in rows], 'cursor': cursor,
                'has_more': bool(self.db.execute('SELECT 1 FROM claim_events WHERE sequence>? LIMIT 1', (cursor,)).fetchone())}
