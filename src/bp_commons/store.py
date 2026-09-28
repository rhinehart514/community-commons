"""Read-only query interface. Imported profiles are not verified human identities."""
import json
import re
import sqlite3
from pathlib import Path


class Commons:
    def __init__(self, database):
        uri = Path(database).resolve().as_uri() + "?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.row_factory = sqlite3.Row

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def stats(self):
        counts = {table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("profiles", "observations", "relationships")}
        counts["unresolved_relationships"] = self.connection.execute(
            "SELECT count(*) FROM relationships WHERE profile_id IS NULL").fetchone()[0]
        counts["snapshot"] = json.loads(self.connection.execute(
            "SELECT payload FROM metadata").fetchone()[0])
        return counts

    def search(self, query, *, limit=20, collector=None):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        tokens = re.findall(r"\w+", query, re.UNICODE)
        if not tokens:
            return []
        # Treat input as words, never as FTS operators or SQL.
        expression = " AND ".join('"' + token + '"' for token in tokens)
        rows = self.connection.execute(
            "SELECT p.payload FROM search_index s JOIN profiles p ON p.id=s.profile_id "
            "WHERE search_index MATCH ? AND (? IS NULL OR p.collector=?) "
            "ORDER BY rank, p.id LIMIT ?", (expression, collector, collector, limit))
        return [json.loads(row[0]) for row in rows]

    def unresolved(self, *, limit=20, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("limit must be 1–100 and offset must be nonnegative")
        return [{"relationship_id": row[0], "evidence": json.loads(row[1])}
                for row in self.connection.execute(
                    "SELECT id,payload FROM relationships WHERE profile_id IS NULL "
                    "ORDER BY id LIMIT ? OFFSET ?", (limit, offset))]

    def history(self, *, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1–100")
        return [{"snapshot_id": r[0], "created_at": r[1], "metadata": json.loads(r[2])}
                for r in self.connection.execute(
                    "SELECT id,created_at,metadata FROM snapshots ORDER BY rowid DESC LIMIT ?", (limit,))]

    def changes(self, snapshot_id, *, limit=20, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("limit must be 1–100 and offset must be nonnegative")
        if not self.connection.execute("SELECT 1 FROM snapshots WHERE id=?", (snapshot_id,)).fetchone():
            raise KeyError(snapshot_id)
        return [{"kind": r[0], "record_key": r[1], "change_type": r[2],
                 "before": json.loads(r[3]), "after": json.loads(r[4])}
                for r in self.connection.execute(
                    "SELECT kind,record_key,change_type,before_payload,after_payload FROM changes "
                    "WHERE snapshot_id=? ORDER BY kind,record_key LIMIT ? OFFSET ?",
                    (snapshot_id, limit, offset))]

    def profile(self, record_id, *, snapshot_id=None):
        if snapshot_id:
            rows = self.connection.execute(
                "SELECT kind,payload FROM versions WHERE snapshot_id=? AND profile_id=? ORDER BY position",
                (snapshot_id, record_id)).fetchall()
            profiles = [json.loads(r[1]) for r in rows if r[0] == "profiles"]
            if not profiles:
                raise KeyError((snapshot_id, record_id))
            return {"profile": profiles[0], "observations": [json.loads(r[1]) for r in rows if r[0] == "observations"],
                    "relationships": [json.loads(r[1]) for r in rows if r[0] == "relationships"]}

        row = self.connection.execute("SELECT payload FROM profiles WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        return {
            "profile": json.loads(row[0]),
            "observations": [json.loads(r[0]) for r in self.connection.execute(
                "SELECT payload FROM observations WHERE profile_id=? ORDER BY id", (record_id,))],
            "relationships": [json.loads(r[0]) for r in self.connection.execute(
                "SELECT payload FROM relationships WHERE profile_id=? ORDER BY id", (record_id,))],
        }

    def browse(self, query='', *, collector=None, offset=0, limit=30):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError('Invalid pagination')
        tokens = re.findall(r'\w+', query, re.UNICODE)
        parameters = []
        source = 'profiles p'
        where = 'WHERE (? IS NULL OR p.collector=?)'
        order = 'p.id'
        if tokens:
            source += ' JOIN search_index s ON p.id=s.profile_id'
            where += ' AND search_index MATCH ?'
            order = 'rank, p.id'
        parameters.extend([collector, collector])
        if tokens:
            parameters.append(' AND '.join('"' + t + '"' for t in tokens))
        if query.strip() and not tokens:
            return {'total': 0, 'profiles': []}
        total = self.connection.execute(f'SELECT count(*) FROM {source} {where}', parameters).fetchone()[0]
        rows = self.connection.execute(f'SELECT p.id,p.payload FROM {source} {where} ORDER BY {order} LIMIT ? OFFSET ?', [*parameters, limit, offset])
        return {'total': total, 'profiles': [{'id': r[0], 'record': json.loads(r[1])} for r in rows]}
