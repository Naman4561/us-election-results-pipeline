{#- The release being published (or every loaded release in a development build). -#}
{% macro releases(dataset) %}
select r.release_id, r.snapshot_id, s.source_id, s.state, s.election_date, s.dataset,
       f.raw_sha256, r.loaded_at, r.expected_rows, r.loaded_rows,
       (r.parser_report ->> 'all_counts_zero')::boolean as all_counts_zero,
       r.parser_report -> 'reference_comparison' ->> 'status' as reference_status
from {{ source('ops', 'processing_releases') }} r
join {{ source('ops', 'snapshots') }} f on r.snapshot_id = f.snapshot_id
join {{ source('ops', 'sources') }} s on f.source_id = s.source_id
where r.loaded_at is not null and s.dataset = '{{ dataset }}'
{% if var('publication_release_id') is not none %}
  and r.release_id = {{ var('publication_release_id') | int }}
{% endif %}
{% endmacro %}

{#- An unknown count anywhere in a group leaves the group's total unknown. -#}
{% macro known_sum(column) -%}
case when count({{ column }}) = count(*) then sum({{ column }}) end::bigint
{%- endmacro %}
