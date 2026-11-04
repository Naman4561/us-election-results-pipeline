{{ config(tags=['state_mn']) }}
select release_id, unit_type, county, contest_id
from {{ ref('stg_mn_results') }}
group by release_id, unit_type, county, contest_id
having count(*) < {{ 1 if var('allow_uncontested', false) else 2 }} or count(total_votes) not in (0,count(*))
    or min(total_votes)<0
    or count(total_votes) <> count(*)
