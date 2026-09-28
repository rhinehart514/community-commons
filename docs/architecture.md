# Architecture

`importer.py` owns the regional-talent export adapter and snapshot construction.
`history.py` owns retained snapshots and evidence diffs.
`store.py` owns the read-only application interface. `cli.py` exposes those
operations locally. SQLite stores full source payloads and an FTS5 search index.

## Tables

- `profiles`: imported source-backed profile records, keyed by upstream record ID.
- `observations`: source observations tied to those records, including URL aliases.
- `relationships`: complete dated relationship evidence; profile linkage is nullable.
- `metadata`: current snapshot ID, adapter identity, import timestamp, and input-file hashes.
- `snapshots`: retained snapshot identities and import metadata.
- `versions`: complete profile, observation, and relationship payloads per snapshot.
- `changes`: grouped before/after evidence for each accepted refresh.
- `search_index`: derived text from profiles and resolved relationship evidence.

JSON payloads preserve the source representation without silently converting
collector-specific relationship labels into a universal ontology. The first
version provides inspectability, not a completed normalized knowledge graph.

Imports build a temporary database on the same filesystem, check constraints and
SQLite integrity, then atomically replace the selected database. Readers already
holding the old snapshot can finish; new readers see the new snapshot. Run one
importer per target database. Concurrent writers are not supported. The database
retains complete historical snapshots and does not yet store user edits. Preserve
the database or every accepted source export to retain history.

A profile URL is accepted as an alias only if it resolves to one imported profile.
Without a subject URL, unique exact name/source matching is permitted. An unknown
or ambiguous URL is not downgraded to name matching. Unresolved edges are retained
for later review, not discarded or attached speculatively.

Refresh builds the candidate current state, copies retained history, appends a
version and semantic diff, and publishes everything through one atomic replace.
Failed validation leaves both the current state and history unchanged. An identical
export does not create a new snapshot. Readers can hold the previous file open.

Next: reviewed identity decisions and source-level collection status, so partial
collector failures can be distinguished from real source withdrawals. The current
refresh boundary requires a complete export.

## Reconciliation

`datasets.py` validates source-scoped records and adapts the existing evidence
store. `reconcile.py` generates and explains pair candidates. `reviews.py` stores
original datasets, comparison runs, and append-only decisions in a separate audit
database. `external.py` retrieves public organization evidence on demand.
`evaluate.py` measures automatic decisions and candidate retrieval separately.

This is pairwise record reconciliation, not a universal entity graph. No application
workflow terms, transitive entity merges, or inferred social connections are added.

## Enrichment

`enrichment.py` owns a separate SQLite evidence store: durable jobs, links to
source profiles, immutable HTTP response archives, and typed claims. Fixed API
endpoints accept validated identifiers. No arbitrary URL fetching or private
consumer records enter the worker. Provider identities remain source-scoped.
Claim search uses the latest successful response; history stays available by
response ID. Publication affiliation transitions are hypotheses, not identity or
residence decisions. The same commands accept any region's institution IDs.

`web.py` exposes the local UI and JSON query endpoints; it does not start workers
on page load. `docs/enrichment.md` specifies the operational boundaries.

## Headless consumers

The public Python exports are `Commons`, `Enrichment`, `Reconciliation`,
`Workspaces`, `discover`, and `paths`. `workspaces.py` keeps optional consumer
annotations and their history in a separate private store. `discover` can instead
accept a mapping from a consumer's existing database. It combines source retrieval
and latest enriched evidence, then applies consumer filters before pagination.
`paths.py` traverses only explicit object URLs linked to resolved source profiles;
returned paths contain supporting evidence and do not assert social connections.

Flask is an optional `web` extra. The local inspector and HTTP endpoints are
adapters over the library, not the product's destination. Hosted consumers own
identity, authorization, and their interface. See `integration.md` for contracts.

## Query-driven collection

`streams.py` derives an append-only, replayable claim event ledger from committed
extractions. Per-job heads and event insertion commit together. `subscriptions.py`
keeps consumer query definitions, result membership, evaluation metadata, and
pull notifications in a separate private store. Evaluation fingerprints exclude
fetch timestamps and preserve before/after evidence. Plans select missing or due
jobs within an existing relevant cohort; bounded cycles restrict the worker to
those targets. Consumers own scheduling, cursor acknowledgement, authorization,
and delivery. See `subscriptions.md` for the exact coverage boundaries.
