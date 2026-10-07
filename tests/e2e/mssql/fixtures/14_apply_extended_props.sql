-- MSSQL E2E Fixture — 14_apply_extended_props.sql
-- Extended properties (MS_Description) — MSSQL equivalent of COMMENT ON.
-- sp_addextendedproperty requires separate calls per object/column.

-- Schema description
EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'E2E training schema for migration validation',
    @level0type = N'SCHEMA', @level0name = 'training';

-- Table descriptions
EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Customer master data for E2E validation',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'TABLE', @level1name = 'Customers';

EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Product catalog for E2E validation',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'TABLE', @level1name = 'Products';

EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Order header records',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'TABLE', @level1name = 'Orders';

EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Order line items with computed LineTotal',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'TABLE', @level1name = 'OrderDetails';

EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Audit log populated by trigger on Orders',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'TABLE', @level1name = 'OrderAudit';

-- Column descriptions
EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Unique customer email address',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'TABLE', @level1name = 'Customers',
    @level2type = N'COLUMN', @level2name = 'Email';

EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Primary key identity column',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'TABLE', @level1name = 'Customers',
    @level2type = N'COLUMN', @level2name = 'CustomerID';

EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Computed line total = Quantity * UnitPrice',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'TABLE', @level1name = 'OrderDetails',
    @level2type = N'COLUMN', @level2name = 'LineTotal';

-- View description
EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Customer order summary with aggregate totals',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'VIEW', @level1name = 'vw_CustomerOrders';

-- Function description
EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Calculates 8 percent tax on an amount',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'FUNCTION', @level1name = 'fn_CalculateTax';

-- Procedure description
EXEC sp_addextendedproperty
    @name = N'MS_Description', @value = N'Retrieves orders for a given customer',
    @level0type = N'SCHEMA', @level0name = 'training',
    @level1type = N'PROCEDURE', @level1name = 'usp_GetCustomerOrders';

PRINT 'Extended properties applied.';
