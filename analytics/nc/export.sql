-- Run with psql -X -qAt -v ON_ERROR_STOP=1. The only output is one JSON object.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY;
SET LOCAL statement_timeout = '60s';
SET LOCAL lock_timeout = '2s';
SET LOCAL TIME ZONE 'UTC';

WITH sources AS (
    SELECT source_id, dataset FROM ops.sources
    WHERE state = 'NC' AND election_date = DATE '2026-11-03'
    UNION
    VALUES ('nc_2026_general', 'election_results'),
           ('nc_2026_general_all', 'election_results'),
           ('nc_absentee_early_demographics_2026', 'absentee_early_demographics')
), raw_stats AS (
    SELECT source_id, count(*) AS snapshots, sum(raw_bytes) AS archive_bytes
    FROM ops.snapshots JOIN sources USING (source_id) GROUP BY source_id
), fetch_stats AS (
    SELECT source_id,
           min(finished_at) FILTER (WHERE outcome IN ('downloaded', 'manual_imported'))
               AS first_capture,
           max(finished_at) FILTER (WHERE outcome IN ('downloaded', 'manual_imported'))
               AS last_capture,
           max(finished_at) FILTER (WHERE outcome IN ('downloaded', 'unchanged'))
               AS last_successful_check
    FROM ops.fetch_attempts JOIN sources USING (source_id) GROUP BY source_id
), load_stats AS (
    SELECT source_id, count(*) AS loaded_releases, sum(loaded_rows) AS loaded_rows
    FROM ops.processing_releases JOIN ops.snapshots USING (snapshot_id)
    JOIN sources USING (source_id)
    WHERE loaded_at IS NOT NULL GROUP BY source_id
), publications AS (
    SELECT p.publication_id, p.source_id, p.snapshot_id, p.release_id,
           p.dataset, p.status, p.raw_sha256, p.first_observed_at,
           p.source_modified_at, p.published_at
    FROM serving.publications p JOIN sources s USING (source_id)
    WHERE p.state = 'NC' AND p.election_date = DATE '2026-11-03'
), publication_stats AS (
    SELECT source_id,
           count(*) FILTER (WHERE status = 'unofficial') AS unofficial_publications,
           count(*) FILTER (WHERE status = 'test') AS test_publications
    FROM publications GROUP BY source_id
), inventory AS (
    SELECT s.source_id, s.dataset, coalesce(r.snapshots, 0) AS snapshots,
           coalesce(r.archive_bytes, 0) AS archive_bytes,
           f.first_capture, f.last_capture, f.last_successful_check,
           coalesce(l.loaded_releases, 0) AS loaded_releases,
           coalesce(l.loaded_rows, 0) AS loaded_rows,
           coalesce(p.unofficial_publications, 0) AS unofficial_publications,
           coalesce(p.test_publications, 0) AS test_publications,
           c.publication_id AS current_publication_id
    FROM sources s
    LEFT JOIN raw_stats r USING (source_id)
    LEFT JOIN fetch_stats f USING (source_id)
    LEFT JOIN load_stats l USING (source_id)
    LEFT JOIN publication_stats p USING (source_id)
    LEFT JOIN serving.current_publications c USING (source_id)
), result_publications AS (
    -- A transform rerun can publish a second version of the same source snapshot.
    SELECT DISTINCT ON (snapshot_id) * FROM publications
    WHERE dataset = 'election_results' AND status = 'unofficial'
    ORDER BY snapshot_id, published_at DESC, publication_id DESC
), vote_rows AS (
    SELECT r.publication_id, NULL::text AS county, r.contest_id, r.choice, r.choice_party,
           r.election_day, r.early_voting, r.absentee_by_mail, r.provisional, r.total_votes
    FROM nc.contest_candidate_results r JOIN result_publications p USING (publication_id)
    UNION ALL
    SELECT r.publication_id, r.county, r.contest_id, r.choice, r.choice_party,
           r.election_day, r.early_voting, r.absentee_by_mail, r.provisional, r.total_votes
    FROM nc.county_candidate_results r JOIN result_publications p USING (publication_id)
), votes AS (
    SELECT publication_id,
           json_agg(json_build_array(county, contest_id, choice, choice_party,
                        election_day, early_voting, absentee_by_mail, provisional, total_votes)
                    ORDER BY contest_id, county NULLS FIRST, choice) AS rows
    FROM vote_rows GROUP BY publication_id
), latest_demographics AS (
    SELECT DISTINCT ON (source_id) * FROM publications
    WHERE dataset = 'absentee_early_demographics' AND status = 'unofficial'
    ORDER BY source_id, coalesce(first_observed_at, published_at) DESC,
             published_at DESC, publication_id DESC
), demographic_groups AS (
    -- Marginal breakdowns keep the export compact. Each dimension has its own denominator.
    SELECT d.publication_id, d.county, d.request_type, dimension.name,
           dimension.value, sum(d.ballot_count) AS ballots
    FROM nc.county_absentee_demographics d JOIN latest_demographics p USING (publication_id)
    CROSS JOIN LATERAL (VALUES
        ('party_registration', d.party_registration), ('sex', d.sex),
        ('race', d.race), ('ethnicity', d.ethnicity)
    ) dimension(name, value)
    WHERE d.ballot_count > 0
    GROUP BY d.publication_id, d.county, d.request_type, dimension.name, dimension.value
), demographics AS (
    SELECT publication_id,
           json_agg(json_build_array(county, request_type, name, value, ballots)
                    ORDER BY county, request_type, name, value) AS rows
    FROM demographic_groups GROUP BY publication_id
)
SELECT json_build_object(
    'schema_version', 1, 'state', 'NC', 'election_date', '2026-11-03',
    'exported_at', statement_timestamp(),
    'inventory', (SELECT json_agg(i ORDER BY source_id) FROM inventory i),
    'results', coalesce((
        SELECT json_agg(json_build_object(
            'source_id', p.source_id, 'publication_id', p.publication_id,
            'release_id', p.release_id, 'raw_sha256', p.raw_sha256,
            'first_observed_at', p.first_observed_at, 'source_modified_at', p.source_modified_at,
            'published_at', p.published_at, 'rows', v.rows
        ) ORDER BY coalesce(p.first_observed_at, p.published_at), p.published_at, p.publication_id)
        FROM result_publications p JOIN votes v USING (publication_id)
    ), '[]'::json),
    'demographics', coalesce((
        SELECT json_agg(json_build_object(
            'source_id', p.source_id, 'publication_id', p.publication_id,
            'first_observed_at', p.first_observed_at, 'published_at', p.published_at,
            'rows', coalesce(d.rows, '[]'::json)
        ) ORDER BY p.source_id)
        FROM latest_demographics p LEFT JOIN demographics d USING (publication_id)
    ), '[]'::json)
);
COMMIT;
