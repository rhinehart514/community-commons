# Buffalo Projects — Community Commons

Open-source infrastructure for collecting public evidence about people, capabilities, organizations, and their histories.

BP Commons is a standalone foundation for understanding people, organizations,
projects, resources, and their relationships. Buffalo Projects is its first
intended consumer. The engine belongs in this repository; application-specific
interfaces and workflows belong in consuming applications.

## Use it inside your application

Community Commons is a Python library and a set of JSON-producing commands.
Buffalo Projects and other consumers keep their own user experience. The web
interface below is an optional local evidence inspector, not a required product
or hosted destination.

```python
from bp_commons import Commons, Enrichment, discover

with Commons("data/commons.sqlite") as records, Enrichment("data/enrichment.sqlite") as evidence:
    results = discover(records, evidence, {}, query="robotics")
```

Consumers can supply their own private annotations, or use the optional local
workspace store. Source discovery, history filtering, shortlist export, and
bounded evidence-path queries work without the browser. See the
[integration contract and runnable example](docs/integration.md).

## Subscribe to evidence changes

Applications can save queries, pull newly qualifying records, and request
bounded enrichment of relevant records. No notifications are sent automatically.

```sh
bp-commons subscription create --consumer my-app --name Robotics --query-file examples/subscription-query.json
bp-commons subscription cycle --consumer my-app --id SUBSCRIPTION_ID --max-requests 10 --max-seconds 300
bp-commons subscription events --consumer my-app --after 0
bp-commons feed --after 0
```

Both feeds use durable cursors; repeat evaluations suppress unchanged results.
[Subscription contracts, limitations, and Python examples](docs/subscriptions.md).
After creating the demo below, `python examples/subscriber.py data/demo` runs an
offline consumer and verifies that the second evaluation emits no duplicate events.

## Try it without collecting anyone's data

```sh
git clone https://github.com/rhinehart514/community-commons.git
cd community-commons
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[web]"
.venv/bin/python examples/demo.py
.venv/bin/bp-commons --db data/demo/commons.sqlite serve --audit-db data/demo/reconciliation.sqlite
```

Open http://127.0.0.1:8765. The demo contains fictional profiles, comparison
candidates, and affiliation changes. It makes no network requests.

## Enrichment loop

```sh
.venv/bin/bp-commons enrich seed --limit 100
.venv/bin/bp-commons enrich run --watch --max-requests 100 --max-seconds 600
.venv/bin/bp-commons enrich status
```

Use these commands with your imported evidence store, not the fictional demo.
The worker collects OpenAlex affiliation histories and topics, GitHub profiles
and repository signals, and institution locations. It stores raw responses and
typed claims separately, resumes from a durable queue, retries with backoff,
and refreshes successful jobs after 30 days. Both time and request budgets are
mandatory limits with defaults; nothing runs forever or installs a background
service. The UI has an **Enrichment & movement** view for inspecting results.

[Worker commands, semantics, limitations, and provider references](docs/enrichment.md).

## Local web interface

From this repository, install the package and start the workbench:

```sh
.venv/bin/python -m pip install -e ".[web]"
.venv/bin/bp-commons serve
```

Open **http://127.0.0.1:8765**. Keep the terminal running; Ctrl+C stops the server.
The interface binds to this computer only and is not a public deployment.

- **Explore people:** search and filter source profiles, open source links, and inspect observations and connections.
- **Compare & review:** select saved datasets or upload Commons dataset JSON files, inspect candidate pairs, and save a decision with your name and supporting evidence.
- **Evidence history:** inspect snapshot manifests and paginate through added, changed, or removed records.

Saved reviews do not rewrite a past comparison. Click **Re-run with decisions** to
create updated results, then export them as JSON. Uploaded data must follow the
[dataset contract](docs/reconciliation.md); arbitrary spreadsheets are not yet supported.
Profiles remain source records, and a dataset match does not establish attendance
or participation. The interface reads the current evidence store and writes only
the separate reconciliation store. Refreshing the evidence store still uses the CLI.

Use `--db PATH` before `serve` to select an evidence database, or `serve --audit-db PATH`
to select a review store. Both default to the files in `data/`.

## Working today

- Import and refresh complete regional-talent exports atomically, retaining every accepted snapshot.
- Inspect added, changed, and removed evidence and retrieve historical profiles.
- Preserve complete profile records, source observations, dated evidence, and relationships.
- Search names, roles, specialties, organizations, and relationship evidence with full-text search.
- Inspect a profile alongside its supporting observations and documented relationships.
- Use the same read-only Python interface from another application.

