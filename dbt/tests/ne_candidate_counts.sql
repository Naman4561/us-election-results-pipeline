{{ config(tags=['state_ne']) }}
select release_id, unit_type, county, contest_id
from {{ ref('stg_ne_results') }}
group by release_id, unit_type, county, contest_id
having count(*) < 2 or count(total_votes) not in (0,count(*))
    or min(total_votes)<0
    or count(total_votes) <> count(*)
