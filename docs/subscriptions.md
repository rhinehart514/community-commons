# Change feeds and subscriptions

These are headless building blocks for consuming applications. They do not send
email, publish webhooks, install a daemon, or require anyone to visit Commons.
All stores live under ignored `data/` by default.

## Claim change feed

```python
from bp_commons import Enrichment, ChangeFeed

with Enrichment("data/enrichment.sqlite") as evidence:
    page = ChangeFeed(evidence).read(after=0, limit=100)
    for event in page["events"]:
        print(event["sequence"], event["kind"], event["predicate"])
    # Save page['cursor'] after your application successfully processes the page.
```

The feed compares sets of claim values grouped by provider job and predicate.
Changes contain before/after values, source URL, fetch time, response IDs and hash.
`added` means a predicate appeared; `changed` means its value set changed;
`absent_from_latest` means the predicate is missing from a newer successful
extraction. The last kind does NOT mean real-world withdrawal or retraction,
particularly for incomplete source responses. Imported snapshot changes retain
their separate existing `changes` API; this feed covers enrichment claims.

The first read creates baseline events for already-collected successful evidence.
Those events are not discoveries made at polling time. Source observation times
remain attached. Identical claim content produces no new event, even when a new
response has a different fetch timestamp. Invalid or in-flight responses do not
advance a source's feed head. A source response older than an already processed
head does not roll the stream backward; raw evidence remains available.

Events have stable integer sequences. Reading again from the same cursor replays
the same records; consumers must deduplicate by sequence and persist their cursor
after their own transaction succeeds. `has_more` indicates another page. Cursors
belong to one store: do not reuse them after replacing a database. This provides
durable at-least-once consumption, not exactly-once external side effects.

## Saved discovery queries

```json
{"query":"robotics","collector":"researchers","mode":"all","institutions":["I63190737"]}
```

All fields are optional, but at least one of query/collector/institutions is
required. Unsupported fields are rejected. Institutions are a supplied origin
set: the record must also qualify as a later-publication-affiliation candidate
outside that set. This is not verified relocation or current residence.

```python
from bp_commons import Commons, Enrichment, Subscriptions

with Subscriptions("data/subscriptions.sqlite") as subscriptions:
    saved = subscriptions.create("my-app", "Robotics", {"query": "robotics"})
    with Commons("data/commons.sqlite") as records, Enrichment("data/enrichment.sqlite") as evidence:
        report = subscriptions.evaluate("my-app", saved["id"], records, evidence)
    page = subscriptions.events("my-app", after=0)
```

Evaluation emits `entered`, `updated`, and `exited` result changes. The first
successful evaluation emits the existing matching set as entered. Subsequent
identical evaluations emit nothing. Re-entry after exit emits a new event.
Notifications include the saved query, prior/result payload, evidence match
explanations, and movement candidates where requested. Evaluation reports record
the source snapshot and enrichment response high-water mark.

Private modes (`new`, `shortlisted`, `dismissed`) require an explicit annotation
mapping on every evaluation; missing consumer records are never silently treated
as an empty private history. Consumer-scoped events may contain private records;
keep the subscription database private. Consumer names are namespaces, not
authentication. Hosting applications must enforce authorization or use separate
stores. Saved definitions are immutable; create another subscription to change
criteria. `pause(consumer, id, True)` disables evaluation/planning; pass False to
resume. Historical events remain readable while paused.

The current evaluator recomputes the full matching set (up to 100000 records),
using the source snapshot and a consistent read of the enrichment database.
It serializes evaluations in a subscription store to prevent duplicate transitions.
This is suitable for the current local corpus, not yet an incremental distributed
query engine. Timestamp-only refreshes are excluded from result fingerprints.
Updates concern the returned profile and matching evidence, not every claim about
that person. An exit means the record no longer qualifies under the inputs;
it does not prove a departure, deletion, or disinterest.

## Targeted enrichment

`subscriptions.plan(consumer, id, records, evidence, limit=25, apply=False)`
returns a bounded plan without network activity. Apply=True enqueues missing jobs
and links the planned records to provider identifiers. Existing jobs retain their
cooldowns, future refresh times, and failed state. Eligible existing jobs may also
be included so a cycle can finish pending work. This is selection within an
existing cohort, not a learned priority score.

For topic queries, the cohort is records already matching imported or enriched
evidence. For movement queries, it is records containing explicit origin
institution IDs, regardless of topic match: those need enrichment before they can
qualify. The origin cohort match uses exact institution-ID tokens, not city names.
Missing/invalid supported identifiers are reported for the inspected portion of
the cohort. This cannot discover people outside the imported collection or prove
that no additional matching people exist.

```sh
bp-commons feed --after 0 --limit 100
bp-commons subscription create --consumer my-app --name Robotics --query-file examples/subscription-query.json
bp-commons subscription list --consumer my-app
bp-commons subscription plan --consumer my-app --id SUBSCRIPTION_ID
bp-commons subscription cycle --consumer my-app --id SUBSCRIPTION_ID --limit 25 --max-requests 10 --max-seconds 300
bp-commons subscription events --consumer my-app --after 0 --limit 100
bp-commons subscription pause --consumer my-app --id SUBSCRIPTION_ID
```

Cycle = plan/apply → bounded fetch of ONLY those target jobs → sync claim changes
→ evaluate. It runs once and exits; schedule repeated invocations in the consuming
application's existing runner. Newly discovered institution followups remain queued
for a subsequent general enrichment run; cycles do not silently expand their
request scope. Private modes require `--annotations-file` containing a JSON mapping.
Use `--subscription-db`, `--evidence-db`, and global `--db` for explicit paths.
Rate limits can end collection early; preserved evidence is still evaluated.

## Optional loopback HTTP adapter

- `GET /api/feed?after=0&limit=100`
- `POST /api/subscriptions` with consumer, name, query
- `GET /api/subscriptions?consumer=my-app`
- `POST /api/subscriptions/:id/evaluate` with consumer and optional annotations
- `POST /api/subscriptions/:id/plan` with consumer, limit, apply
- `POST /api/subscriptions/:id/pause` with consumer and boolean paused
- `GET /api/subscription-events?consumer=my-app&after=0&limit=100`

POSTs use the existing local cross-origin header. The adapter does not expose a
network-fetch cycle; workers run through the library or CLI. It has no public
multi-user authentication. See [integration boundaries](integration.md).

## Remaining layers

This release supplies change delivery and the query-driven collection loop.
Participant-supplied corrections, calibrated project-to-capability ranking,
region-wide flow statistics with coverage denominators, and outbound webhook
infrastructure require separate contracts and tests. They are not implemented
by treating these candidate events as verified facts.
