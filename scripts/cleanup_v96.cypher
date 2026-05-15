// Cleanup test data leak: Module/Model/Field/etc at odoo_version='96.0'
// Dry-run confirmed: 1 isolated Module node, 0 connected relationships
// Safe to run idempotently

MATCH (n) WHERE n.odoo_version = '96.0'
DETACH DELETE n;
