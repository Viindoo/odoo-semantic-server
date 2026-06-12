# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pattern-feedback + admin-audit-log domain methods for AuthStore."""


class _FeedbackMixin:
    """Pattern feedback + (deprecated) admin audit-log SQL operations.

    Composed into AuthStore; relies on ``self._pool`` set by AuthStore.__init__.
    """

    # ------------------------------------------------------------------
    # Pattern feedback
    # ------------------------------------------------------------------

    def create_feedback(
        self,
        *,
        pattern_node_id: str,
        api_key_id: int | None,
        rating: str,
        comment: str | None = None,
    ) -> int:
        """Store a thumbs-up/down rating for a PatternExample node.

        Args:
            pattern_node_id: Neo4j node id or pattern_id string
                (e.g. 'python__write-read-before-super').
            api_key_id: Authenticated API key id (or None for anonymous).
            rating: 'up' or 'down'.
            comment: Optional free-text comment from the user.

        Returns:
            Integer id of the newly created pattern_feedback row.
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pattern_feedback (pattern_node_id, api_key_id, rating, comment) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (pattern_node_id, api_key_id, rating, comment),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return row_id

    def list_feedback(self, pattern_node_id: str) -> list[dict]:
        """Return all feedback entries for a given pattern node, newest first.

        Args:
            pattern_node_id: The pattern id to filter on.

        Returns:
            List of dicts with keys: id, api_key_id, rating, comment, created_at.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT id, api_key_id, rating, comment, created_at "
                "FROM pattern_feedback "
                "WHERE pattern_node_id = %s "
                "ORDER BY created_at DESC",
                (pattern_node_id,),
            )
        return [
            {
                "id": r["id"],
                "api_key_id": r["api_key_id"],
                "rating": r["rating"],
                "comment": r["comment"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # M9: Admin audit log
    # ------------------------------------------------------------------

    def log_audit(
        self,
        *,
        actor_id: int | None,
        action: str,
        target_id: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Insert an admin audit log entry. Fire-and-forget — swallows exceptions.

        DEPRECATED (M9 W-AL): Use src.db.audit.write_audit_log() directly instead.
        This method is kept for backward compatibility only and will be removed
        in a future major release.  Only the canonical columns (actor, action,
        target, success) are written; legacy columns were dropped by migration
        m9_010_drop_audit_legacy_columns.sql.

        New callers should use:
            from src.db.audit import write_audit_log
            write_audit_log(actor=f"user:{actor_id}", action=action, target=str(target_id))

        Args:
            actor_id: webui_users.id of the admin performing the action.
            action: Short action code (e.g. 'user.deactivate', 'user.reset_password').
            target_id: webui_users.id of the affected user (if applicable).
            detail: Accepted for backward compatibility; not persisted (INSERT omits this column).
        """
        try:
            with self._pool.checkout() as conn:
                # Canonical-only insert (M10 WI-4: legacy actor_id/target_id/detail_text
                # columns dropped by migration m9_010_drop_audit_legacy_columns.sql).
                actor_str = str(actor_id) if actor_id is not None else "system"
                target_str = str(target_id) if target_id is not None else None
                self._pool.execute(
                    conn,
                    "INSERT INTO admin_audit_log"
                    " (actor, action, target, success)"
                    " VALUES (%s, %s, %s, %s)",
                    (actor_str, action, target_str, True),
                )
        except Exception:
            pass  # best-effort — audit failure must not break the main action
