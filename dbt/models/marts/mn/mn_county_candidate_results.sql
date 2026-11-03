select release_id, source_id, election_date, state, county, contest_id,
       choice, choice_party, total_votes
from {{ ref('stg_mn_results') }} where unit_type='county'
