select
    release_id,
    source_id,
    election_date,
    state,
    county,
    contest_id,
    choice,
    choice_party,
    sum(election_day)::bigint as election_day,
    sum(early_voting)::bigint as early_voting,
    sum(absentee_by_mail)::bigint as absentee_by_mail,
    sum(provisional)::bigint as provisional,
    sum(total_votes)::bigint as total_votes
from {{ ref('stg_nc_results') }}
group by
    release_id,
    source_id,
    election_date,
    state,
    county,
    contest_id,
    choice,
    choice_party