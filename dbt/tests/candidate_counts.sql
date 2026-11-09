{{ config(tags=['results']) }}
-- Counts are all known or all unknown within a contest (and within a county contest).
select release_id, contest_id, '' as county
from {{ ref('contest_results') }}
group by release_id, contest_id
having count(total_votes) not in (0, count(*)) or min(total_votes) < 0
union all
select release_id, contest_id, county
from {{ ref('county_results') }}
group by release_id, contest_id, county
having count(total_votes) not in (0, count(*)) or min(total_votes) < 0
