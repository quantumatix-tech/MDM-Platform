-- MSSQL E2E Fixture — 02_create_types.sql
-- User-defined (alias) data types.
-- CREATE TYPE must be the only statement in its batch, hence the GO separators.

CREATE TYPE training.CustomerCode FROM VARCHAR(20) NOT NULL;
GO

PRINT 'Type [training.CustomerCode] created.';
