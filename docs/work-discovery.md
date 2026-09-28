# People through credited work

Work discovery collects projects, publications, awards, software, and public
creative work before extracting named contributions. A credited role is not a
skill rating, proof of residence, or a personal connection.

The public repository contains collectors and synthetic tests. All collection
outputs and source bytes live under ignored `data/`, and the Buffalo Projects
consumer publishes its serving snapshot and evidence archives to private hosted
storage. Source data is not part of the open-source distribution.

## Collect, validate, publish

Each collector under `scripts/work_discovery/` writes a lane directory with
`works.jsonl`, `contributions.jsonl`, `manifest.json`, and raw source artifacts.
See the [collection contract](../scripts/work_discovery/CONTRACT.md). Collectors
declare the scope and limitations of each source, including blocked requests,
pagination limits, incomplete historical coverage, and unsupported attribution.

Run source lanes separately, with explicit private output paths and budgets:

```sh
python3 scripts/work_discovery/funded_inventions.py --output data/work-discovery/RUN/funded_inventions --max-requests 150 --max-seconds 900
python3 scripts/work_discovery/research_work.py --output data/work-discovery/RUN/research_work --cache-root data/seeds/research --max-requests 70 --max-seconds 300
python3 scripts/work_discovery/software_work.py --output data/work-discovery/RUN/software_work --seed-dir data/seeds/developers --budget 140 --deadline-seconds 600
python3 scripts/work_discovery/builders_business.py --output data/work-discovery/RUN/builders_business --max-requests 150 --minutes 15
python3 scripts/work_discovery/creative_public_work.py --output data/work-discovery/RUN/creative_public_work --max-requests 120 --max-seconds 900
```

The software lane requires authenticated `gh api` access and a prior regional
developer seed; a package or account keyword match alone does not establish a
regional tie. Its seed directory contains `people.jsonl` with `profile_url`,
`external_ids.github`, `name`, `location`, `observed_at`, and `geography_scope`
(`Buffalo/WNY` or `Rochester expansion`). Optional `raw/graphql-*.json` GitHub
responses supply repository candidates; ownership alone never supplies a credit.

Research requires `researchers/institutions.jsonl` under its cache root, with
`openalex_id`, `display_name`, and `geography_scope` per institution. Optional
`researchers/people.jsonl` and `researchers/raw/works-responses.jsonl` reuse prior
OpenAlex snapshots, retaining their original observation dates. Seed inputs are
private and are not bundled with this repository. Research refuses a nonempty
output directory. The creative lane uses `pdftotext` when collecting the public
planning report. All scripts expose their additional options through `--help`.
Use the creative collector's `--offline` flag to reparse preserved responses
without network requests; missing cached sources remain explicit gaps.

Each invocation has its own budget. Repeated invocations spend additional
requests; there is no shared cross-lane quota. Review the resulting source
coverage and attribution before accepting a lane into the archive.

```sh
bp-commons works ingest --source data/work-discovery/RUN/LANE
bp-commons works stats
bp-commons works export --output data/work-discovery/complete-export
bp-commons refresh data/work-discovery/complete-export
```

Use a new export directory for each publication. Export reads the current
Commons database without changing it; refresh validates the complete replacement
and retains its history. Existing unrelated profiles and observations survive.
The separate work archive defaults to `data/works.sqlite` (`--archive` overrides).

The work archive validates exact file hashes, safe artifact paths, unique work
IDs, declared row counts, observation timestamps, and contribution references
before committing a run. Re-importing identical input is idempotent. Raw bytes,
manifests, works, and contributions are retained transactionally. Validation
establishes structural integrity; it does not independently prove an extracted
credit is correct. Review source samples before publication.

## Identity and coverage

The adapter groups credits within a collection lane by explicit profile URL or
source-provided ORCID, GitHub numeric ID, or NIH profile ID. Without one of those,
a name stays scoped to its work and source (including when many works share one
bulk file URL). An exact profile URL matching exactly one
existing source record attaches work to that record instead of creating another.
Ambiguous URLs do not link automatically. Original profile fields remain intact;
`discovered_work` and `work_evidence` are regenerated projections from the archive.
It never merges
people globally by name, invents an identifier, or treats a source account as a
verified real-world identity. Separate records may describe the same person.

Each source record contains its work objects and contribution evidence. Work
titles and descriptions are searchable, and each contribution becomes a dated
relationship to its work. Current residence remains blank unless another source
explicitly establishes it; a regional project connection is recorded separately.

Repeated imports retain historical observations. The export suppresses identical
work/role/source credits, but does not interpret missing credits as withdrawals.
Work archive statistics count retained work versions and credits, not globally
deduplicated works or unique people. No collection claims to cover everyone.

## Consumer integration

```python
from bp_commons import Works

with Works('data/works.sqlite') as archive:
    receipt = archive.ingest('data/work-discovery/RUN/LANE')
    print(receipt)
```

The Buffalo Projects publisher archives `works.sqlite` alongside `commons.sqlite`
and `enrichment.sqlite`, verifies remote hashes, and only then switches the live
snapshot. Browsing the hosted dataset does not require a collector's filesystem.
The collector scripts run when explicitly invoked; this addition does not install
a daily scheduler or an unbounded background worker.
