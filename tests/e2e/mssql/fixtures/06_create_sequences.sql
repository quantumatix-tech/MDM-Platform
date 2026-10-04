-- MSSQL E2E Fixture — 06_create_sequences.sql
-- A sequence used to populate Order.OrderNumber at insert time.

CREATE SEQUENCE training.Seq_OrderNumber
    AS BIGINT
    START WITH 1000
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    NO CYCLE
    CACHE 10;

PRINT 'Sequence [training.Seq_OrderNumber] created.';
