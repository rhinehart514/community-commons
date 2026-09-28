"""Offline consumer of the fictional demo; no UI, credentials, or network calls."""
import json
from pathlib import Path
import sys
from bp_commons import Commons, Enrichment, Subscriptions, ChangeFeed

root = Path(sys.argv[1] if len(sys.argv) > 1 else 'data/demo')
with Commons(root / 'commons.sqlite') as records, Enrichment(root / 'enrichment.sqlite') as evidence, Subscriptions(root / 'subscriptions.sqlite') as subscriptions:
    saved = subscriptions.create('example-consumer', 'Robotics', {'query': 'robotics'})
    first = subscriptions.evaluate('example-consumer', saved['id'], records, evidence)
    repeat = subscriptions.evaluate('example-consumer', saved['id'], records, evidence)
    page = subscriptions.events('example-consumer')
    claims = ChangeFeed(evidence).read()
    assert repeat['emitted'] == 0
    print(json.dumps({'subscription': saved['id'], 'first_evaluation': first, 'repeat_evaluation': repeat, 'event_cursor': page['cursor'], 'claim_cursor': claims['cursor']}, indent=2))
