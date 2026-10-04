-- MSSQL E2E Fixture — 11_create_synonyms.sql
-- Synonyms that alias real objects in the training schema.

CREATE SYNONYM training.syn_Orders      FOR training.Orders;
CREATE SYNONYM training.syn_OrderDetails FOR training.OrderDetails;

PRINT 'Synonyms created.';
