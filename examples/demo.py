"""Create isolated fictional data for trying the UI without credentials or network."""
import json
from pathlib import Path
import tempfile
from bp_commons.importer import import_snapshot
from bp_commons.reviews import Reconciliation
from bp_commons.enrichment import Enrichment

root = Path('data/demo')
root.mkdir(parents=True, exist_ok=True)
if any(root.glob('*.sqlite')):
    raise SystemExit('Demo databases already exist. Choose a fresh working directory to recreate them.')
examples = Path(__file__).parent
left = json.loads((examples / 'people-left.json').read_text())
right = json.loads((examples / 'people-right.json').read_text())
people = [{'record_id': r['record_id'], 'name': r['name'], 'collector': 'fictional-demo', 'source_url': r['source_ref'], 'regional_evidence': 'Fictional example only', 'expertise': 'Robotics', 'organization': 'Example University'} for r in left['records']]
with tempfile.TemporaryDirectory() as folder:
    for name, rows in [('people', people), ('evidence', people), ('connections', [])]:
        Path(folder, name + '.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    import_snapshot(folder, root / 'commons.sqlite')
with Reconciliation(root / 'reconciliation.sqlite') as audit:
    audit.compare(left, right)
with Enrichment(root / 'enrichment.sqlite') as evidence:
    evidence.enqueue('openalex-author', 'A1')
    fixture = {'id': 'https://openalex.org/A1', 'display_name': 'Fictional Researcher', 'affiliations': [{'institution': {'id': 'https://openalex.org/I1', 'display_name': 'Example Origin'}, 'years': [2020]}, {'institution': {'id': 'https://openalex.org/I2', 'display_name': 'Example Destination'}, 'years': [2025]}], 'last_known_institutions': [{'id': 'https://openalex.org/I2', 'display_name': 'Example Destination'}], 'topics': [{'id': 'T1', 'display_name': 'Robotics', 'count': 3}]}
    evidence.step(lambda *_: json.dumps(fixture).encode())
    # Demo entities are fictional; never send their queued IDs to providers.
    with evidence.db:
        evidence.db.execute("UPDATE jobs SET status='failed',error='FictionalDemoNoNetwork'")
print('Demo ready. Run: bp-commons --db data/demo/commons.sqlite serve --audit-db data/demo/reconciliation.sqlite')
