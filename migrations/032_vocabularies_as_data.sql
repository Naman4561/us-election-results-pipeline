-- Two lists the schema used to hold, now the election definition's.
--
-- Vote methods: the names a state's results may be split into are in config/methods.toml, and the
-- election definition refuses a state that uses another. The schema keeps only the shape of a
-- name, so a state with groups of its own needs a reviewed line there, not a migration (Arizona
-- needed 028).
--
-- Contest IDs: a race may carry a suffix for a special election held beside the regular one
-- (GA-SEN-S next to GA-SEN), which the State model lists as a special race.
--
-- Both only relax a constraint: every value the previous release writes still passes, so a
-- rollback to it still loads and publishes.
BEGIN;

ALTER TABLE landing.result_methods DROP CONSTRAINT result_methods_method_check;
ALTER TABLE landing.result_methods
    ADD CONSTRAINT result_methods_method_check CHECK (method ~ '^[a-z][a-z0-9_]{1,40}$');

ALTER TABLE landing.results DROP CONSTRAINT results_contest_id_check;
ALTER TABLE landing.results
    ADD CONSTRAINT results_contest_id_check
    CHECK (contest_id ~ '^[A-Z]{2}-(SEN|GOV|AL|[0-9]{2})(-[A-Z0-9]{1,4})?$');

ALTER TABLE landing.rcv_rounds DROP CONSTRAINT rcv_rounds_contest_id_check;
ALTER TABLE landing.rcv_rounds
    ADD CONSTRAINT rcv_rounds_contest_id_check
    CHECK (contest_id ~ '^[A-Z]{2}-(SEN|GOV|AL|[0-9]{2})(-[A-Z0-9]{1,4})?$');

COMMIT;
