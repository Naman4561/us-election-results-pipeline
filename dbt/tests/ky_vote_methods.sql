{{ config(tags=['state_ky']) }}
select release_id from {{ ref('stg_ky_results') }}
where election_day < 0 or absentee_combined < 0 or total_votes < 0
   or election_day + absentee_combined <> total_votes
