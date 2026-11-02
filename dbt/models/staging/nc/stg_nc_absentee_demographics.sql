select
    d.release_id,
    m.source_id,
    m.election_date,
    m.state,
    d.source_line,
    d.county,
    d.request_type,
    d.party_registration,
    d.sex,
    d.race,
    d.ethnicity,
    d.ballot_count
from {{ source('landing', 'nc_absentee_demographics') }} as d
inner join {{ ref('stg_nc_releases') }} as m
    on d.release_id = m.release_id
where m.dataset = 'absentee_early_demographics'