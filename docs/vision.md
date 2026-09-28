# Direction

Given what someone wants to accomplish, identify what exists, what is missing,
and what could be brought together to make it happen.

BP Commons should support a maintained, inspectable understanding of capabilities
and needs. People are one entity type alongside organizations, projects,
publications, resources, and eventually expressed needs. Buffalo is the first
regional dataset, not a hard-coded boundary of the engine.

## Layers

1. Evidence: observations with provenance, applicable dates, and acquisition time.
2. Understanding: identities, typed claims, relationships, and uncertainty.
3. Possibilities: evidence-backed matches and the missing facts needed to assess them.
4. Coordination: explicit interests, availability, proposals, and commitments.
5. Learning: participant corrections and observed outcomes improve subsequent decisions.

Keep demonstrated capability, declared availability, and confirmed commitment
separate. A plausible collaboration is a hypothesis for participants to evaluate.

## Working increments

The import/query engine, snapshot history, pair review, and bounded public-source
enrichment loop are implemented. Next:

- Extend the OpenAlex/GitHub collectors to biographies and ORCID with explicit
  source coverage; absence must not be mistaken for confirmed withdrawal.
- Extend the implemented pair-review decisions into evaluated entity grouping;
  reviewed same/different/unsure links already preserve original records.
- Normalize sourced capabilities and needs; validate one matching workflow using
  real requests and human-reviewed candidate lists.
- Add a Jev adapter for narrow typed decisions after establishing an evaluation
  set. Model outputs must retain their input evidence and decision version.
- Accept explicit participant corrections, interests, and availability.
- Track proposed collaborations and actual outcomes through consuming applications.

Do not build generic workflow execution, a universal ontology, or autonomous
outreach ahead of an observed need. Prove reuse by integrating a second consumer
without importing Buffalo Projects' product logic into the engine.

## First consumer

Buffalo Projects can use BP Commons to surface local capabilities, connect them
to project needs, and capture corrections and outcomes. Nick's talent discovery
workflow is one additional application. Research collaboration and open-source
project staffing are other possible consumers, not implemented capabilities.
