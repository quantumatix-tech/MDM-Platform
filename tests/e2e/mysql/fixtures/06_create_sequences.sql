-- MySQL E2E Fixture — 06_create_sequences.sql
-- MySQL has no standalone sequence objects — AUTO_INCREMENT is table-bound.
-- The Orders table's auto-increment is used in lieu of a sequence.
-- This placeholder is kept for structural parity with the PostgreSQL fixture.

SELECT 'MySQL has no standalone sequences; AUTO_INCREMENT columns serve this role.' AS 'info';