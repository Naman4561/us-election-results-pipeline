-- Arizona's vote groups. Its early ballots (mailed, dropped off or cast at an early
-- voting site) are one group, "Early Voting", which is not in-person early voting
-- (early_voting) or mail alone (mail); with them come two groups Arizona names "Early
-- A.R.S. 16-579" and "Late Early*". Stored under Arizona's own names (parsers/az.py).
BEGIN;

ALTER TABLE landing.result_methods DROP CONSTRAINT result_methods_method_check;
ALTER TABLE landing.result_methods ADD CONSTRAINT result_methods_method_check CHECK (method IN (
    'election_day', 'early_voting', 'absentee_by_mail', 'absentee_combined',
    'provisional', 'post_election', 'mail', 'in_person',
    'early_ballot', 'early_ars_16_579', 'late_early'
));

COMMIT;
