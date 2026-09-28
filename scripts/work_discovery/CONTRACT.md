# Work-first collection contract

Collectors write only into an ignored, private run directory, conventionally
`data/work-discovery/RUN/LANE/`. Use a new directory per accepted run. Source code
and fictional tests can be public; collected people, raw responses, credentials,
and run manifests must not be committed.

## Output

- `works.jsonl`: objects with `id`, `title`, `kind`, `source_url`, `observed_at`,
  `work_date`, `organization`, `regional_evidence`, `geography_scope`,
  `description`, `raw_path`, and `raw_sha256`. IDs are stable and source-scoped.
  Observation times include a timezone; dates use ISO format when known and an
  empty string otherwise. Raw paths are relative to the lane directory.
- `contributions.jsonl`: objects with `work_id`, `name`, `role`, `person_url`,
  `identifiers`, `evidence`, and `source_url`. An unknown person URL is an empty
  string, and unknown identifiers are an empty object. Each credit references a
  collected work and preserves the source's actual role.
- `manifest.json`: `lane`, `started_at`, `completed_at`, `requests`, exact `works`
  and `contributions` row counts, explicit `complete` boolean, `scope`, `sources`,
  `limitations`, and `errors`. Source entries identify URL, status, records,
  coverage, and limitation. State what each count measures. Every attempted
  family appears, including blocked sources and zero verified results.
- `raw/`: original fetched bytes and any seed snapshots needed to substantiate
  identity or regional claims. The archive retains these even when no accepted
  work resulted. Work objects reference the corresponding SHA-256 digest.

## Attribution and coverage

Collect public professional work with documented regional ties. Regional work
does not establish personal residence. Preserve grants as funding records, not
proof that proposed results were accomplished. Distinguish historical evidence
from current observations, and keep cached acquisition dates intact.

Do not infer authorship from repository ownership, personal relationships from
co-occurrence, attendance from an event listing, or cross-platform identity from
matching names or handles. Obvious organizations, bots, headings, and collective
credits must not become person records. Preserve uncertain attribution as
unresolved rather than guessing.

Use bounded requests and elapsed-time limits, respect provider rate limits, and
save errors without credentials. Prefer bulk data and reparsing preserved source
bytes over repeated unchanged requests. A request ceiling or access blocker must
be reflected in coverage; it does not mean the source was exhausted. Collector
execution does not authorize outreach or bypassing access controls.

## Validation

`bp-commons works ingest --source DIRECTORY` validates structure, counts,
references, timestamps, raw paths, and hashes before committing the lane.
Collectors also need source-specific attribution checks and synthetic regression
tests. Passing structural validation alone does not verify that a parsed name or
role is correct.
