select
    r.release_id,
    m.source_id,
    m.election_date,
    m.state,
    r.source_line,
    r.county,
    r.precinct as reporting_unit,
    r.real_precinct,
    r.contest_id,
    r.contest_name,
    r.source_contest_group_id,
    r.contest_type,
    r.choice,
    r.choice_party,
    r.vote_for,
    r.election_day,
    r.early_voting,
    r.absentee_by_mail,
    r.provisional,
    r.total_votes
from {{ source('landing', 'nc_results') }} as r
inner join {{ ref('stg_nc_releases') }} as m
    on r.release_id = m.release_id
where m.dataset = 'election_results'