select
    release_id,
    source_id,
    election_date,
    state,
    request_type,
    party_registration,
    sex,
    race,
    ethnicity,
    sum(ballot_count)::bigint as ballot_count
from {{ ref('stg_nc_absentee_demographics') }}
group by
    release_id,
    source_id,
    election_date,
    state,
    request_type,
    party_registration,
    sex,
    race,
    ethnicity