# BP Commons

Evidence infrastructure for connecting capabilities with needs.

BP Commons is a standalone foundation for understanding people, organizations,
projects, resources, and their relationships. Buffalo Projects is its first
intended consumer. The engine belongs in this repository; application-specific
interfaces and workflows belong in consuming applications.

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
.venv/bin/python -m pip install -e .
.venv/bin/bp-commons import ../regional-talent-research/collection-2026-09-25/deliverables
.venv/bin/bp-commons refresh ../regional-talent-research/collection-2026-09-25/deliverables
.venv/bin/bp-commons history
.venv/bin/bp-commons changes SNAPSHOT_ID --limit 10
.venv/bin/bp-commons show PERSON_RECORD_ID --snapshot SNAPSHOT_ID
.venv/bin/bp-commons stats
.venv/bin/bp-commons unresolved --limit 5
.venv/bin/bp-commons search 'computer vision' --limit 5
.venv/bin/bp-commons search 'Nick Branholm'
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

This repository has local version control and a CI workflow, but has not been
published and has no license assigned yet. Dataset redistribution rights are
separate from code licensing.

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
