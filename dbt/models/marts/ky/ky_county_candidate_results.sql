select release_id, source_id, election_date, state, county, contest_id,
       choice, choice_party, election_day, absentee_combined, total_votes
from {{ ref('stg_ky_results') }}
where unit_type = 'county'
