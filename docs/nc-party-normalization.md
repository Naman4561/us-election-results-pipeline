# NC missing-party marker and private preparation corrections

The official ZIP with SHA-256
`5d9ac397a8d8dc6b8b2f2bb5faf120361edc0de5aaaf707540bc7357040101d9`
contains a single NUL character in the Choice Party field for 177 NC-02
Write-In (Miscellaneous) reporting-unit rows. The original five-race scope did
not include NC-02, so these rows first reached database loading during expansion.

The NC parser maps that exact missing-party marker to an empty parsed value,
which the existing database conversion stores as SQL NULL. It records the number
of normalizations. It retains the candidate name, reporting units, all vote
methods, totals and raw ZIP bytes. No party affiliation is inferred. Embedded
NULs and NULs in other retained fields are rejected. Untracked local races remain
outside the parsed output.

The generic loader also rejects any remaining NUL in a detailed CSV before
passing the row to PostgreSQL. A normalized write-in is now included in the
four-state PostgreSQL acceptance fixture.

A corrected image must be able to replace a private preparation image without
changing a live source. Re-pinning requires an inactive private expansion source,
identical source configuration, collector settings and race coverage, matching
image and checkout package hashes, and unchanged applied migrations. It preserves
the prior preparation's provenance and historical database records, clears only
catalog validation/publication pointers, and requires preparation and publication
to pass again. Enabled or public sources cannot be re-pinned this way.
