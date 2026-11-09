{{ config(tags=['results']) }}
{% set methods = var('methods') | sort %}
-- Each row carries exactly the state's methods, adding up to its total; or none at all.
with rows as (
    select release_id, source_line, count(*) as methods, sum(votes) as votes,
           array_agg(method order by method)::text[] as names
    from {{ ref('stg_result_methods') }} group by release_id, source_line
)
select r.release_id, r.source_line
from {{ ref('stg_results') }} r
left join rows m using (release_id, source_line)
{% if methods %}
where m.methods is null or m.votes is distinct from r.total_votes
   or m.names <> array[{% for m in methods %}'{{ m }}'{% if not loop.last %}, {% endif %}{% endfor %}]::text[]
{% else %}
where m.methods is not null
{% endif %}
