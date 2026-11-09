select d.*, m.source_id, m.election_date, m.state
from {{ source('nc', 'absentee_demographics') }} d
join {{ ref('stg_demographic_releases') }} m using (release_id)
