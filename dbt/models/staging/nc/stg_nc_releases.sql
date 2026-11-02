select
    r.release_id,
    r.snapshot_id,
    s.source_id,
    s.state,
    s.election_date,
    s.dataset,
    f.raw_sha256,
    r.loaded_at,
    r.expected_rows,
    r.loaded_rows,
    (r.parser_report ->> 'all_counts_zero')::boolean as all_counts_zero,
    r.parser_report -> 'reference_comparison' ->> 'status' as reference_status
from {{ source('ops', 'processing_releases') }} as r
inner join {{ source('ops', 'snapshots') }} as f
    on r.snapshot_id = f.snapshot_id
inner join {{ source('ops', 'sources') }} as s
    on f.source_id = s.source_id
where r.loaded_at is not null
  and s.source_id in (
      '{{ var("nc_results_source") }}',
      '{{ var("nc_demographics_source") }}'
  )
{% if var('publication_release_id', none) is not none %}
  and r.release_id = {{ var('publication_release_id') | int }}
{% endif %}