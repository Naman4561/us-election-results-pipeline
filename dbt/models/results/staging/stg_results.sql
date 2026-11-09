select r.*, m.source_id, m.election_date
from {{ source('landing', 'results') }} r
join {{ ref('stg_releases') }} m using (release_id)
