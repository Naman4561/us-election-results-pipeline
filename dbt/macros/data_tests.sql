{% test unique_combination(model, columns) %}

select
    {{ columns | join(', ') }},
    count(*) as records
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1

{% endtest %}


{% test nonnegative_columns(model, columns) %}

select *
from {{ model }}
where
{% for column in columns %}
    {{ column }} is null or {{ column }} < 0
    {% if not loop.last %}or{% endif %}
{% endfor %}

{% endtest %}


{% test vote_methods_match_total(model) %}

select *
from {{ model }}
where election_day + early_voting + absentee_by_mail + provisional
      is distinct from total_votes

{% endtest %}


{% test reconciles_to(model, input_model, dimensions, measures) %}

with expected as (
    select
        {{ dimensions | join(', ') }},
        {% for measure in measures %}
        sum({{ measure }}) as {{ measure }}{% if not loop.last %},{% endif %}
        {% endfor %}
    from {{ input_model }}
    group by {{ dimensions | join(', ') }}
)

select
    coalesce(e.release_id, a.release_id) as release_id
from expected as e
full outer join {{ model }} as a
    on e.release_id = a.release_id
    {% for column in dimensions if column != 'release_id' %}
       and e.{{ column }} is not distinct from a.{{ column }}
    {% endfor %}
where e.release_id is null
   or a.release_id is null
    {% for measure in measures %}
    or e.{{ measure }} is distinct from a.{{ measure }}
    {% endfor %}

{% endtest %}


{% test vote_shares_match_counts(model) %}

with checked as (
    select
        *,
        sum(total_votes) over (
            partition by release_id, contest_id
        ) as expected_denominator
    from {{ model }}
)

select *
from checked
where contest_votes is distinct from expected_denominator
   or vote_share is distinct from
      total_votes::numeric / nullif(expected_denominator, 0)

{% endtest %}