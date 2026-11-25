-- Ranked-choice rounds by county (or other local unit), for a state whose tabulation
-- report breaks each round out by place, so the map can follow the rounds. A statewide
-- round has county ''; Alaska's report (2026) is statewide only.
BEGIN;

ALTER TABLE landing.rcv_rounds ADD COLUMN county text NOT NULL DEFAULT '';
ALTER TABLE landing.rcv_rounds
    DROP CONSTRAINT rcv_rounds_release_id_contest_id_round_kind_choice_key;
ALTER TABLE landing.rcv_rounds
    ADD UNIQUE (release_id, county, contest_id, round, kind, choice);

ALTER TABLE serving.rcv_rounds ADD COLUMN county text NOT NULL DEFAULT '';
ALTER TABLE serving.rcv_rounds DROP CONSTRAINT rcv_rounds_pkey;
ALTER TABLE serving.rcv_rounds
    ADD PRIMARY KEY (publication_id, county, contest_id, round, kind, choice);

COMMIT;
