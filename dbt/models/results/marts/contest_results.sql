with published as (
    select release_id, state, contest_id, choice, choice_party, total_votes
    from {{ ref('stg_results') }} where unit_type = 'state'
), derived as (
    select c.release_id, c.state, c.contest_id, c.choice, c.choice_party,
           {{ known_sum('c.total_votes') }} as total_votes
    from {{ ref('county_results') }} c
    where not exists (
        select 1 from published p
        where p.release_id = c.release_id and p.contest_id = c.contest_id
    )
    group by c.release_id, c.state, c.contest_id, c.choice, c.choice_party
), totals as (
    select * from published union all select * from derived
), denominators as (
    select *, sum(total_votes) over (partition by release_id, contest_id) as contest_votes
    from totals
)
select *, total_votes::numeric / nullif(contest_votes, 0) as vote_share from denominators
