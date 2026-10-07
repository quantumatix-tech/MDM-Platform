-- MSSQL E2E Fixture — 13_create_security.sql
-- Database roles, a contained user, role membership, and grants.
-- Roles and users are database principals — no schema qualifier.

CREATE ROLE Role_ReadOnly;
GO

CREATE ROLE Role_DataWriter;
GO

-- Contained user (no server login required)
CREATE USER E2E_TestUser WITHOUT LOGIN;
GO

-- Role membership
ALTER ROLE Role_ReadOnly ADD MEMBER E2E_TestUser;
GO

-- Grants
GRANT SELECT ON training.Customers TO Role_ReadOnly;
GRANT SELECT ON training.Products TO Role_ReadOnly;
GRANT SELECT ON training.Orders TO Role_ReadOnly;
GRANT SELECT ON training.vw_CustomerOrders TO Role_ReadOnly;

GRANT SELECT, INSERT, UPDATE, DELETE ON training.OrderDetails TO Role_DataWriter;
GRANT SELECT, INSERT, UPDATE, DELETE ON training.Orders TO Role_DataWriter;
GRANT SELECT, INSERT ON training.OrderAudit TO Role_DataWriter;

GRANT EXECUTE ON training.usp_GetCustomerOrders TO Role_ReadOnly;
GRANT EXECUTE ON training.fn_CalculateTax TO Role_ReadOnly;

PRINT 'Security objects created.';
