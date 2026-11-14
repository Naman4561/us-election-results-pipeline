-- One row per race in each state's current publication: Senate, Governor and House.
select distinct
    r.contest_id,
    p.state,
    case
        when r.contest_id like '%-SEN' then 'Senate'
        when r.contest_id like '%-GOV' then 'Governor'
        else 'House'
    end as office,
    -- The House district ("01"; "AL" at large), null for statewide races.
    case when r.contest_id !~ '-(SEN|GOV)$' then split_part(r.contest_id, '-', 2) end as district,
    p.status,
    p.source_id,
    p.publication_id,
    p.published_at,
    p.votes_changed_at
from {{ source('ref', 'current_publications') }} p
join {{ source('serving', 'contest_results') }} r using (publication_id)
