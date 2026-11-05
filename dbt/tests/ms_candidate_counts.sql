{{ config(tags=['state_ms']) }}
select release_id, unit_type, county, contest_id
from {{ ref('stg_ms_results') }}
group by release_id, unit_type, county, contest_id
having count(*) <> 3 or count(total_votes) <> 3 or min(total_votes) < 0
union all
select release_id, unit_type, county, contest_id
from {{ ref('stg_ms_results') }}
where not ((choice='Scott Colom' and choice_party='DEM')
        or (choice='Cindy Hyde-Smith' and choice_party='REP')
        or (choice='Ty Pinkins' and choice_party='IND'))
