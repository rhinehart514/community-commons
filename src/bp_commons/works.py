"""Work-first evidence archive and source-record export. No identity inference."""
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from .importer import dump, url_key

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, lane TEXT NOT NULL, manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(sha256 TEXT PRIMARY KEY, bytes BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS run_artifacts(run_id TEXT NOT NULL REFERENCES runs(id),
    path TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES artifacts(sha256), PRIMARY KEY(run_id,path));
CREATE TABLE IF NOT EXISTS works(run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY(run_id,id));
CREATE TABLE IF NOT EXISTS contributions(run_id TEXT NOT NULL, work_id TEXT NOT NULL,
    id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,id),
    FOREIGN KEY(run_id,work_id) REFERENCES works(run_id,id));
"""


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def source_identity(lane, credit):
    """Group provider records, not independently verified real-world identities."""
    if credit.get('person_url'):
        return [lane, 'profile_url', url_key(credit['person_url'])]
    for namespace in ('orcid', 'github_id', 'nih_profile_id'):
        value = credit['identifiers'].get(namespace)
        if isinstance(value, (str, int)) and str(value).strip():
            return [lane, namespace, str(value).strip()]
    return [lane, 'work_name', credit['work_id'], url_key(credit['source_url']), credit['name'].casefold()]


class Works:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def ingest(self, directory):
        directory = Path(directory).resolve()
        manifest = json.loads((directory / 'manifest.json').read_text())
        works = read_rows(directory / 'works.jsonl')
        credits = read_rows(directory / 'contributions.jsonl')
        if not isinstance(manifest.get('complete'), bool) or not manifest.get('scope') or not manifest.get('lane'):
            raise ValueError('Manifest requires lane, scope and explicit completeness')
        if manifest.get('works') != len(works) or manifest.get('contributions') != len(credits):
            raise ValueError('Manifest counts do not match collected rows')
        # All validation precedes the transaction; malformed batches never partially land.
        ids, artifacts, checked_paths = set(), {}, {}
        for work in works:
            for field in ('id', 'title', 'kind', 'source_url', 'observed_at', 'regional_evidence', 'raw_path', 'raw_sha256'):
                if not isinstance(work.get(field), str) or not work[field].strip():
                    raise ValueError(f'Work missing {field}')
            if work['id'] in ids:
                raise ValueError('Duplicate work ID in batch')
            ids.add(work['id'])
            url_key(work['source_url'])
            if datetime.fromisoformat(work['observed_at'].replace('Z', '+00:00')).tzinfo is None:
                raise ValueError('Observation time must include timezone')
            path = (directory / work['raw_path']).resolve()
            if not path.is_relative_to(directory) or not path.is_file():
                raise ValueError('Raw artifact is outside batch or missing')
            if path not in checked_paths:
                raw = path.read_bytes()
                if digest(raw) != work['raw_sha256']:
                    raise ValueError('Raw artifact hash mismatch')
                artifacts[work['raw_sha256']] = raw
                checked_paths[path] = work['raw_sha256']
            elif checked_paths[path] != work['raw_sha256']:
                raise ValueError('Raw artifact hash mismatch')
        credit_ids = set()
        for credit in credits:
            if credit.get('work_id') not in ids:
                raise ValueError('Contribution references unknown work')
            for field in ('name', 'role', 'evidence', 'source_url'):
                if not isinstance(credit.get(field), str) or not credit[field].strip():
                    raise ValueError(f'Contribution missing {field}')
            url_key(credit['source_url'])
            url_key(credit.get('person_url'))
            if not isinstance(credit.get('identifiers'), dict):
                raise ValueError('Identifiers must be an object')
            key = digest(dump(credit).encode())
            if key in credit_ids:
                raise ValueError('Duplicate contribution in batch')
            credit_ids.add(key)
        # Also retain discovery/identity inputs and failed source responses. They
        # can explain a coverage gap even when they produced no accepted work.
        for path in sorted((directory / 'raw').rglob('*')):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(directory):
                raise ValueError('Raw artifact is outside batch')
            if resolved not in checked_paths:
                raw = resolved.read_bytes()
                sha = digest(raw)
                artifacts[sha] = raw
                checked_paths[resolved] = sha
        paths = {str(p.relative_to(directory)): sha for p, sha in checked_paths.items()}
        run_id = digest(dump([manifest, works, credits, paths]).encode())
        if self.db.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
            return {'run_id': run_id, 'status': 'unchanged', 'works': len(works), 'contributions': len(credits)}
        with self.db:
            self.db.execute('INSERT INTO runs VALUES(?,?,?)', (run_id, manifest['lane'], dump(manifest)))
            self.db.executemany('INSERT OR IGNORE INTO artifacts VALUES(?,?)', artifacts.items())
            self.db.executemany('INSERT INTO run_artifacts VALUES(?,?,?)',
                                ((run_id, path, sha) for path, sha in paths.items()))
            self.db.executemany('INSERT INTO works VALUES(?,?,?)', ((run_id, w['id'], dump(w)) for w in works))
            self.db.executemany('INSERT INTO contributions VALUES(?,?,?,?)',
                                ((run_id, c['work_id'], digest(dump(c).encode()), dump(c)) for c in credits))
        return {'run_id': run_id, 'status': 'imported', 'works': len(works), 'contributions': len(credits)}

    def stats(self):
        return {table: self.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in ('runs', 'works', 'contributions', 'artifacts')}

    def export(self, base_database, output):
        """Append source-scoped work records to an existing complete export.

        Explicit profile URLs/provider IDs group credits within a lane only.
        Without one, a name stays scoped to its work and source. This is not entity resolution.
        Previously imported work records are regenerated from retained evidence.
        """
        output = Path(output)
        output.mkdir(parents=True, exist_ok=False)
        base = sqlite3.connect(Path(base_database).resolve().as_uri() + '?mode=ro', uri=True)
        streams, counts, profiles = {}, {}, {}
        urls = defaultdict(set)
        try:
            for table, filename in [('profiles', 'people'), ('observations', 'evidence'), ('relationships', 'connections')]:
                streams[filename] = (output / (filename + '.jsonl')).open('w')
                counts[filename] = 0
                for (payload,) in base.execute(f'SELECT payload FROM {table} ORDER BY id'):
                    row = json.loads(payload)
                    if row.get('work_discovery') is True:
                        continue
                    if table == 'profiles':
                        row.pop('discovered_work', None)
                        row.pop('work_evidence', None)
                        profiles[row['record_id']] = row
                        if row.get('profile_url'):
                            urls[url_key(row['profile_url'])].add(row['record_id'])
                        continue
                    streams[filename].write(dump(row) + '\n')
                    counts[filename] += 1
            groups = defaultdict(dict)
            # Preserve all historical credits, collapse identical work/credit observations.
            for lane, wp, cp in self.db.execute('''SELECT r.lane,w.payload,c.payload FROM contributions c
                    JOIN works w ON w.run_id=c.run_id AND w.id=c.work_id JOIN runs r ON r.id=c.run_id
                    ORDER BY r.rowid'''):
                work, credit = json.loads(wp), json.loads(cp)
                identity = source_identity(lane, credit)
                rid = 'person_' + digest(dump(identity).encode())[:24]
                matches = urls.get(url_key(credit.get('person_url')), set())
                if len(matches) == 1:
                    rid = next(iter(matches))
                groups[rid][(work['id'], credit['role'], credit['source_url'])] = (lane, work, credit)
            collector = {'research_work': 'researchers', 'funded_inventions': 'ecosystem',
                         'software_work': 'developers', 'builders_business': 'university',
                         'creative_public_work': 'regional_institutions'}
            new_profiles = enriched_profiles = 0
            for rid, observations in groups.items():
                entries = list(observations.values())
                lane, first, person = entries[0]
                evidence = [f"{w['title']} — {c['role']}: {c['evidence']} {w.get('description', '')}"
                            for _, w, c in entries]
                collected = {'record_id': rid, 'name': person['name'], 'profile_url': person.get('person_url', ''),
                       'source_url': person['source_url'], 'collector': collector[lane],
                       'organization': '; '.join(sorted({w.get('organization', '') for _, w, _ in entries} - {''})),
                       'role': '; '.join(sorted({c['role'] for _, _, c in entries})),
                       'expertise': '\n'.join(evidence), 'regional_tie': 'Documented work connection',
                       'regional_evidence': '\n'.join(dict.fromkeys(w['regional_evidence'] for _, w, _ in entries)),
                       'location': '', 'geography_scope': first.get('geography_scope', ''),
                       'evidence_date': first.get('work_date', ''),
                       'observed_at': max(w['observed_at'] for _, w, _ in entries),
                       'external_ids': person.get('identifiers', {}), 'work_discovery': True}
                if rid in profiles:
                    row = profiles[rid]
                    enriched_profiles += 1
                else:
                    row = profiles[rid] = dict(collected)
                    new_profiles += 1
                row['discovered_work'] = [{'work': w, 'contribution': c} for _, w, c in entries]
                row['work_evidence'] = '\n'.join(evidence)
                for _, work, credit in entries:
                    observation = {**collected, 'discovered_work': [{'work': work, 'contribution': credit}],
                                   'name': credit['name'], 'profile_url': credit.get('person_url', ''),
                                   'source_url': credit['source_url'], 'observed_at': work['observed_at'],
                                   'organization': work.get('organization', ''), 'role': credit['role'],
                                   'regional_evidence': work['regional_evidence'],
                                   'geography_scope': work.get('geography_scope', ''),
                                   'external_ids': credit['identifiers'],
                                   'expertise': credit['evidence'], 'evidence_date': work.get('work_date', '')}
                    streams['evidence'].write(dump(observation) + '\n')
                    counts['evidence'] += 1
                    relation = {'subject_record_id': rid, 'subject_name': person['name'], 'subject_url': person.get('person_url', ''),
                                'relation': credit['role'], 'object_name': work['title'], 'object_url': work['source_url'],
                                'source_url': credit['source_url'],
                                'evidence': credit['evidence'], 'evidence_date': work.get('work_date', ''),
                                'work_discovery': True}
                    streams['connections'].write(dump(relation) + '\n')
                    counts['connections'] += 1
            for row in profiles.values():
                streams['people'].write(dump(row) + '\n')
                counts['people'] += 1
            return {**counts, 'work_profiles': len(groups), 'new_work_profiles': new_profiles,
                    'enriched_existing_profiles': enriched_profiles, 'archive': self.stats()}
        finally:
            base.close()
            for stream in streams.values():
                stream.close()
