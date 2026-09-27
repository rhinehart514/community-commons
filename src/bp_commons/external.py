"""Explicit public organization lookup. Provider suggestions never become identity decisions."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def ror_lookup(query, output):
    if not query.strip() or len(query) > 500:
        raise ValueError('Provide a public organization name of 1–500 characters')
    url = 'https://api.ror.org/v2/organizations?' + urlencode({'affiliation': query})
    request = Request(url, headers={'User-Agent': 'BP-Commons/0.1 (public organization reconciliation)', 'Accept': 'application/json'})
    with urlopen(request, timeout=30) as response:
        raw = response.read(10_000_001)
        if len(raw) > 10_000_000:
            raise ValueError('ROR response exceeded 10MB')
        payload = json.loads(raw)
    observed = datetime.now(timezone.utc).isoformat()
    records = []
    for item in payload.get('items', []):
        org = item.get('organization', {})
        if not org.get('id'):
            continue
        names = org.get('names', [])
        display = next((n['value'] for n in names if 'ror_display' in n.get('types', [])), None)
        if not display:
            raise ValueError('ROR organization missing display name')
        records.append({'record_id': org['id'], 'name': display, 'source_ref': org['id'],
                        'identifiers': {'ror': [org['id']]},
                        'aliases': sorted({n['value'] for n in names if n['value'] != display}),
                        'provider_suggested': bool(item.get('chosen')), 'original': org})
    result = {'dataset_id': 'ror:' + hashlib.sha256(query.encode()).hexdigest()[:16],
              'entity_type': 'organization', 'source': url, 'observed_at': observed,
              'coverage': {'complete': False, 'scope': 'Ranked ROR candidates for one affiliation query; not the complete registry.'},
              'records': records, 'request': {'query': query, 'url': url},
              'response_sha256': hashlib.sha256(raw).hexdigest()}
    from .datasets import validate_dataset
    validate_dataset(result)
    output = Path(output)
    raw_path = output.with_name(output.name + '.response.json')
    if output.exists() or raw_path.exists():
        raise FileExistsError('Choose a new output path to preserve existing evidence')
    output.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(raw)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return {'dataset': str(output), 'raw_response': str(raw_path), 'records': len(records),
            'suggestions': [r['name'] for r in records if r['provider_suggested']]}


def openalex_institution(institution_id, output):
    import re
    if not re.fullmatch(r'I[0-9]+', institution_id):
        raise ValueError('Provide an OpenAlex institution ID such as I63190737')
    url = 'https://api.openalex.org/institutions/' + institution_id
    request = Request(url, headers={'User-Agent': 'BP-Commons/0.1', 'Accept': 'application/json'})
    with urlopen(request, timeout=30) as response:
        raw = response.read(10_000_001)
        if len(raw) > 10_000_000:
            raise ValueError('OpenAlex response exceeded 10MB')
        org = json.loads(raw)
    if not org.get('id') or not org.get('display_name'):
        raise ValueError('OpenAlex response missing institution identity')
    ids = {'openalex': [org['id']]}
    if org.get('ror'):
        ids['ror'] = [org['ror']]
    result = {'dataset_id': 'openalex:' + institution_id, 'entity_type': 'organization',
              'source': url, 'observed_at': datetime.now(timezone.utc).isoformat(),
              'coverage': {'complete': True, 'scope': f'One requested institution {institution_id}', 'expected_records': 1},
              'records': [{'record_id': org['id'], 'name': org['display_name'], 'source_ref': url,
                           'identifiers': ids, 'aliases': org.get('display_name_alternatives') or [], 'original': org}],
              'response_sha256': hashlib.sha256(raw).hexdigest()}
    from .datasets import validate_dataset
    validate_dataset(result)
    output = Path(output)
    raw_path = output.with_name(output.name + '.response.json')
    if output.exists() or raw_path.exists():
        raise FileExistsError('Choose a new output path to preserve existing evidence')
    output.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(raw)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return {'dataset': str(output), 'raw_response': str(raw_path), 'records': 1}
