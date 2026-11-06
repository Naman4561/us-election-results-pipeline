{{ config(tags=['state_nm']) }}
-- New Mexico hides small county counts for voter privacy ("*" in its county exports).
-- The parser leaves such a county out of that race and checks every printed count exactly,
-- so stored county rows may add up to less than an official total, but never more, and
-- never name a candidate the official contest lacks.
with counties as (
    select release_id, contest_id, choice, coalesce(choice_party,'') as party, sum(total_votes) as votes
    from {{ ref('nm_county_candidate_results') }} group by 1,2,3,4
), official as (
    select release_id, contest_id, choice, coalesce(choice_party,'') as party, total_votes as votes
    from {{ ref('nm_contest_candidate_results') }}
)
select c.release_id, c.contest_id, c.choice
from counties c left join official s using (release_id,contest_id,choice,party)
where s.votes is null or c.votes > s.votes
