{{ config(tags=['state_mn']) }}
select release_id, choice
from {{ ref('mn_county_candidate_results') }} where contest_id in ('MN-SEN','MN-GOV')
group by release_id, contest_id, choice having count(distinct county) <> 87
