-- MySQL E2E Fixture — 00_reset.sql
-- Drops and recreates the training database so the fixture is reproducible
-- on re-runs without a full database reset.
--
-- NOTE: For MySQL, "schema" == "database".  The E2E framework runs fixtures
-- against the MigrationE2E_MySQL_Source database, so this script drops all
-- user objects in that database rather than dropping the database itself.

SET FOREIGN_KEY_CHECKS = 0;

DROP TABLE IF EXISTS `vw_OrderSummary`;
DROP VIEW IF EXISTS `vw_OrderSummary`;
DROP TABLE IF EXISTS `orderdetails`;
DROP TABLE IF EXISTS `orders`;
DROP TABLE IF EXISTS `orderaudit`;
DROP TABLE IF EXISTS `partitionedorders`;
DROP TABLE IF EXISTS `pk_name_test`;
DROP TABLE IF EXISTS `products`;
DROP TABLE IF EXISTS `customers`;

DROP PROCEDURE IF EXISTS `sp_UpdateProductStock`;
DROP FUNCTION IF EXISTS `fn_GetOrderTotal`;
DROP TRIGGER IF EXISTS `tr_AuditOrder`;
DROP TRIGGER IF EXISTS `tr_AuditOrder_Insert`;

SET FOREIGN_KEY_CHECKS = 1;

-- MySQL has no PRINT statement; SELECT with a string literal outputs to the console.
SELECT 'Schema [training] reset.' AS 'info';
