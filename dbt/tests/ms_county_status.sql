{{ config(tags=['ms']) }}
with results as (
    select release_id, county, count(*) as candidates
    from {{ ref('county_results') }} group by release_id, county
)
select f.release_id, f.county
from {{ ref('ms_county_feed_status') }} f
full outer join results r using (release_id, county)
where f.collection_status is null
   or (f.collection_status in ('current','stale') and coalesce(r.candidates,0) <> 3)
   or (f.collection_status in ('not_collected','unavailable') and coalesce(r.candidates,0) <> 0)
   or f.county_fips !~ '^28[0-9]{3}$'
   or f.collection_status not in ('current','stale','unavailable','not_collected')
   or (f.collection_status in ('current','stale') and
       (f.raw_sha256 is null or f.raw_sha256 !~ '^[a-f0-9]{64}$'
        or f.last_success_at is null or f.last_attempt_at is null))
   or (f.collection_status in ('not_collected','unavailable') and
       (f.raw_sha256 is not null or f.last_success_at is not null))
   or (f.collection_status = 'current' and
       (f.last_success_at is distinct from f.last_attempt_at or f.issue is not null))
   or (f.collection_status in ('stale','unavailable') and f.issue is null)
   or f.last_success_at > f.last_attempt_at
   or (f.precincts_reporting is null) <> (f.precincts_total is null)
   or f.precincts_reporting > f.precincts_total
   or f.precincts_reporting < 0 or f.precincts_total < 0
   or f.write_in_votes < 0
