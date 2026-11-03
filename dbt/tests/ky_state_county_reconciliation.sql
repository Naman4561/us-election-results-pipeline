{{ config(tags=['state_ky']) }}
with official as (
    select release_id, contest_id, choice, choice_party, election_day, absentee_combined, total_votes
    from {{ ref('stg_ky_results') }} where unit_type='state'
)
select coalesce(a.release_id,b.release_id) as release_id
from official a full join {{ ref('ky_contest_candidate_results') }} b
on a.release_id=b.release_id and a.contest_id=b.contest_id and a.choice=b.choice
and a.choice_party is not distinct from b.choice_party
where a.release_id is null or b.release_id is null
   or (a.election_day,a.absentee_combined,a.total_votes)
      is distinct from (b.election_day,b.absentee_combined,b.total_votes)
