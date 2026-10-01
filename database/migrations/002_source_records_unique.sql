-- Migration 002: Ensure uniqueness for (source_identifier, row_index)
DROP INDEX IF EXISTS idx_source_records_ident_row;
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_records_ident_row ON source_records(source_identifier, row_index);
