-- Ranked-choice tabulations: each round of a race a state decides by instant runoff
-- (Alaska's U.S. House race, parsers/ak_rcv.py), beside its first-choice results.
BEGIN;

-- A release's rounds as the state's tabulation report prints them: per round, each
-- candidate still in it and its outcome after that round, then the round's blanks,
-- exhausted ballots and overvotes. Every round holds the same ballots.
CREATE TABLE landing.rcv_rounds (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    state text NOT NULL CHECK (state ~ '^[A-Z]{2}$'),
    contest_id text NOT NULL CHECK (contest_id ~ '^[A-Z]{2}-(SEN|GOV|AL|[0-9]{2})$'),
    round integer NOT NULL CHECK (round >= 1),
    kind text NOT NULL CHECK (kind IN ('candidate', 'blanks', 'exhausted', 'overvotes')),
    choice text NOT NULL,
    votes bigint NOT NULL CHECK (votes >= 0),
    outcome text CHECK (outcome IN ('continuing', 'eliminated', 'elected', 'defeated')),
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, contest_id, round, kind, choice),
    CHECK (left(contest_id, 2) = state),
    CHECK ((kind = 'candidate') = (btrim(choice) <> '')),
    CHECK ((kind = 'candidate') = (outcome IS NOT NULL))
);

-- The rounds a publication froze, read by the dashboard.
CREATE TABLE serving.rcv_rounds (
    publication_id uuid NOT NULL,
    state text NOT NULL,
    contest_id text NOT NULL,
    round integer NOT NULL CHECK (round >= 1),
    kind text NOT NULL CHECK (kind IN ('candidate', 'blanks', 'exhausted', 'overvotes')),
    choice text NOT NULL,
    votes bigint NOT NULL CHECK (votes >= 0),
    outcome text CHECK (outcome IN ('continuing', 'eliminated', 'elected', 'defeated')),
    PRIMARY KEY (publication_id, contest_id, round, kind, choice),
    FOREIGN KEY (publication_id, state) REFERENCES serving.publications (publication_id, state)
);

GRANT SELECT ON serving.rcv_rounds TO election_dashboard;

COMMIT;
