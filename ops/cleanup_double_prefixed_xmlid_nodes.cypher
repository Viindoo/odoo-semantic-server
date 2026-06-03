// Cleanup: remove double-prefixed View/QWebTmpl phantom nodes left by the pre-fix
// xmlid bug (parser_xml/parser_qweb used to do f"{module}.{id}" even when `id` was
// already module-qualified → produced `M.M.rest` nodes, e.g.
// `website_blog.website_blog.blog_post_complete`).
//
// Root cause fixed in src/indexer/_xmlid.qualify_xmlid (Odoo's external-id rule).
// After deploying the fix AND re-running `index-repo --all --full --no-embed --gc`,
// the correctly-keyed `M.rest` node is (re)created and children resolve to it; the
// stale `M.M.rest` node is an orphan duplicate that --gc does NOT remove (it is not
// an `__unresolved__` placeholder). This script deletes those phantoms.
//
// Safe: the `M.M.` prefix (module name repeated) uniquely identifies the bug
// artifact — the correct qualifier never produces it. Idempotent. Run AFTER the
// post-fix re-index. (Mirrors the ADR-0037 ops/cleanup_absolute_path_nodes.cypher
// precedent.)

// 1) Pre-count (inspect before deleting):
//    MATCH (n) WHERE (n:View OR n:QWebTmpl) AND n.module IS NOT NULL
//      AND n.xmlid STARTS WITH (n.module + '.' + n.module + '.')
//    RETURN labels(n) AS labels, n.odoo_version AS v, count(*) AS n ORDER BY v;

// 2) Delete the phantoms:
MATCH (n)
WHERE (n:View OR n:QWebTmpl)
  AND n.module IS NOT NULL
  AND n.xmlid STARTS WITH (n.module + '.' + n.module + '.')
DETACH DELETE n;
