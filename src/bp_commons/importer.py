"""Adapter for the September 2026 regional-talent JSONL export.

Build a complete snapshot in a temporary file, validate, then atomically replace
an existing database. No partial import can replace a working snapshot.
"""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import urlsplit, urlunsplit

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE profiles(id TEXT PRIMARY KEY, collector TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE observations(id INTEGER PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(id), payload TEXT NOT NULL);
CREATE INDEX observations_profile ON observations(profile_id);
CREATE TABLE relationships(id INTEGER PRIMARY KEY, profile_id TEXT REFERENCES profiles(id), payload TEXT NOT NULL);
CREATE INDEX relationships_profile ON relationships(profile_id);
CREATE TABLE metadata(payload TEXT NOT NULL);
CREATE VIRTUAL TABLE search_index USING fts5(profile_id UNINDEXED, text);
"""


def url_key(value):
    if not value:
        return ""
    p = urlsplit(value)
    if p.scheme not in {"http", "https"} or not p.netloc:
        raise ValueError(f"Invalid source/profile URL: {value!r}")
    return urlunsplit(("https", p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), p.query, ""))


def rows(path, required):
    with path.open(encoding="utf-8-sig") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or any(not row.get(key) for key in required):
                raise ValueError(f"{path.name}:{number}: missing required fields {required}")
            url_key(row["source_url"])
            yield row


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build_snapshot(source, database):
    source, database = Path(source), Path(database)
    inputs = [source / name for name in ("people.jsonl", "evidence.jsonl", "connections.jsonl")]
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(path)
    input_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    database.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bp-import-", dir=database.parent)
    os.close(fd)
    db = sqlite3.connect(temporary)
    try:
        db.executescript(SCHEMA)
        urls, names, documents = defaultdict(set), defaultdict(set), {}
        for row in rows(inputs[0], ("record_id", "name", "source_url", "regional_evidence")):
            rid = row["record_id"]
            db.execute("INSERT INTO profiles VALUES(?,?,?)", (rid, row.get("collector", ""), dump(row)))
            documents[rid] = [" ".join(str(row.get(k, "")) for k in
                ("name", "organization", "role", "expertise", "regional_evidence", "geography_scope"))]
            if key := url_key(row.get("profile_url")):
                urls[key].add(rid)
            names[(row["name"].strip().casefold(), url_key(row["source_url"]))].add(rid)
        if not documents:
            raise ValueError("Snapshot must contain profiles")
        for row in rows(inputs[1], ("record_id", "name", "source_url", "regional_evidence")):
            rid = row["record_id"]
            db.execute("INSERT INTO observations(profile_id,payload) VALUES(?,?)", (rid, dump(row)))
            if key := url_key(row.get("profile_url")):
                urls[key].add(rid)
            names[(row["name"].strip().casefold(), url_key(row["source_url"]))].add(rid)
        for row in rows(inputs[2], ("subject_name", "relation", "source_url", "evidence")):
            key = url_key(row.get("subject_url"))
            candidates = urls.get(key, set()) if key else names.get(
                (row["subject_name"].strip().casefold(), url_key(row["source_url"])), set())
            rid = next(iter(candidates)) if len(candidates) == 1 else None
            db.execute("INSERT INTO relationships(profile_id,payload) VALUES(?,?)", (rid, dump(row)))
            if rid:
                documents[rid].append(" ".join(str(row.get(k, "")) for k in
                    ("relation", "object_name", "evidence")))
        db.executemany("INSERT INTO search_index VALUES(?,?)",
                       ((rid, "\n".join(parts)) for rid, parts in documents.items()))
        metadata = {"imported_at": datetime.now(timezone.utc).isoformat(),
                    "adapter": "regional-talent-jsonl-v1",
                    "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}}
        if metadata["files"] != input_hashes:
            raise ValueError("Source files changed during import; retry with a stable export")
        db.execute("INSERT INTO metadata VALUES(?)", (dump(metadata),))
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Invalid references")
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        db.close()
        os.replace(temporary, database)
    finally:
        db.close()
        Path(temporary).unlink(missing_ok=True)
    return metadata


def import_snapshot(source, database):
    """Import or refresh a complete export while retaining previous evidence."""
    from .history import refresh_snapshot
    return refresh_snapshot(source, database)
