with levels as (
    select release_id, contest_id, bool_or(unit_type = 'county') as by_county
    from {{ ref('stg_results') }} where unit_type <> 'state'
    group by release_id, contest_id
)
select r.*
from {{ ref('stg_results') }} r
join levels l using (release_id, contest_id)
where r.unit_type = case when l.by_county then 'county' else 'precinct' end
