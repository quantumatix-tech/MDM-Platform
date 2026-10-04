-- PostgreSQL E2E Fixture — 02_create_types.sql
-- Custom ENUM type used by the Orders table for a typed status column.

CREATE TYPE training.OrderStatus AS ENUM (
    'pending',
    'processing',
    'shipped',
    'delivered',
    'cancelled'
);

\echo 'Custom type [training.OrderStatus] created.'
