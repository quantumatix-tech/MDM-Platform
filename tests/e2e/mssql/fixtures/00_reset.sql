-- MSSQL E2E Fixture — 00_reset.sql
-- Drops all fixture objects within the current database.
-- Safe to run repeatedly; every statement is guarded by IF EXISTS / OBJECT_ID.
--
-- Object drop order (reverse dependency):
--   triggers → procedures → functions → views → synonyms
--   → partitioned table → partition scheme → partition function
--   → tables → sequences → user-defined types → users → roles → schema

-- Triggers
IF OBJECT_ID('training.trg_Orders_Insert', 'TR') IS NOT NULL DROP TRIGGER training.trg_Orders_Insert;
IF OBJECT_ID('training.trg_OrderDetails_Audit', 'TR') IS NOT NULL DROP TRIGGER training.trg_OrderDetails_Audit;
GO

-- Stored procedures
IF OBJECT_ID('training.usp_GetCustomerOrders', 'P') IS NOT NULL DROP PROCEDURE training.usp_GetCustomerOrders;
GO

-- Functions (scalar, inline TVF, table-valued)
IF OBJECT_ID('training.fn_CalculateTax', 'FN') IS NOT NULL DROP FUNCTION training.fn_CalculateTax;
IF OBJECT_ID('training.fn_CustomerOrderStats', 'IF') IS NOT NULL DROP FUNCTION training.fn_CustomerOrderStats;
GO

-- Views
IF OBJECT_ID('training.vw_CustomerOrders', 'V') IS NOT NULL DROP VIEW training.vw_CustomerOrders;
GO

-- Synonyms
IF OBJECT_ID('training.syn_Orders', 'SN') IS NOT NULL DROP SYNONYM training.syn_Orders;
IF OBJECT_ID('training.syn_OrderDetails', 'SN') IS NOT NULL DROP SYNONYM training.syn_OrderDetails;
GO

-- Partitioned table (must drop before partition scheme)
IF OBJECT_ID('training.PartitionedOrders', 'U') IS NOT NULL DROP TABLE training.PartitionedOrders;
GO

-- Partition scheme
IF EXISTS (SELECT * FROM sys.partition_schemes WHERE name = 'PS_OrderDate') DROP PARTITION SCHEME PS_OrderDate;
GO

-- Partition function
IF EXISTS (SELECT * FROM sys.partition_functions WHERE name = 'PF_OrderDate') DROP PARTITION FUNCTION PF_OrderDate;
GO

-- Tables (DROP TABLE cascades constraints, indexes, triggers on that table)
IF OBJECT_ID('training.OrderDetails', 'U') IS NOT NULL DROP TABLE training.OrderDetails;
IF OBJECT_ID('training.Orders', 'U') IS NOT NULL DROP TABLE training.Orders;
IF OBJECT_ID('training.OrderAudit', 'U') IS NOT NULL DROP TABLE training.OrderAudit;
IF OBJECT_ID('training.Products', 'U') IS NOT NULL DROP TABLE training.Products;
IF OBJECT_ID('training.Customers', 'U') IS NOT NULL DROP TABLE training.Customers;
IF OBJECT_ID('training.PK_Name_Test', 'U') IS NOT NULL DROP TABLE training.PK_Name_Test;
GO

-- Sequences
IF EXISTS (SELECT 1 FROM sys.sequences WHERE NAME = 'Seq_OrderNumber' AND schema_id = SCHEMA_ID('training'))
    DROP SEQUENCE training.Seq_OrderNumber;
GO

-- User-defined types (alias types must be dropped after tables that use them)
IF TYPE_ID('training.CustomerCode') IS NOT NULL DROP TYPE training.CustomerCode;
GO

-- Users (must drop before roles to release role memberships)
IF EXISTS (SELECT * FROM sys.database_principals WHERE name = 'E2E_TestUser') DROP USER E2E_TestUser;
GO

-- Roles
IF EXISTS (SELECT * FROM sys.database_principals WHERE name = 'Role_DataWriter' AND type IN ('R','C')) DROP ROLE Role_DataWriter;
IF EXISTS (SELECT * FROM sys.database_principals WHERE name = 'Role_ReadOnly' AND type IN ('R','C')) DROP ROLE Role_ReadOnly;
GO

-- Extended properties on schema
IF EXISTS (SELECT 1 FROM sys.extended_properties WHERE class = 3 AND name = 'MS_Description' AND major_id = SCHEMA_ID('training'))
    EXEC sp_dropextendedproperty @name = N'MS_Description', @level0type = N'SCHEMA', @level0name = 'training';
GO

-- Schema
IF EXISTS (SELECT * FROM sys.schemas WHERE name = 'training') DROP SCHEMA training;
GO

PRINT 'E2E fixture reset complete.';
