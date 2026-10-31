import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple, Union

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
                    timestamp TEXT NOT NULL,
                    trigger_source TEXT DEFAULT 'queue',
                    queue_messages TEXT DEFAULT '',
                    release_source TEXT DEFAULT '',
                    indexer TEXT DEFAULT '',
                    extra_data TEXT DEFAULT ''
                );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_status ON processed_items(status);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_timestamp ON processed_items(timestamp);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_retry ON processed_items(retry_requested);")

                # Migration: Ensure trigger_source, queue_messages, release_source, indexer, and extra_data exist in existing databases
                try:
                    cursor = conn.execute("PRAGMA table_info(processed_items);")
                    col_names = [row["name"] for row in cursor.fetchall()]
                    if "trigger_source" not in col_names:
                        conn.execute("ALTER TABLE processed_items ADD COLUMN trigger_source TEXT DEFAULT 'queue';")
                    if "queue_messages" not in col_names:
                        conn.execute("ALTER TABLE processed_items ADD COLUMN queue_messages TEXT DEFAULT '';")
                    if "release_source" not in col_names:
                        conn.execute("ALTER TABLE processed_items ADD COLUMN release_source TEXT DEFAULT '';")
                    if "indexer" not in col_names:
                        conn.execute("ALTER TABLE processed_items ADD COLUMN indexer TEXT DEFAULT '';")
                    if "extra_data" not in col_names:
                        conn.execute("ALTER TABLE processed_items ADD COLUMN extra_data TEXT DEFAULT '';")
                except Exception as e:
                    logger.warning(f"Could not check/migrate processed_items table columns: {e}")

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
                    error_code TEXT DEFAULT '',
                    prompt_text TEXT DEFAULT '',
                    response_text TEXT DEFAULT ''
                );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_token_timestamp ON token_usage(timestamp);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_token_model ON token_usage(model);")

                # Migration: Ensure thinking_tokens, prompt_text, response_text, and error_code exist in existing databases
                try:
                    cursor = conn.execute("PRAGMA table_info(token_usage);")
                    col_names = [row["name"] for row in cursor.fetchall()]
                    if "thinking_tokens" not in col_names:
                        conn.execute("ALTER TABLE token_usage ADD COLUMN thinking_tokens INTEGER DEFAULT 0;")
                    if "error_code" not in col_names:
                        conn.execute("ALTER TABLE token_usage ADD COLUMN error_code TEXT DEFAULT '';")
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

                # 6. Normalize legacy redundant already_imported records that were mistakenly logged as imported
                conn.execute("""
                    UPDATE processed_items
                    SET status = 'dismissed',
                        details = 'All file(s) already in library (0 imported, queue cleared)'
                    WHERE category = 'already_imported'
                      AND status = 'imported'
                      AND (details LIKE '%0 existing file(s) kept on disk%' OR details LIKE '%movie file already imported%');
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

    def clear_history(
        self,
        clear_items: bool = True,
        clear_tokens: bool = True,
        reset_counters: bool = False,
    ) -> Dict[str, int]:
        """
        Clear processed item history and/or token usage history.
        Optionally resets lifetime counters in app_metadata.
        Runs VACUUM to reclaim disk space.
        Returns counts of cleared records.
        """
        deleted_items = 0
        deleted_keys = 0
        deleted_tokens = 0
        reset_metadata = 0

        with self._lock:
            conn = self._get_connection()
            with conn:
                if clear_items:
                    c_keys = conn.execute("DELETE FROM processed_item_keys;")
                    deleted_keys = c_keys.rowcount
                    c_items = conn.execute("DELETE FROM processed_items;")
                    deleted_items = c_items.rowcount
                if clear_tokens:
                    c_tokens = conn.execute("DELETE FROM token_usage;")
                    deleted_tokens = c_tokens.rowcount
                if reset_counters:
                    c_meta = conn.execute("DELETE FROM app_metadata WHERE key NOT LIKE 'gemini_%';")
                    reset_metadata = c_meta.rowcount

            try:
                conn.execute("VACUUM;")
            except Exception as e:
                logger.warning(f"Could not VACUUM database after clear_history: {e}")

        logger.info(
            f"Cleared database history: {deleted_items} items, {deleted_keys} keys, "
            f"{deleted_tokens} tokens, reset {reset_metadata} counters"
        )
        return {
            "deleted_items": deleted_items,
            "deleted_keys": deleted_keys,
            "deleted_tokens": deleted_tokens,
            "reset_metadata": reset_metadata,
        }

    def get_metadata(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieve a string value from the app_metadata key-value table."""
        try:
            with self._lock:
                conn = self._get_connection()
                cur = conn.execute("SELECT value FROM app_metadata WHERE key = ?;", (key,))
                row = cur.fetchone()
                if row and row["value"] is not None:
                    return str(row["value"])
        except Exception as e:
            logger.warning("Failed to get metadata key '%s': %s", key, e)
        return default

    def set_metadata(self, key: str, value: str) -> None:
        """Store or update a string value in the app_metadata key-value table."""
        try:
            with self._lock:
                conn = self._get_connection()
                with conn:
                    conn.execute("INSERT OR REPLACE INTO app_metadata (key, value) VALUES (?, ?);", (key, value))
        except Exception as e:
            logger.warning("Failed to set metadata key '%s': %s", key, e)

    def get_latest_item_record(self, item_keys: List[str]) -> Optional[Dict[str, Any]]:
        """
        Fetch the most recent processed_items record for the given item keys.
        Returns a dict of record columns or None if never recorded.
        """
        if not item_keys:
            return None

        with self._lock:
            conn = self._get_connection()
            placeholders = ",".join("?" for _ in item_keys)
            query = f"""
            SELECT p.id, p.app, p.title, p.category, p.status, p.confidence, p.model,
                   p.reasoning, p.details, p.retry_requested, p.timestamp,
                   p.trigger_source, p.queue_messages, p.release_source, p.indexer, p.extra_data
            FROM processed_items p
            JOIN processed_item_keys k ON p.id = k.processed_id
            WHERE k.item_key IN ({placeholders})
            ORDER BY p.id DESC
            LIMIT 1;
            """
            cursor = conn.execute(query, item_keys)
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)

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

    def is_item_attempted_within_days(self, item_keys: List[str], days: int = 7) -> bool:
        """
        Check if any of the given item keys have been processed within the last `days` days
        and NOT marked for retry.
        """
        if not item_keys:
            return False

        with self._lock:
            conn = self._get_connection()
            placeholders = ",".join("?" for _ in item_keys)
            query = f"""
            SELECT p.id, p.retry_requested, p.status, p.timestamp
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

            if bool(row["retry_requested"]):
                return False

            if days > 0 and row["timestamp"]:
                cutoff_str = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                if row["timestamp"] < cutoff_str:
                    return False

            return True

    def is_item_attempted_within_hours(self, item_keys: List[str], hours: int = 24) -> bool:
        """
        Check if any of the given item keys have been processed within the last `hours` hours
        and NOT marked for retry.
        """
        if not item_keys:
            return False

        with self._lock:
            conn = self._get_connection()
            placeholders = ",".join("?" for _ in item_keys)
            query = f"""
            SELECT p.id, p.retry_requested, p.status, p.timestamp
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

            if bool(row["retry_requested"]):
                return False

            if hours > 0 and row["timestamp"]:
                cutoff_str = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
                if row["timestamp"] < cutoff_str:
                    return False

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
        trigger_source: str = "queue",
        queue_messages: Optional[str] = None,
        release_source: Optional[str] = None,
        indexer: Optional[str] = None,
        extra_data: Optional[str] = None,
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
                        app, title, category, status, confidence, model, reasoning, details, retry_requested, timestamp,
                        trigger_source, queue_messages, release_source, indexer, extra_data
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
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
                    trigger_source or "queue",
                    queue_messages or "",
                    release_source or "",
                    indexer or "",
                    extra_data or "",
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

    def mark_item_for_retry(self, item_id: int, allow_all: bool = False) -> bool:
        """
        Flag a retryable processed item for retry.
        By default, only 'skipped', 'failed', and 'dry_run' can be retried.
        If allow_all=True (e.g. from UI retry action), allows retrying 'imported' or 'dismissed' items.
        """
        with self._lock:
            conn = self._get_connection()
            allowed = ("skipped", "failed", "dry_run", "imported", "dismissed") if allow_all else ("skipped", "failed", "dry_run")
            placeholders = ",".join("?" for _ in allowed)
            with conn:
                cursor = conn.execute(
                    f"UPDATE processed_items SET retry_requested = 1 WHERE id = ? AND status IN ({placeholders})",
                    (item_id, *allowed),
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

    def cancel_item_retry(self, item_id: int) -> bool:
        """
        Unmark retry_requested for a specific processed item.
        """
        with self._lock:
            conn = self._get_connection()
            with conn:
                cursor = conn.execute(
                    "UPDATE processed_items SET retry_requested = 0 WHERE id = ?",
                    (item_id,)
                )
                success = cursor.rowcount > 0
                if success:
                    logger.info(f"Cancelled retry for processed item #{item_id}.")
                return success

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
        error_code: Optional[str] = None,
    ) -> int:
        """Record an AI token usage transaction."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_connection()
            with conn:
                cursor = conn.execute("""
                    INSERT INTO token_usage (
                        timestamp, operation, model, prompt_tokens, thinking_tokens, completion_tokens, total_tokens, duration_ms, item_title, success, error_code, prompt_text, response_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    error_code or "",
                    prompt_text or "",
                    response_text or "",
                ))
                return cursor.lastrowid

    def get_processed_history(
        self,
        limit: int = 100,
        page: Optional[int] = None,
        search: Optional[str] = None,
        filter_status: Optional[str] = None,
        filter_trigger: Optional[str] = None,
    ) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
        """Return processed items with optional pagination, filter, and search."""
        with self._lock:
            conn = self._get_connection()
            where_clauses = []
            params = []

            # Filter clause (Status)
            if filter_status and filter_status != "all":
                if filter_status == "in_queue":
                    where_clauses.append("status IN ('skipped', 'failed', 'dry_run')")
                elif filter_status == "rejected":
                    where_clauses.append("status IN ('rejected', 'blocked')")
                elif filter_status in ("season_backfill", "episode_retry", "missing_movie"):
                    where_clauses.append("category = ?")
                    params.append(filter_status)
                else:
                    where_clauses.append("status = ?")
                    params.append(filter_status)

            # Filter clause (Trigger Origin)
            if filter_trigger and filter_trigger != "all":
                if filter_trigger == "queue":
                    where_clauses.append("trigger_source = 'queue'")
                elif filter_trigger == "automated":
                    where_clauses.append("trigger_source != 'queue'")
                else:
                    where_clauses.append("trigger_source = ?")
                    params.append(filter_trigger)

            # Search clause
            if search:
                pattern = f"%{search.strip().lower()}%"
                where_clauses.append("""(
                    LOWER(title) LIKE ?
                    OR LOWER(category) LIKE ?
                    OR LOWER(reasoning) LIKE ?
                    OR LOWER(model) LIKE ?
                    OR LOWER(details) LIKE ?
                    OR LOWER(indexer) LIKE ?
                    OR LOWER(release_source) LIKE ?
                    OR LOWER(queue_messages) LIKE ?
                )""")
                params.extend([pattern] * 8)

            where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            select_cols = """
                id, app, title, category, status, confidence, model, reasoning, details,
                retry_requested, timestamp, trigger_source, queue_messages, release_source,
                indexer, extra_data
            """

            if page is None:
                query = f"""
                    SELECT {select_cols}
                    FROM processed_items
                    {where_sql}
                    ORDER BY id DESC
                    LIMIT ?
                """
                cursor = conn.execute(query, params + [limit])
                rows = cursor.fetchall()
                return [dict(r) for r in rows]

            # Paginated query
            count_cursor = conn.execute(f"SELECT COUNT(*) FROM processed_items {where_sql}", params)
            total = count_cursor.fetchone()[0]

            page = max(1, page)
            limit = max(1, limit)
            offset = (page - 1) * limit
            total_pages = math.ceil(total / limit) if total > 0 else 1

            query = f"""
                SELECT {select_cols}
                FROM processed_items
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            """
            cursor = conn.execute(query, params + [limit, offset])
            rows = cursor.fetchall()
            return {
                "items": [dict(r) for r in rows],
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
            }

    def get_token_history(
        self,
        limit: int = 100,
        page: Optional[int] = None,
        search: Optional[str] = None,
    ) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
        """Return token transactions with optional pagination and search."""
        with self._lock:
            conn = self._get_connection()
            where_clauses = []
            params = []

            if search:
                pattern = f"%{search.strip().lower()}%"
                where_clauses.append("(LOWER(operation) LIKE ? OR LOWER(model) LIKE ? OR LOWER(item_title) LIKE ? OR LOWER(error_code) LIKE ?)")
                params.extend([pattern, pattern, pattern, pattern])

            where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            if page is None:
                query = f"""
                    SELECT id, timestamp, operation, model, prompt_tokens, thinking_tokens, completion_tokens, total_tokens, duration_ms, item_title, success, error_code, prompt_text, response_text
                    FROM token_usage
                    {where_sql}
                    ORDER BY id DESC
                    LIMIT ?
                """
                cursor = conn.execute(query, params + [limit])
                rows = cursor.fetchall()
                return [dict(r) for r in rows]

            count_cursor = conn.execute(f"SELECT COUNT(*) FROM token_usage {where_sql}", params)
            total = count_cursor.fetchone()[0]

            page = max(1, page)
            limit = max(1, limit)
            offset = (page - 1) * limit
            total_pages = math.ceil(total / limit) if total > 0 else 1

            query = f"""
                SELECT id, timestamp, operation, model, prompt_tokens, thinking_tokens, completion_tokens, total_tokens, duration_ms, item_title, success, error_code, prompt_text, response_text
                FROM token_usage
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            """
            cursor = conn.execute(query, params + [limit, offset])
            rows = cursor.fetchall()
            return {
                "items": [dict(r) for r in rows],
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
            }

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
