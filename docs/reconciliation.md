# Compare datasets

BP Commons reconciles person records or organization records from two sources.
It does not interpret application workflows or infer prior contact, participation,
availability, or a personal relationship.

## Run the examples

```sh
bp-commons compare examples/people-left.json examples/people-right.json
bp-commons compare examples/organizations-left.json examples/organizations-right.json
bp-commons comparison RUN_ID --status possible_match --limit 10
bp-commons pair RUN_ID LEFT_ID RIGHT_ID
bp-commons review RUN_ID LEFT_ID RIGHT_ID same --reviewer YOUR_NAME --reason 'Evidence inspected and reason for decision'
```

A review can be `same`, `different`, or `unsure`. Re-run `compare` to apply the
latest saved decisions. `comparison` reads the original run without rewriting it.
Review decisions are append-only. A later decision supersedes an earlier one for
that source-scoped pair. Changing either record makes the decision stale and
returns the pair for review. Review fingerprints currently include the entire
record, so even a harmless record metadata change conservatively requires review.

`pair` returns original input records and coverage. Reviews may identify missed
candidates by specifying any two existing records in that run. No records are
merged or deleted. There is no transitive clustering. A later application can use
the pair decisions to construct an entity view.

## Input contract

```json
{
  "dataset_id": "source-a:roster",
  "entity_type": "person",
  "source": "https://example.org/roster",
  "observed_at": "2026-09-26T12:00:00Z",
  "coverage": {
    "complete": true,
    "scope": "Entire specified roster, not every person in the region",
    "expected_records": 1
  },
  "records": [{
    "record_id": "source-record-1",
    "name": "Alex Example",
    "source_ref": "https://example.org/profiles/1",
    "identifiers": {"github": ["example-account"]},
    "aliases": ["A. Example"],
    "affiliations": ["Example Organization"]
  }]
}
```

Entity type is `person` or `organization`; never compare different types. Source
record IDs must be unique inside a dataset. Keep dataset IDs stable across
versions and never reuse a record ID for a different entity. `coverage.complete`
is required, not inferred from record count. Optional `expected_records` must
match the received count. The engine validates declarations; it cannot independently
know whether a collector omitted a source or falsely claimed complete coverage.

Identifiers are namespace-to-list mappings. ORCID and ROR URL forms normalize to
registry IDs. Profile URL normalization preserves path case and query strings;
GitHub handles compare case-insensitively. Unknown namespaces remain exact text.
Emails can produce candidates but never automatic identity matches. Domain-only,
shared mailbox, and organization affiliation agreement do not establish a person.
Different profile URLs are not treated as conflicting identity identifiers: a
person can have profiles on several services. Registry identifier checks are
matching normalization, not authenticity validation.

## Decisions

- **matched**: a unique matching strong identifier with no conflicting strong
  identifiers, or an explicit current human decision. Extra name-only alternatives
  do not defeat a unique strong match. Multiple accepted targets or duplicate
  left records matching the same target require review.
- **possible_match**: a candidate exists but the evidence is ambiguous, conflicting,
  nonunique, or a previous review is stale.
- **unmatched**: no accepted/candidate pair under this matcher within the supplied
  datasets. This does not prove the entity has never interacted with an organization.
- **incomplete**: negative matching cannot be concluded because declared coverage
  is incomplete or the input has insufficient identifying information.

Known matches remain useful under incomplete coverage, and every result includes
coverage. Strong namespaces are `profile_url`, `orcid`, `ror`, `github`, and
`github_id`. OpenAlex IDs are candidate signals rather than independent identity
proof. Matching identifiers must be unique in both supplied datasets. These are
explicit conservative rules, not calibrated probabilities.

Name candidate generation uses RapidFuzz ratio >=75 and at most 10 candidates
per supplied name/alias. All exact-identifier candidates and reviewed pairs are
also included. This bounds fuzzy retrieval but can miss real matches. Supplied
aliases help; the engine does not invent name changes or nickname equivalences.
The similarity score is not the probability of identity. Evidence includes exact
agreements, conflicts, name similarity, affiliation overlap, and review reasons.

## Audit and integration

Reconciliation uses `data/reconciliation.sqlite` by default (`--audit-db` overrides).
It stores original versioned datasets, immutable comparison outputs, and review
decisions. It is separate from the refreshable evidence database so evidence
imports cannot erase reviews. This is a local single-user interface: reviewer
names are declarations, not authenticated identities. Do not expose it as a
multi-tenant service without authorization and source access controls.

```python
from bp_commons.datasets import load_dataset
from bp_commons.reviews import Reconciliation

with Reconciliation('data/reconciliation.sqlite') as audit:
    result = audit.compare(load_dataset('left.json'), load_dataset('right.json'))
```

Export an existing evidence-store cohort without discarding its original records:

```sh
bp-commons export-dataset regional:university data/university.json --collector university
```

## Measure quality

```sh
bp-commons evaluate examples/people-left.json examples/people-right.json examples/people-labels.json
bp-commons evaluate examples/organizations-left.json examples/organizations-right.json examples/organizations-labels.json
```

Labels map every left record ID to the correct right record IDs (or an empty list).
Metrics distinguish automatic precision, automatic recall, and candidate recall.
Example IDs and names are fictional. These small fixtures verify behavior, not
production accuracy. Real accuracy requires held-out human labels, including
hard negatives and records with sparse evidence. No real-world precision claim
is made by the included benchmark.
