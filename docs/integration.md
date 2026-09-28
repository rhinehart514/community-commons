# Embed Community Commons in another application

Community Commons is upstream infrastructure. Buffalo Projects, a CRM, a research
script, or an agent owns its user experience. No Commons account, browser session,
or hosted Commons interface is required. The included web UI is an optional local
operator inspector, not the distribution model.

## Python library

```python
from bp_commons import Commons, Enrichment, discover, paths

with Commons("data/commons.sqlite") as records, Enrichment("data/enrichment.sqlite") as evidence:
    result = discover(records, evidence, {}, query="robotics")
    for match in result["profiles"]:
        print(match["record"]["name"], match["match_basis"])
        print(match["enriched_matches"])
    # Source record IDs, not names or speculative merged identities:
    # connection = paths(records, "record-a", "record-b", max_hops=4)
```

`discover` returns total, up to 30 profiles, and a semantics note. It accepts
`collector`, `offset`, and `mode` (all/new/shortlisted/dismissed). Imported evidence
uses literal-word FTS matching; enriched evidence uses all query tokens within
one claim's JSON text. This is deterministic retrieval, not semantic ranking or
proof of expertise. Imported matches retain FTS ordering, with enrichment-only
matches appended. Enriched matches include provider source and observation time.
Filtering happens before pagination. Missing profiles after refresh do not appear
in discovery even if their private annotations remain retained.

The third argument is a profile-ID-to-annotation mapping. Pass `{}` to use only
public evidence, or have the consuming application supply its own records:

```python
annotations = {
    "record-a": {"known": "yes", "contacted": "unknown", "participated": "no"}
}
# discover(records, evidence, annotations, query="robotics", mode="new")
```

There is no requirement to adopt Commons' optional workspace store. The consumer
can keep these records in its existing CRM/database. `new` means no affirmative
known/contacted/participated marker and not dismissed; unknown is not proof of
novelty. A contact does not imply participation, or vice versa.

## Optional local annotation persistence

`Workspaces(path)` provides named namespaces, current annotations, and an append-only
history of changes. `create(name)`, `list()`, `records(workspace)`,
`save(workspace, profile_id, data)`, and `history(workspace, profile_id)` are available.

A saved annotation requires `known`, `contacted`, and `participated` (each
`yes`/`no`/`unknown`), `selection` (`none`/`shortlisted`/`dismissed`), `reviewer`, and
`reason`. Save a supporting source/date in the reason. The library does not
validate that the named reviewer is the caller or that the note is true. The
consumer owns authentication, authorization, and person-to-record resolution.

Workspace namespaces are logical separation, not security boundaries. Use separate
stores and application authorization for separate tenants. The store contains
private consumer records; do not publish it or feed it into public collection.
Changes never modify the source profile or enrichment claims. Status applies to
one source record ID; it is not automatically propagated across possible duplicates.

## Sourced paths

`paths(records, source_id, target_id, max_hops=4, max_visited=5000)` returns one
shortest path through resolved profile-to-object-URL edges. Every step includes
the original relationship ID, relation, source URL, and evidence/date. Shared
names without a URL and unresolved profile links are excluded. URLs must match
exactly; no fuzzy entity merge is performed. Parallel edges retain one supporting
observation for the path, with all original observations still in the source store.

A two-hop path means two records share a documented entity. A four-hop path crosses
an intermediate profile. Neither means friendship, contemporaneous affiliation,
verified collaboration, or an available introduction. Dates are preserved but paths
are not restricted to overlapping periods. No result means no path found within
the available graph and limits; `truncated=true` means the visit budget was reached.
The graph is built from imported relationships per call; the current implementation
is intended for local datasets, not an unbounded hosted graph service.

## CLI and local HTTP

```sh
bp-commons discover robotics
bp-commons discover robotics --workspace personal --mode new
bp-commons workspace create --name 'Example consumer'
bp-commons workspace list
bp-commons workspace show --workspace personal
bp-commons workspace save --workspace personal --profile record-a --input annotation.json
bp-commons paths record-a record-b --max-hops 4
```

Use `--workspace-db` / `--evidence-db` to select stores for discovery; the global
`--db` selects source evidence. All CLI results are JSON suitable for another process.

The optional loopback HTTP service exposes:

- `GET /api/discover?q=robotics&workspace=personal&mode=new&offset=0`
- `GET /api/workspaces` and `POST /api/workspaces` with `{ "name": "..." }`
- `GET` / `POST /api/workspaces/:workspace/profiles/:profile_id`
- `GET /api/workspaces/:workspace/export` (shortlisted records and annotations)
- `GET /api/paths?source=record-a&target=record-b`

POSTs require `Content-Type: application/json` and `X-BP-Commons: workbench`.
The latter is a local cross-origin guard, not authentication. The service has no
multi-user auth and binds only to localhost. Deploying behind another app requires
that app's authentication and authorization; the shipped server is not a public API.

Run `python examples/consume.py data/demo` after creating the fictional demo for
a complete headless integration example. No external requests are made.

## Changes and subscriptions

`ChangeFeed` and `Subscriptions` expose durable pull events and saved discovery
queries. See [subscriptions](subscriptions.md) for the bounded cycle, consumer
cursors, targeted planning, and optional local HTTP endpoints.
