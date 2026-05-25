// Cleanup stale ABSOLUTE-path nodes after the ADR-0037 path-portability reindex.
//
// WHY: Stylesheet and LintViolation use file_path INSIDE their composite MERGE
// key. After the indexer switched to storing repo-relative paths (ADR-0037), a
// full reindex creates NEW nodes keyed on the relative path, while the OLD nodes
// keyed on the absolute path (starting with "/") remain as orphans. Other node
// types (Module.path, OWLComp/JSPatch/CoreSymbol/CLICommand.file_path) are SET
// after MERGE, so a reindex overwrites them in place — they need no cleanup.
//
// WHEN: Run ONCE, AFTER a full `--full` reindex v8→v19 has completed for ALL
// repos (so every live stylesheet/violation now has a relative-keyed node).
// Running it before/between reindex would delete still-valid nodes.
//
// SAFETY: idempotent. On a clean (relative-only) graph these match nothing and
// DETACH DELETE is a no-op. Diagnose first with the SELECT-style counts below.

// --- Diagnose (run first; expect 0 after a clean reindex) -------------------
// MATCH (ss:Stylesheet)   WHERE ss.file_path STARTS WITH '/' RETURN count(ss) AS stale_stylesheets;
// MATCH (lv:LintViolation) WHERE lv.file_path STARTS WITH '/' RETURN count(lv) AS stale_violations;

// --- Cleanup ----------------------------------------------------------------
MATCH (ss:Stylesheet)
WHERE ss.file_path STARTS WITH '/'
DETACH DELETE ss;

MATCH (lv:LintViolation)
WHERE lv.file_path STARTS WITH '/'
DETACH DELETE lv;

// --- Verify (must both be 0) ------------------------------------------------
// MATCH (ss:Stylesheet)   WHERE ss.file_path STARTS WITH '/' RETURN count(ss) AS remaining_stylesheets;
// MATCH (lv:LintViolation) WHERE lv.file_path STARTS WITH '/' RETURN count(lv) AS remaining_violations;
// Postgres embeddings check (run in psql, must be 0):
//   SELECT count(*) FROM embeddings WHERE file_path LIKE '/%';
