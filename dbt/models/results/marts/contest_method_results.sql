with published as (
    select r.release_id, r.contest_id, r.choice, m.method, m.votes
    from {{ ref('stg_results') }} r
    join {{ ref('stg_result_methods') }} m using (release_id, source_line)
    where r.unit_type = 'state'
), derived as (
    select c.release_id, c.contest_id, c.choice, c.method, sum(c.votes)::bigint as votes
    from {{ ref('county_method_results') }} c
    where not exists (
        select 1 from {{ ref('stg_results') }} p
        where p.release_id = c.release_id and p.contest_id = c.contest_id
          and p.unit_type = 'state'
    )
    group by c.release_id, c.contest_id, c.choice, c.method
)
select * from published union all select * from derived
