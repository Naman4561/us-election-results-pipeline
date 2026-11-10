-- An official publication is marked official by the state's own source, or attested
-- by an operator: the state certified these results and the operator checked them
-- (`deploy certify --approve-snapshot` or `--certified-file`). The attestation records
-- who, when, and the state's own certification notice.
BEGIN;

ALTER TABLE ops.publication_attempts ADD COLUMN attestation jsonb;
ALTER TABLE ops.publication_attempts
    ADD CONSTRAINT publication_attempts_attestation_check
    CHECK (attestation IS NULL OR requested_status = 'official');

ALTER TABLE serving.publications ADD COLUMN attestation jsonb;
ALTER TABLE serving.publications
    ADD CONSTRAINT publications_attestation_check
    CHECK (attestation IS NULL OR status = 'official');

COMMIT;
