-- Every ballot choice, write-ins included, with one party code (seeds/party_codes.csv)
-- and, for Senate and House candidates, their FEC ID. candidate_key is the key of
-- ref.candidates: "<contest_id>|<choice as the ballot prints it>".
select
    r.contest_id || '|' || r.choice as candidate_key,
    r.contest_id,
    c.state,
    c.office,
    r.choice,
    r.choice_party as party_label,
    case
        when r.choice ~* 'write[- ]?in|scatter' then 'WRITE_IN'
        when coalesce(r.choice_party, '') = '' then 'NONE'
        else coalesce(p.party, 'UNMAPPED')
    end as party,
    i.external_id as fec_id
from {{ ref('dim_contests') }} c
join {{ source('serving', 'contest_results') }} r
    on r.publication_id = c.publication_id and r.contest_id = c.contest_id
left join {{ ref('party_codes') }} p on p.label = r.choice_party
left join {{ source('ref', 'candidate_ids') }} i
    on i.candidate_key = r.contest_id || '|' || r.choice and i.system = 'fec'
