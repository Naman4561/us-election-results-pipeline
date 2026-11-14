-- Each Senate and House candidate's FEC money: their committee's totals (newest FEC
-- download) and the independent expenditures for and against them, counted only after
-- review (finance.independent_expenditures_reviewed: amendments replaced, repeated reports
-- once, junk filers excluded). Totals run through coverage_end.
with totals as (
    select t.*
    from {{ source('finance', 'fec_candidate_totals') }} t
    where t.dataset_id = (
        select dataset_id from {{ source('provenance', 'datasets') }}
        where name = 'fec_totals_2026' order by downloaded_at desc limit 1
    )
),

outside as (
    select
        cand_id,
        sum(amount) filter (where support_oppose = 'S') as outside_for,
        sum(amount) filter (where support_oppose = 'O') as outside_against
    from {{ source('finance', 'independent_expenditures_reviewed') }}
    where counted
    group by cand_id
)

select
    d.candidate_key,
    d.contest_id,
    d.fec_id,
    t.coverage_end,
    coalesce(t.receipts, 0) as receipts,
    coalesce(t.disbursements, 0) as disbursements,
    coalesce(t.cash_on_hand_end, 0) as cash_on_hand,
    coalesce(t.individual_contributions, 0) as individual_contributions,
    coalesce(t.candidate_contributions + t.candidate_loans, 0) as self_funding,
    coalesce(t.party_contributions, 0) as party_contributions,
    coalesce(t.other_committee_contributions, 0) as committee_contributions,
    coalesce(o.outside_for, 0) as outside_for,
    coalesce(o.outside_against, 0) as outside_against
from {{ ref('dim_candidates') }} d
left join totals t on t.cand_id = d.fec_id
left join outside o on o.cand_id = d.fec_id
where d.office in ('Senate', 'House') and d.fec_id is not null
