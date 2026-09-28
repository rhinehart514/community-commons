# Public evidence enrichment

The enrichment worker is a reusable Python/SQLite layer. Regions are query inputs;
there is no Buffalo-specific rule in extraction or scheduling.

## Run it

After importing a supported regional export:

```sh
bp-commons enrich seed --limit 100
bp-commons enrich run --max-requests 50 --max-seconds 300 --interval 2
bp-commons enrich status
bp-commons enrich claims --query robotics --limit 20
bp-commons enrich transitions --institution I63190737
```

Seed reads source-provided identifiers, not names, and links each job to its
original profile. Repeating seed skips existing jobs and finds more records.
Limits count newly queued jobs; a developer may create two jobs.

`run --watch --max-requests 1000 --max-seconds 3600` waits for due jobs and exits
when either budget is reached. Ctrl+C stops the process; its in-flight job becomes
eligible again after its 120-second lease expires. A request already in progress
may take up to its 25-second network timeout beyond the time budget. Nothing
installs a daemon or schedules itself. Schedule this bounded command with cron,
launchd, or another runner when persistent operation is desired. The scheduler
must use an absolute executable path, working directory, and database path.

Successful jobs refresh after 30 days. Failures back off exponentially; after five
attempts they stop. HTTP 400/404/410/422 stop that job immediately. HTTP 401/403/429
stop the current run and defer pending jobs for that provider. Retry-After seconds
and GitHub reset timestamps are respected. Failed jobs remain inspectable; repair
the identifier or credentials before explicitly requeuing them in the local store.
Use one worker per store to enforce the configured request interval across calls.
Atomic job leases prevent duplicate claims, but multiple workers do not share a
global request-rate budget.

Optional credentials are read only from `OPENALEX_API_KEY` and `GITHUB_TOKEN`.
They are sent in authorization headers, never stored in claims, request URLs, or
error messages. Unauthenticated usage is supported within provider limits.
No paid plan, secret lookup, or outreach is enabled automatically.

## What gets collected

- OpenAlex authors: provider names/ORCID, publication affiliation years, latest
  publication institutions, and provider-assigned research topics.
- OpenAlex institutions: location and ROR, queued from latest author affiliations.
- GitHub users: self-reported professional profile fields and stable numeric ID.
- GitHub repositories: at most the 100 most recently pushed owned public repos,
  excluding forks from claims; language, topics, description, URL and timestamps.
  Hitting 100 marks the fetched repository set incomplete. Ownership does not
  establish authorship; language/topic metadata does not establish proficiency.

The original imported snapshot is untouched. `data/enrichment.sqlite` contains
jobs, profile-to-source links, raw response bytes, observation timestamps,
SHA-256 checksums, extraction version, and typed claims. Every claim points to its
source response. Refreshes append responses and claims rather than replacing them.
Search shows the latest successful extraction per job; raw failures do not hide
older valid claims. Claims can be stale: their observation time is always exposed.
Raw successful HTTP responses are retained even when parsing fails. HTTP error
bodies are not stored. This is a provider claim store, not independent verification.

The local UI's **Enrichment & movement** view searches claims, downloads preserved
responses, shows errors, and finds later-affiliation candidates. Its enrichment
database sits next to the selected reconciliation database.

## Movement is a hypothesis

Pass the complete set of origin-region institution IDs using repeated
`--institution` flags. A candidate needs a recorded origin affiliation, a later
year at an institution outside that set, and no origin institution in the provider's
latest-publication list. Missing years, simultaneous affiliations, or unknown
latest institutions do not produce a candidate.

This does not establish a move out of a city, current employment, or residence.
An omitted local institution can produce an outside-set candidate. Review the
institution's location and a dated professional biography before confirming
relocation. OpenAlex's history is incomplete and its author identities can be
wrong; no automatic identity merge is performed.

## Next layers, not implemented

- Public biography/ORCID adapters with source-specific dates and coverage.
- Human-confirmed relocation claims and contradiction review.
- Availability declarations; private known/contacted/participated annotations are
  now supported through the optional workspace library.
- Cross-source entity grouping; bounded paths through imported shared entities
  are now available through the library and CLI.
- Needs-to-capabilities ranking with a labeled relevance benchmark.
- Reusable typed model extraction through Jev after deterministic fixtures exist.

## Provider references

[OpenAlex authors](https://help.openalex.org/data/authors/),
[OpenAlex authentication](https://help.openalex.org/api/authentication/),
[GitHub repository API](https://docs.github.com/en/rest/repos/repos).
