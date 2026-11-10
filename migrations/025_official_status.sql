-- A publication may be official: the state's own source marks the snapshot as its
-- certified or final results, and an operator has reviewed it (`deploy certify`).
BEGIN;

ALTER TABLE serving.publications DROP CONSTRAINT publications_status_check;
ALTER TABLE serving.publications
    ADD CONSTRAINT publications_status_check CHECK (status IN ('test', 'unofficial', 'official'));

ALTER TABLE ops.publication_attempts DROP CONSTRAINT publication_attempts_requested_status_check;
ALTER TABLE ops.publication_attempts
    ADD CONSTRAINT publication_attempts_requested_status_check
    CHECK (requested_status IN ('test', 'unofficial', 'official'));

COMMIT;
