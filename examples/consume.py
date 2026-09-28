"""A consuming application: no Commons UI or web dependency required."""
import json
from pathlib import Path
import sys
from bp_commons import Commons, Enrichment, discover

root = Path(sys.argv[1] if len(sys.argv) > 1 else 'data/demo')
with Commons(root / 'commons.sqlite') as records, Enrichment(root / 'enrichment.sqlite') as evidence:
    # An application can provide its own private annotations; no workspace store required.
    consumer_records = {'p1': {'known': 'yes', 'contacted': 'unknown', 'participated': 'unknown'}}
    result = discover(records, evidence, consumer_records, query='robotics', mode='new')
    print(json.dumps(result, ensure_ascii=False, indent=2))
