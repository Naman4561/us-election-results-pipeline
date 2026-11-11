-- When a publication's vote totals were first seen. A new snapshot can change a state's
-- file (a timestamp, precinct or district detail) and be published while every contest
-- total stays the same; such a publication keeps the earlier time. Set when a results
-- publication is promoted (publication.promote); existing ones are filled in below by
-- comparing each source's publications in the order they were first observed.
BEGIN;

ALTER TABLE serving.publications ADD COLUMN votes_changed_at timestamptz;

WITH totals AS (
    SELECT p.publication_id, p.source_id, p.first_observed_at, p.published_at,
           md5(string_agg(c.contest_id || chr(31) || c.choice || chr(31) || c.total_votes,
                          chr(30) ORDER BY c.contest_id, c.choice)) AS votes
    FROM serving.publications p
    JOIN serving.contest_results c USING (publication_id)
    GROUP BY p.publication_id
), marked AS (
    SELECT *, votes IS DISTINCT FROM lag(votes) OVER (
        PARTITION BY source_id ORDER BY first_observed_at, published_at
    ) AS changed
    FROM totals
), runs AS (
    SELECT *, count(*) FILTER (WHERE changed) OVER (
        PARTITION BY source_id ORDER BY first_observed_at, published_at
    ) AS run
    FROM marked
)
UPDATE serving.publications p
SET votes_changed_at = r.since
FROM (
    SELECT publication_id, min(first_observed_at) OVER (PARTITION BY source_id, run) AS since
    FROM runs
) r
WHERE r.publication_id = p.publication_id;

COMMIT;
