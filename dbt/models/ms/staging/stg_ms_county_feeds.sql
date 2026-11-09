select f.*, r.source_id, r.election_date, r.state
from {{ source('ms', 'county_feeds') }} f
join {{ ref('stg_releases') }} r using (release_id)
