{{ config(tags=['state_mn']) }}
select release_id, choice
from {{ ref('mn_county_candidate_results') }} where contest_id='MN-SEN'
group by release_id, choice having count(distinct county) <> 87
