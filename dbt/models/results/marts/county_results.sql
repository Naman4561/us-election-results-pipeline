select release_id, state, county, contest_id, choice, choice_party,
       {{ known_sum('total_votes') }} as total_votes
from {{ ref('stg_county_rows') }}
group by release_id, state, county, contest_id, choice, choice_party
