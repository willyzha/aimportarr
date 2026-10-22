import collections
from datetime import datetime, timezone
import logging
import threading
from typing import Any, Dict, List, Optional

from .database import DatabaseManager

logger = logging.getLogger("aimportarr.metrics")

class MetricsTracker:
    """
    Thread-safe metrics, retry tracking, and history manager for AImportarr.
    Backed by SQLite for persistence across restarts with automatic retention purging.
    """

    def __init__(self, db_manager: Optional[DatabaseManager] = None, max_history: int = 500):
        self._lock = threading.Lock()
        self.start_time = datetime.now(timezone.utc)
        self.last_scan_time: Optional[datetime] = None
        self.max_history = max_history
        self.db = db_manager

        # General activity counters
        self.total_scans: int = 0
        self.total_items_processed: int = 0
        self.total_imported: int = 0
        self.total_dry_run: int = 0
        self.total_rejected: int = 0
        self.total_blocked: int = 0
        self.total_dismissed: int = 0
        self.total_archives_extracted: int = 0

        # AI & Token counters
        self.total_ai_calls: int = 0
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_tokens: int = 0

        # Detailed breakdowns
        self.tokens_by_model: Dict[str, Dict[str, int]] = {}
        self.tokens_by_operation: Dict[str, Dict[str, int]] = {}
        self.items_by_category: Dict[str, int] = collections.defaultdict(int)
        self.items_by_status: Dict[str, int] = collections.defaultdict(int)

        # In-memory bounded histories (newest records first)
        self.processed_history: collections.deque = collections.deque(maxlen=max_history)
        self.token_history: collections.deque = collections.deque(maxlen=max_history)
        self._fallback_id_seq: int = 0

        if self.db:
            self._load_from_db()

    def init_database(self, db_path: str = "/config/aimportarr.db", retention_days: int = 30) -> None:
        """Initialize or rebind the SQLite database manager."""
        with self._lock:
            self.db = DatabaseManager(db_path=db_path, retention_days=retention_days)
            self._load_from_db()

    def _load_from_db(self) -> None:
        """Load retained summary aggregates and recent records from SQLite."""
        if not self.db:
            return

        try:
            summary = self.db.get_summary_stats()
            self.total_items_processed = summary["total_items_processed"]
            self.total_imported = summary["total_imported"]
            self.total_dry_run = summary["total_dry_run"]
            self.total_rejected = summary["total_rejected"]
            self.total_blocked = summary["total_blocked"]
            self.total_dismissed = summary["total_dismissed"]
            self.total_ai_calls = summary["total_ai_calls"]
            self.total_prompt_tokens = summary["total_prompt_tokens"]
            self.total_completion_tokens = summary["total_completion_tokens"]
            self.total_tokens = summary["total_tokens"]
            self.tokens_by_model = summary["tokens_by_model"]
            self.tokens_by_operation = summary["tokens_by_operation"]
            self.items_by_category = collections.defaultdict(int, summary["items_by_category"])
            self.items_by_status = collections.defaultdict(int, summary["items_by_status"])

            # Load recent history into memory
            self.processed_history.clear()
            items = self.db.get_processed_history(limit=self.max_history)
            for item in items:
                self.processed_history.append(item)

            self.token_history.clear()
            tokens = self.db.get_token_history(limit=self.max_history)
            for tok in tokens:
                self.token_history.append(tok)

            logger.info(
                f"Loaded persisted metrics from database: {self.total_tokens} tokens across {self.total_ai_calls} AI calls, {self.total_items_processed} processed items"
            )
        except Exception as e:
            logger.error(f"Failed to load metrics from SQLite: {e}")

    def purge_old_records(self) -> int:
        """Purge records older than retention period from database."""
        if self.db:
            purged = self.db.purge_old_records()
            if purged > 0:
                self._load_from_db()
            return purged
        return 0

    def is_item_attempted(self, item_keys: List[str]) -> bool:
        """
        Check if any key in item_keys was previously attempted and NOT marked for retry.
        Uses persistent SQLite records so rejection memory survives reboots.
        """
        if self.db:
            return self.db.is_item_attempted(item_keys)
        return False

    def mark_item_for_retry(self, item_id: int) -> bool:
        """Flag a specific rejected/failed item for retry on the next queue scan cycle."""
        with self._lock:
            # Update memory history item if present
            for item in self.processed_history:
                if item.get("id") == item_id:
                    item["retry_requested"] = 1
                    break

            if self.db:
                return self.db.mark_item_for_retry(item_id)
            return True

    def mark_all_rejected_for_retry(self) -> int:
        """Flag all previously rejected, failed, or blocked items for retry."""
        with self._lock:
            count = 0
            for item in self.processed_history:
                if item.get("status") in ("rejected", "failed", "blocked"):
                    item["retry_requested"] = 1
                    count += 1

            if self.db:
                return self.db.mark_all_rejected_for_retry()
            return count

    def record_scan(self) -> None:
        """Record an execution cycle of the queue scanner."""
        with self._lock:
            self.total_scans += 1
            self.last_scan_time = datetime.now(timezone.utc)

    def record_archive_extracted(self) -> None:
        """Record a successful extraction of a media archive."""
        with self._lock:
            self.total_archives_extracted += 1

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
    ) -> None:
        """
        Record a processed queue item in SQLite and in-memory summary.
        status: 'imported', 'dry_run', 'rejected', 'blocked', 'dismissed', or 'skipped'
        """
        with self._lock:
            st = status.lower()
            self.total_items_processed += 1
            if st == "imported":
                self.total_imported += 1
            elif st == "dry_run":
                self.total_dry_run += 1
            elif st == "rejected":
                self.total_rejected += 1
            elif st == "blocked":
                self.total_blocked += 1
            elif st == "dismissed":
                self.total_dismissed += 1

            self.items_by_category[category] += 1
            self.items_by_status[st] += 1

            item_id = None
            if self.db:
                try:
                    item_id = self.db.record_processed_item(
                        app=app,
                        title=title,
                        category=category,
                        status=st,
                        confidence=confidence,
                        model=model,
                        reasoning=reasoning,
                        details=details,
                        item_keys=item_keys,
                    )
                except Exception as e:
                    logger.error(f"Failed to record processed item to SQLite: {e}")

            if item_id is None:
                self._fallback_id_seq += 1
                item_id = self._fallback_id_seq

            record = {
                "id": item_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "app": app,
                "title": title,
                "category": category,
                "status": st,
                "confidence": round(confidence, 3) if confidence is not None else None,
                "model": model,
                "reasoning": reasoning or "",
                "details": details or "",
                "retry_requested": 0,
            }
            self.processed_history.appendleft(record)

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
    ) -> None:
        """Record an AI token usage transaction in SQLite and in-memory summary."""
        with self._lock:
            self.total_ai_calls += 1
            self.total_prompt_tokens += max(0, prompt_tokens)
            self.total_completion_tokens += max(0, completion_tokens)
            self.total_tokens += max(0, total_tokens)

            # Model breakdown
            if model not in self.tokens_by_model:
                self.tokens_by_model[model] = {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }
            m = self.tokens_by_model[model]
            m["calls"] += 1
            m["prompt_tokens"] += max(0, prompt_tokens)
            m["completion_tokens"] += max(0, completion_tokens)
            m["total_tokens"] += max(0, total_tokens)

            # Operation breakdown
            if operation not in self.tokens_by_operation:
                self.tokens_by_operation[operation] = {
                    "calls": 0,
                    "total_tokens": 0,
                }
            op = self.tokens_by_operation[operation]
            op["calls"] += 1
            op["total_tokens"] += max(0, total_tokens)

            tok_id = None
            if self.db:
                try:
                    tok_id = self.db.record_token_usage(
                        operation=operation,
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        duration_ms=duration_ms,
                        item_title=item_title,
                        success=success,
                    )
                except Exception as e:
                    logger.error(f"Failed to record token usage to SQLite: {e}")

            if tok_id is None:
                self._fallback_id_seq += 1
                tok_id = self._fallback_id_seq

            record = {
                "id": tok_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "operation": operation,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "duration_ms": duration_ms,
                "item_title": item_title or "",
                "success": success,
            }
            self.token_history.appendleft(record)

    def get_summary(self) -> Dict[str, Any]:
        """Return aggregated summary metrics."""
        with self._lock:
            uptime_sec = int((datetime.now(timezone.utc) - self.start_time).total_seconds())
            days, rem = divmod(uptime_sec, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)
            if days > 0:
                uptime_str = f"{days}d {hours}h {minutes}m"
            elif hours > 0:
                uptime_str = f"{hours}h {minutes}m {seconds}s"
            elif minutes > 0:
                uptime_str = f"{minutes}m {seconds}s"
            else:
                uptime_str = f"{seconds}s"

            return {
                "status": "online",
                "uptime_seconds": uptime_sec,
                "uptime_formatted": uptime_str,
                "started_at": self.start_time.isoformat(),
                "last_scan_at": self.last_scan_time.isoformat() if self.last_scan_time else None,
                "total_scans": self.total_scans,
                "total_items_processed": self.total_items_processed,
                "total_imported": self.total_imported,
                "total_dry_run": self.total_dry_run,
                "total_rejected": self.total_rejected,
                "total_blocked": self.total_blocked,
                "total_dismissed": self.total_dismissed,
                "total_archives_extracted": self.total_archives_extracted,
                "total_ai_calls": self.total_ai_calls,
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_tokens,
                "tokens_by_model": dict(self.tokens_by_model),
                "tokens_by_operation": dict(self.tokens_by_operation),
                "items_by_category": dict(self.items_by_category),
                "items_by_status": dict(self.items_by_status),
                "db_path": str(self.db.db_path) if self.db else None,
                "retention_days": self.db.retention_days if self.db else 0,
            }

    def get_processed_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return the most recent processed items."""
        with self._lock:
            if self.db:
                return self.db.get_processed_history(limit=limit)
            return list(self.processed_history)[:limit]

    def get_token_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return the most recent token transactions."""
        with self._lock:
            if self.db:
                return self.db.get_token_history(limit=limit)
            return list(self.token_history)[:limit]

    def reset_all(self) -> None:
        """Clear all metrics and history (e.g. for testing)."""
        with self._lock:
            self.total_scans = 0
            self.total_items_processed = 0
            self.total_imported = 0
            self.total_dry_run = 0
            self.total_rejected = 0
            self.total_blocked = 0
            self.total_dismissed = 0
            self.total_archives_extracted = 0
            self.total_ai_calls = 0
            self.total_prompt_tokens = 0
            self.total_completion_tokens = 0
            self.total_tokens = 0
            self.tokens_by_model.clear()
            self.tokens_by_operation.clear()
            self.items_by_category.clear()
            self.items_by_status.clear()
            self.processed_history.clear()
            self.token_history.clear()
            self._fallback_id_seq = 0

# Shared singleton instance
_tracker_instance: Optional[MetricsTracker] = None

def get_metrics() -> MetricsTracker:
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = MetricsTracker()
    return _tracker_instance

metrics = get_metrics()
