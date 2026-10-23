import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("aimportarr.database")

class DatabaseManager:
    """
    SQLite persistence manager for AImportarr.
    Stores processed media history, retry states, AI token usage, and aggregates.
    Supports automatic retention purge (default: 30 days) and WAL concurrency.
    """

    def __init__(self, db_path: str = "/config/aimportarr.db", retention_days: int = 30):
        self.raw_path = db_path
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self.db_path = self._resolve_db_path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _resolve_db_path(self, target_path: str) -> Path:
        p = Path(target_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # Test writability
            test_file = p.parent / ".write_test"
            test_file.touch()
            test_file.unlink(missing_ok=True)
            return p
        except (PermissionError, OSError) as e:
            fallback = Path("config/aimportarr.db")
            logger.warning(
                f"Cannot write to database path '{target_path}' ({e}). Falling back to local '{fallback.resolve()}'"
            )
            fallback.parent.mkdir(parents=True, exist_ok=True)
            return fallback

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=10.0
            )
            self._conn.row_factory = sqlite3.Row
            # Enable WAL mode for high concurrency and fast performance
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
        return self._conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_connection()
            with conn:
                # 1. Processed items table
                conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL,
                    model TEXT,
                    reasoning TEXT,
                    details TEXT,
                    retry_requested INTEGER DEFAULT 0,
                    timestamp TEXT NOT NULL
                );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_status ON processed_items(status);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_timestamp ON processed_items(timestamp);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_retry ON processed_items(retry_requested);")

                # 2. Processed item keys table (maps downloadId, path, and item keys to processed_items)
                conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_item_keys (
                    item_key TEXT NOT NULL,
                    processed_id INTEGER NOT NULL,
                    PRIMARY KEY (item_key, processed_id),
                    FOREIGN KEY (processed_id) REFERENCES processed_items(id) ON DELETE CASCADE
                );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_item_keys_key ON processed_item_keys(item_key);")

                # 3. Token usage history table
                conn.execute("""
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    thinking_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    duration_ms INTEGER DEFAULT 0,
                    item_title TEXT,
                    success INTEGER DEFAULT 1,
                    prompt_text TEXT DEFAULT '',
                    response_text TEXT DEFAULT ''
                );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_token_timestamp ON token_usage(timestamp);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_token_model ON token_usage(model);")

                # Migration: Ensure thinking_tokens, prompt_text, and response_text exist in existing databases
                try:
                    cursor = conn.execute("PRAGMA table_info(token_usage);")
                    col_names = [row["name"] for row in cursor.fetchall()]
                    if "thinking_tokens" not in col_names:
                        conn.execute("ALTER TABLE token_usage ADD COLUMN thinking_tokens INTEGER DEFAULT 0;")
                    if "prompt_text" not in col_names:
                        conn.execute("ALTER TABLE token_usage ADD COLUMN prompt_text TEXT DEFAULT '';")
                    if "response_text" not in col_names:
                        conn.execute("ALTER TABLE token_usage ADD COLUMN response_text TEXT DEFAULT '';")
                except Exception as e:
                    logger.warning(f"Could not check/migrate token_usage table columns: {e}")

                # 4. App metadata / lifetime counters table
                conn.execute("""
                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """)

                # 5. Normalize legacy statuses and clear stuck retries on already-removed items
                conn.execute("""
                    UPDATE processed_items
                    SET status = 'dismissed'
                    WHERE status = 'rejected' AND (details LIKE '%kept in client%' OR category = 'quality_downgrade');
                """)
                conn.execute("""
                    UPDATE processed_items
                    SET status = 'rejected'
                    WHERE status = 'blocked';
                """)
                conn.execute("""
                    UPDATE processed_items
                    SET retry_requested = 0
                    WHERE status IN ('imported', 'rejected', 'dismissed') AND retry_requested = 1;
                """)

            logger.info(f"SQLite database initialized at '{self.db_path}' (Retention: {self.retention_days} days)")
            self.purge_old_records()

    def purge_old_records(self, retention_days: Optional[int] = None) -> int:
        """
        Delete processed items and token usage records older than retention_days.
        Default: configured retention_days (30 days). If <= 0, purge is skipped.
        """
        days = self.retention_days if retention_days is None else retention_days
        if days <= 0:
            logger.debug("Database retention auto-clearing is disabled (retention_days <= 0).")
            return 0

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._lock:
            conn = self._get_connection()
            with conn:
                # Find IDs to delete so child keys are cleaned up
                c1 = conn.execute("DELETE FROM processed_items WHERE timestamp < ?", (cutoff,))
                purged_items = c1.rowcount
                c2 = conn.execute("DELETE FROM token_usage WHERE timestamp < ?", (cutoff,))
                purged_tokens = c2.rowcount
                # Clean orphaned keys if any
                conn.execute("DELETE FROM processed_item_keys WHERE processed_id NOT IN (SELECT id FROM processed_items);")

            total_purged = purged_items + purged_tokens
            if total_purged > 0:
                logger.info(
                    f"Purged {total_purged} database records older than {days} days ({purged_items} items, {purged_tokens} token calls)"
                )
            return total_purged

    def is_item_attempted(self, item_keys: List[str]) -> bool:
        """
        Check if any of the given item keys have been processed and NOT marked for retry.
        Returns True if item was already attempted and should be skipped.
        Returns False if item has never been attempted or user requested a retry.
        """
        if not item_keys:
            return False

        with self._lock:
            conn = self._get_connection()
            placeholders = ",".join("?" for _ in item_keys)
            query = f"""
            SELECT p.id, p.retry_requested, p.status
            FROM processed_items p
            JOIN processed_item_keys k ON p.id = k.processed_id
            WHERE k.item_key IN ({placeholders})
            ORDER BY p.id DESC
            LIMIT 1;
            """
            cursor = conn.execute(query, item_keys)
            row = cursor.fetchone()
            if not row:
                return False

            # If user explicitly requested retry, allow it!
            if bool(row["retry_requested"]):
                return False

            # Already attempted and retry not requested -> skip
            return True

    def record_processed_item(
        self,
        app: str,
        title: str,
        category: str,
        status: str,
        confidence: Optional[float] = None,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        details: Optional[str] = None,
        item_keys: Optional[List[str]] = None,
    ) -> int:
        """
        Record a processed queue item in SQLite and index its keys.
        """
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_connection()
            with conn:
                # Remove superseded records matching these keys so retried items don't leave stale rows behind
                if item_keys:
                    placeholders = ",".join("?" for _ in item_keys)
                    conn.execute(f"""
                        DELETE FROM processed_items
                        WHERE id IN (
                            SELECT processed_id FROM processed_item_keys WHERE item_key IN ({placeholders})
                        )
                    """, item_keys)

                cursor = conn.execute("""
                    INSERT INTO processed_items (
                        app, title, category, status, confidence, model, reasoning, details, retry_requested, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """, (
                    app,
                    title,
                    category,
                    status.lower(),
                    round(confidence, 3) if confidence is not None else None,
                    model,
                    reasoning or "",
                    details or "",
                    now_str,
                ))
                item_id = cursor.lastrowid

                # Index keys
                if item_keys:
                    key_tuples = [(k, item_id) for k in set(item_keys) if k]
                    conn.executemany("""
                        INSERT OR IGNORE INTO processed_item_keys (item_key, processed_id)
                        VALUES (?, ?)
                    """, key_tuples)

            return item_id

    def mark_item_for_retry(self, item_id: int) -> bool:
        """
        Flag a retryable processed item (skipped, failed, or dry_run) for retry.
        Items already imported, rejected (deleted), or dismissed from the queue cannot be retried.
        """
        with self._lock:
            conn = self._get_connection()
            with conn:
                cursor = conn.execute(
                    "UPDATE processed_items SET retry_requested = 1 WHERE id = ? AND status IN ('skipped', 'failed', 'dry_run')",
                    (item_id,),
                )
                success = cursor.rowcount > 0
                if success:
                    logger.info(f"Marked processed item #{item_id} for retry on next queue scan cycle.")
                return success

    def mark_all_rejected_for_retry(self) -> int:
        """
        Flag all retryable items (skipped, failed, or dry_run) still in the queue for retry.
        """
        with self._lock:
            conn = self._get_connection()
            with conn:
                cursor = conn.execute("""
                    UPDATE processed_items
                    SET retry_requested = 1
                    WHERE status IN ('skipped', 'failed', 'dry_run') AND retry_requested = 0
                """)
                count = cursor.rowcount
                if count > 0:
                    logger.info(f"Marked {count} retryable item(s) for retry on next queue scan cycle.")
                return count

    def record_token_usage(
        self,
        operation: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        duration_ms: int = 0,
        item_title: Optional[str] = None,
        success: bool = True,
        thinking_tokens: int = 0,
        prompt_text: Optional[str] = None,
        response_text: Optional[str] = None,
    ) -> int:
        """Record an AI token usage transaction."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_connection()
            with conn:
                cursor = conn.execute("""
                    INSERT INTO token_usage (
                        timestamp, operation, model, prompt_tokens, thinking_tokens, completion_tokens, total_tokens, duration_ms, item_title, success, prompt_text, response_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now_str,
                    operation,
                    model,
                    max(0, prompt_tokens),
                    max(0, thinking_tokens),
                    max(0, completion_tokens),
                    max(0, total_tokens),
                    duration_ms,
                    item_title or "",
                    1 if success else 0,
                    prompt_text or "",
                    response_text or "",
                ))
                return cursor.lastrowid

    def get_processed_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return the most recent processed items."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.execute("""
                SELECT id, app, title, category, status, confidence, model, reasoning, details, retry_requested, timestamp
                FROM processed_items
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_token_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return the most recent token transactions."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.execute("""
                SELECT id, timestamp, operation, model, prompt_tokens, thinking_tokens, completion_tokens, total_tokens, duration_ms, item_title, success, prompt_text, response_text
                FROM token_usage
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_summary_stats(self) -> Dict[str, Any]:
        """Compute aggregated statistics from SQLite across retained records."""
        with self._lock:
            conn = self._get_connection()

            # Status counts
            cursor = conn.execute("""
                SELECT status, COUNT(*) as cnt
                FROM processed_items
                GROUP BY status
            """)
            status_map = {row["status"]: row["cnt"] for row in cursor.fetchall()}

            # Category counts
            cursor = conn.execute("""
                SELECT category, COUNT(*) as cnt
                FROM processed_items
                GROUP BY category
            """)
            cat_map = {row["category"]: row["cnt"] for row in cursor.fetchall()}

            # Token totals
            cursor = conn.execute("""
                SELECT
                    COUNT(*) as total_calls,
                    COALESCE(SUM(prompt_tokens), 0) as sum_prompt,
                    COALESCE(SUM(thinking_tokens), 0) as sum_thinking,
                    COALESCE(SUM(completion_tokens), 0) as sum_comp,
                    COALESCE(SUM(total_tokens), 0) as sum_total
                FROM token_usage
            """)
            tok_row = cursor.fetchone()
            total_ai_calls = tok_row["total_calls"] if tok_row else 0
            prompt_tokens = tok_row["sum_prompt"] if tok_row else 0
            thinking_tokens = tok_row["sum_thinking"] if tok_row else 0
            comp_tokens = tok_row["sum_comp"] if tok_row else 0
            total_tokens = tok_row["sum_total"] if tok_row else 0

            # Tokens by model
            cursor = conn.execute("""
                SELECT
                    model,
                    COUNT(*) as calls,
                    COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                    COALESCE(SUM(thinking_tokens), 0) as thinking_tokens,
                    COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens
                FROM token_usage
                GROUP BY model
                ORDER BY total_tokens DESC, calls DESC
            """)
            tokens_by_model = {
                row["model"]: {
                    "calls": row["calls"],
                    "prompt_tokens": row["prompt_tokens"],
                    "thinking_tokens": row["thinking_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "total_tokens": row["total_tokens"],
                }
                for row in cursor.fetchall()
            }

            # Tokens by operation
            cursor = conn.execute("""
                SELECT
                    operation,
                    COUNT(*) as calls,
                    COALESCE(SUM(total_tokens), 0) as total_tokens
                FROM token_usage
                GROUP BY operation
                ORDER BY total_tokens DESC, calls DESC
            """)
            tokens_by_op = {
                row["operation"]: {
                    "calls": row["calls"],
                    "total_tokens": row["total_tokens"],
                }
                for row in cursor.fetchall()
            }

            total_items = sum(status_map.values())

            return {
                "total_items_processed": total_items,
                "total_imported": status_map.get("imported", 0),
                "total_dry_run": status_map.get("dry_run", 0),
                "total_rejected": status_map.get("rejected", 0),
                "total_dismissed": status_map.get("dismissed", 0),
                "total_ai_calls": total_ai_calls,
                "total_prompt_tokens": prompt_tokens,
                "total_thinking_tokens": thinking_tokens,
                "total_completion_tokens": comp_tokens,
                "total_tokens": total_tokens,
                "tokens_by_model": tokens_by_model,
                "tokens_by_operation": tokens_by_op,
                "items_by_category": cat_map,
                "items_by_status": status_map,
            }

    def close(self) -> None:
        """Close connection cleanly."""
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
