select r.*, m.source_id, m.election_date, m.state
from {{ source('ne', 'results') }} r
join {{ ref('stg_ne_releases') }} m using (release_id)
where m.dataset = 'election_results'
