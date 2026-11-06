# Official response fixtures

These `.gz` files contain exact response bytes, not synthetic vote totals.
`provenance.json` records the URL, decompressed SHA-256, byte count and download
time for each response. SC includes both the official fallback page and the
workbook it linked. All snapshots concern the November 3, 2026 election; capture
times can be November 4 in UTC. The fixture numbers are frozen test observations,
not current totals or certified results.

The rosters in `resources/rosters_2026/` were derived from these responses. County
eligibility includes Kansas's unreported map counties, not just rows with votes.
Ohio's true county reporting units are distinct from its county electoral-district
units. These rosters are included in the parser code fingerprint and should change
only after reviewing an actual candidate/geography contract change.

Tests mutate wrong elections, missing/duplicate candidates/counties, inconsistent
totals/methods, formula cells, unsafe report links and malformed XML/JSON. They
also exercise explicit zeros, downward corrections, ticket handling, replay,
dashboard geometry and source discovery budgets. Synthetic mutations are applied
in temporary test directories; the recorded responses remain unchanged.