This version is a local evidence store and query engine. Matching needs to
capabilities, live collector execution, Jev decisions,
participant availability, and coordination workflows are future layers.

## Dataset reconciliation

Compare person or organization datasets, inspect evidence, and save reversible
pair decisions. Complete examples and the input contract are in
[reconciliation](docs/reconciliation.md). Public organization lookups and library
choices are documented in [external sources](docs/external-sources.md).

```sh
.venv/bin/bp-commons compare examples/people-left.json examples/people-right.json
.venv/bin/bp-commons compare examples/organizations-left.json examples/organizations-right.json
```

Results distinguish matched, possible match, unmatched, and incomplete. Original
records remain intact. The review store is separate from the evidence snapshot
store. No application-specific contact or participation status is inferred.

## Quick start

Python 3.11+ with SQLite FTS5 is required. RapidFuzz provides fuzzy name candidate generation.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[web]"
.venv/bin/bp-commons import ../regional-talent-research/collection-2026-09-25/deliverables
.venv/bin/bp-commons refresh ../regional-talent-research/collection-2026-09-25/deliverables
.venv/bin/bp-commons history
.venv/bin/bp-commons changes SNAPSHOT_ID --limit 10
.venv/bin/bp-commons show PERSON_RECORD_ID --snapshot SNAPSHOT_ID
.venv/bin/bp-commons stats
.venv/bin/bp-commons unresolved --limit 5
.venv/bin/bp-commons search 'computer vision' --limit 5
.venv/bin/bp-commons search 'robotics'
.venv/bin/bp-commons show PERSON_RECORD_ID
.venv/bin/python -m unittest discover -s tests -v
```

Run these from the repository root. `--db PATH` before the subcommand selects a
database; the default is `data/commons.sqlite`. Import and refresh publish a new current snapshot
only after validation, retaining previous snapshots inside the same database. JSONL files are required:
`people.jsonl`, `evidence.jsonl`, and `connections.jsonl`.

Search treats words literally and requires every word to match the combined
profile/evidence document. Ranking is lexical relevance, not a measure of a
person's skill or suitability. Use `--collector developers` or another collector
name to narrow a query. All CLI results are JSON.

## Application integration

```python
from bp_commons import Commons

with Commons("data/commons.sqlite") as commons:
    matches = commons.search("computer vision", limit=5)
    detail = commons.profile(matches[0]["record_id"]) if matches else None
```

The current data model deliberately calls these **profiles**. Some GitHub
accounts may represent teams; scholarly author identities can contain errors;
separate records can describe the same person. Upstream merges are preserved,
not independently verified. Exact URL aliases from the observation records link
relationships to profiles. When a relationship has no subject URL, an exact
name-and-source match is accepted only if unique. Ambiguous or unmatched
relationships remain stored and counted as unresolved.

Historical affiliation is not current employment or residence. A shared
institution is not a confirmed personal connection. No availability, consent,
or willingness is inferred. Observation time and evidence time remain separate
in the original records.

## Data and repository boundaries

The local database lives under ignored `data/`. The collected source files remain
in the separate regional-talent-research directory. Neither collected personal
records nor third-party source content should become default repository fixtures.
Tests use fictional records. Source file SHA-256 hashes identify each import.

The code is open source under the [MIT license](LICENSE). Collected datasets,
private review records, credentials, and third-party response archives are not
published. Third-party data retains its own rights and terms.

Read [the vision](docs/vision.md) and [architecture](docs/architecture.md) for the
larger direction and the next working increments.

## Refresh semantics

`refresh` consumes a **complete, stable collector export**, not a partial page or
single-source scrape. Run the collectors separately and publish their complete
export before refreshing. This command does not fetch live websites.

Each accepted refresh retains full profile, observation, and relationship payloads.
Identical input files are a no-op. Changes to `observed_at` alone retain a new
observation snapshot but do not create semantic change entries. Reordering rows
does not create evidence changes. Missing records are absent from the new export;
this does not establish real-world departure, deletion, or retraction.

Diffs group profiles by record ID, observations by profile/source/collector, and
relationships by subject/relation/object/source/evidence date. Multiple records
within one key remain a multiset; counts describe changed groups, not always row
counts. Changing a relationship's identity fields appears as removal plus addition.
Every change contains complete before/after evidence. Search covers the current
snapshot; `show --snapshot` inspects historical evidence. `changes` supports
`--limit` and `--offset`; `history` returns the most recent snapshots (up to 100).

Full snapshots trade disk space for straightforward history inspection. There is
no pruning or concurrent-writer support. Keep one importer per database. The local
database is rebuilt from source exports when its schema changes during development;
there are no compatibility migrations.
