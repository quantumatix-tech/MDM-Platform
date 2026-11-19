# DB2 E2E Execution Guide

1. Open Db2 Command Window.
2. Run 01_source_fixture.sql against E2ESRC01.
3. Run 02_target_prerequisites.sql against E2ETGT01.
4. Run 03_permissions.sql on both databases.
5. Run 04_verify_source.sql.
6. Run migration: python -m migration_platform --config config/db2_e2e.yaml --mode full
7. Run 05_verify_target.sql.
8. Repeat migration for idempotency (trigger scenario blocked).
