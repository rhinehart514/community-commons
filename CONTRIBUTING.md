# Contributing to Community Commons

Community Commons is a Buffalo Projects project. The code is MIT licensed.

Install with `python -m pip install -e ".[web]"`, run
`python -m unittest discover -s tests -v`, and try `python examples/demo.py`.
Tests use fictional records and mocked providers; credentials are not required.

Keep collection, evidence storage, identity decisions, and consumer workflows
separate. Preserve original evidence. Distinguish observation time from the time
a claim applies. Report missing coverage and uncertainty rather than inferring
absence, identity, location, skills, or willingness. New collectors need bounded
requests, provenance, fixtures, rate-limit handling, and documented source terms.

Do not commit collected personal records, tokens, local databases, or private
workspace data. Use example.org URLs and fictional identities in test fixtures.
The code license does not grant redistribution rights to third-party data.

Submit focused pull requests with the behavior changed and validation performed.

The library and CLI are the primary integration surfaces. Keep frontend dependencies
optional, and test new capabilities through a headless consuming example.
