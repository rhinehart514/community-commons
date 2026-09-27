"""Validated, source-scoped records for reconciliation; originals stay intact."""
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def name_key(value):
    value = unicodedata.normalize('NFKD', value).casefold()
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[^\W_]+', value))


def identifier_key(namespace, value):
    value = value.strip()
    if namespace == 'profile_url':
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or not p.netloc:
            raise ValueError('profile_url identifiers must be HTTP(S) URLs')
        return urlunsplit(('https', p.netloc.casefold().removeprefix('www.'), p.path.rstrip('/'), p.query, ''))
    if namespace in ('orcid', 'ror'):
        return value.rstrip('/').rsplit('/', 1)[-1].casefold()
    if namespace in ('email', 'github'):
        return value.casefold()
    return value


def identifiers(record):
    return {(ns, identifier_key(ns, value)) for ns, values in record.get('identifiers', {}).items() for value in values}


def validate_dataset(data):
    if not isinstance(data, dict):
        raise ValueError('Dataset must be a JSON object')
    for field in ('dataset_id', 'entity_type', 'source', 'observed_at'):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f'Dataset requires {field}')
    if data['entity_type'] not in ('person', 'organization'):
        raise ValueError('entity_type must be person or organization')
    coverage = data.get('coverage')
    if not isinstance(coverage, dict) or type(coverage.get('complete')) is not bool or not isinstance(coverage.get('scope'), str) or not coverage['scope'].strip():
        raise ValueError('coverage requires an explicit complete boolean and nonempty scope')
    if not isinstance(data.get('records'), list):
        raise ValueError('records must be a list')
    expected = coverage.get('expected_records')
    if expected is not None and (type(expected) is not int or expected < 0 or expected != len(data['records'])):
        raise ValueError('coverage.expected_records does not match received records')
    seen = set()
    for row in data['records']:
        if not isinstance(row, dict):
            raise ValueError('Each record must be an object')
        for field in ('record_id', 'name', 'source_ref'):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f'Record requires {field}')
        if row['record_id'] in seen:
            raise ValueError(f'Duplicate record ID: {row["record_id"]}')
        seen.add(row['record_id'])
        for field in ('aliases', 'affiliations'):
            values = row.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(f'{field} must be a list of nonempty strings')
        ids = row.get('identifiers', {})
        if not isinstance(ids, dict):
            raise ValueError('identifiers must be an object of string lists')
        for ns, values in ids.items():
            if not ns.strip() or not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError('identifiers must contain nonempty string values')
        identifiers(row)
    return data


def load_dataset(path):
    return validate_dataset(json.loads(Path(path).read_text(encoding='utf-8-sig')))


def from_commons(database, dataset_id, collector=None):
    from .store import Commons
    with Commons(database) as commons:
        records = []
        for stored in commons.connection.execute('SELECT payload FROM profiles WHERE (? IS NULL OR collector=?) ORDER BY id', (collector, collector)):
            p = json.loads(stored[0])
            ids = {}
            # Read structured observations; merged export external_ids may be display strings.
            detail = commons.profile(p['record_id'])
            for observation in detail['observations']:
                if observation.get('profile_url'):
                    ids.setdefault('profile_url', set()).add(observation['profile_url'])
                for ns, value in (observation.get('external_ids') or {}).items():
                    if value and ns in ('orcid', 'github', 'github_id', 'openalex', 'openalex_id'):
                        ns = 'openalex' if ns == 'openalex_id' else ns
                        ids.setdefault(ns, set()).add(str(value))
            records.append({'record_id': p['record_id'], 'name': p['name'], 'source_ref': p['source_url'],
                            'identifiers': {k: sorted(v) for k, v in ids.items()},
                            'affiliations': [p['organization']] if p.get('organization') else [],
                            'original': p})
        stats = commons.stats()
    return validate_dataset({'dataset_id': dataset_id, 'entity_type': 'person', 'source': str(database),
        'observed_at': stats['snapshot']['imported_at'], 'coverage': {'complete': True,
        'scope': f'All stored profiles in snapshot {stats["snapshot"]["snapshot_id"]}; collector={collector or "all"}. Not all regional people.'},
        'records': records})
