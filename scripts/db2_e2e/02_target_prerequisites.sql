-- scripts/db2_e2e/02_target_prerequisites.sql
-- Run against E2ETGT01 as DB2ADMIN/ADMIN.
-- Pre-create only objects the coordinator cannot create automatically.

CREATE SCHEMA TESTE2E;

-- UDT required before dependent table
CREATE DISTINCT TYPE TESTE2E.EMP_CODE AS INTEGER;

-- Partitioned table must be manually pre-created
CREATE TABLE TESTE2E.PART (
  EMP_ID INTEGER NOT NULL,
  EMP_NAME VARCHAR(50),
  SALARY INTEGER
) PARTITION BY RANGE (EMP_ID)
  (STARTING FROM (1) ENDING AT (100) INCLUSIVE,
   STARTING FROM (101) ENDING AT (200) INCLUSIVE);
