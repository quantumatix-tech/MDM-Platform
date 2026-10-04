-- MSSQL E2E Fixture — 12_create_partitions.sql
-- Partition function, partition scheme, and a partitioned table.
-- Partition functions and schemes are database-level objects (no schema).
--
-- SQL Server rule: the partition column must be part of every unique index,
-- including the primary key.  PK_PartitionedOrders includes OrderDate.

CREATE PARTITION FUNCTION PF_OrderDate (DATE)
AS RANGE RIGHT
FOR VALUES ('2025-01-01', '2025-04-01', '2025-07-01', '2025-10-01');
GO

CREATE PARTITION SCHEME PS_OrderDate
AS PARTITION PF_OrderDate
ALL TO ([PRIMARY]);
GO

CREATE TABLE training.PartitionedOrders (
    PartitionID INT IDENTITY(1,1) NOT NULL,
    OrderDate   DATE NOT NULL,
    Amount      DECIMAL(10,2) NOT NULL,
    Status      VARCHAR(50) NOT NULL CONSTRAINT DF_PartitionedOrders_Status DEFAULT ('pending'),
    CONSTRAINT PK_PartitionedOrders PRIMARY KEY (PartitionID, OrderDate)
) ON PS_OrderDate(OrderDate);
GO

PRINT 'Partition function, scheme, and table created.';
