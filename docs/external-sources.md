# External sources and libraries

## Integrated

- [RapidFuzz](https://rapidfuzz.github.io/RapidFuzz/Usage/process.html): maintained
  fuzzy-string matching library. Used for name candidate generation, never as
  identity proof. Runtime dependency `rapidfuzz>=3.14,<4`.
- [ROR affiliation API](https://ror.readme.io/docs/api-affiliation): public research
  organization candidates, names, aliases, and ROR IDs. The provider's `chosen`
  signal is preserved as a suggestion, not silently accepted as a match.
- [OpenAlex institution API](https://docs.openalex.org/api-entities/institutions):
  fetch a specified institution and retain its ROR cross-reference for comparison.

```sh
bp-commons ror 'University at Buffalo' data/ror-buffalo.json
bp-commons openalex-institution I63190737 data/openalex-buffalo.json
bp-commons compare data/openalex-buffalo.json data/ror-buffalo.json
```

Commands fetch public organization information only when explicitly invoked.
They save the raw JSON response, request source, observation time, SHA-256 hash,
and a normalized comparison dataset. Existing output files are not overwritten.
ROR is a ranked candidate response and declares incomplete registry coverage.
OpenAlex declares completeness only for the one requested institution.
Network failures leave no normalized dataset. Live network access is not required
for tests; response fixtures test extraction and preservation.

Live verification on September 26, 2026: University at Buffalo's OpenAlex record
and a ROR candidate shared `01y64my43`. The comparison resolved that pair through
the registry identifier. This is a source agreement, not independent verification
of all attributes supplied by either service.

## Evaluated, not integrated

- [Splink](https://moj-analytical-services.github.io/splink/): probabilistic linkage
  with explanatory comparisons. Worth evaluating when we have labeled real pairs;
  not needed to replace the initial conservative rules blindly.
- [Dedupe](https://docs.dedupe.io/en/latest/how-it-works/Matching-records.html):
  active-learning alternative, also dependent on useful reviewer labels.
- [OpenRefine reconciliation API](https://openrefine.org/docs/technical-reference/reconciliation-api):
  potential public service interface once a stable matching contract is proven.
- [ORCID public API](https://info.orcid.org/what-is-orcid/services/public-api/):
  useful for researcher identity enrichment, with credentials and usage terms to
  resolve before adding requests. Existing source-provided ORCID IDs are supported.

No Exa, Firecrawl, paid enrichment calls, private CRM uploads, or outreach are used.

The bounded [enrichment worker](enrichment.md) also integrates OpenAlex author
histories and topics, GitHub public profiles, and owned public repositories.
