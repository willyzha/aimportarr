import collections
from datetime import datetime, timezone
import logging
import math
import re
import threading
from typing import Any, Dict, List, Optional, Union

from .database import DatabaseManager
from . import __version__

logger = logging.getLogger("aimportarr.metrics")


def estimate_tokens(text: Optional[str]) -> int:
    """Estimate token count from raw text (approx 3.8 characters per token)."""
    if not text:
        return 0
    return max(1, math.ceil(len(text.strip()) / 3.8))

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
        self.fifo_queue_size: int = 0
        self.total_items_processed: int = 0
        self.total_imported: int = 0
        self.total_dry_run: int = 0
        self.total_rejected: int = 0
        self.total_dismissed: int = 0
        self.total_archives_extracted: int = 0

        # AI & Token counters
        self.total_ai_calls: int = 0
        self.total_prompt_tokens: int = 0
        self.total_thinking_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_tokens: int = 0

        # Background scheduled tasks state
        self.tasks_status: Dict[str, Dict[str, Any]] = {
            "season_pack_backfill": {
                "state": "idle",
                "last_run": None,
                "last_duration_sec": None,
                "items_grabbed_last_run": 0,
                "current_progress": None,
                "next_run": None,
            },
            "missing_episode_retry": {
                "state": "idle",
                "last_run": None,
                "last_duration_sec": None,
                "items_grabbed_last_run": 0,
                "current_progress": None,
                "next_run": None,
            },
        }

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
            self.total_dismissed = summary["total_dismissed"]
            self.total_ai_calls = summary["total_ai_calls"]
            self.total_prompt_tokens = summary["total_prompt_tokens"]
            self.total_thinking_tokens = summary.get("total_thinking_tokens", 0)
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

    def clear_history(
        self,
        clear_items: bool = True,
        clear_tokens: bool = True,
        reset_counters: bool = False,
    ) -> Dict[str, int]:
        """Clear processed history and/or token usage in SQLite and in-memory caches."""
        with self._lock:
            result = {"deleted_items": 0, "deleted_keys": 0, "deleted_tokens": 0, "reset_metadata": 0}
            if self.db:
                result = self.db.clear_history(
                    clear_items=clear_items,
                    clear_tokens=clear_tokens,
                    reset_counters=reset_counters,
                )

            if clear_items:
                self.processed_history.clear()
                if reset_counters:
                    self.total_items_processed = 0
                    self.total_imported = 0
                    self.total_dry_run = 0
                    self.total_rejected = 0
                    self.total_dismissed = 0
                    self.total_archives_extracted = 0
                    self.items_by_category.clear()
                    self.items_by_status.clear()

            if clear_tokens:
                self.token_history.clear()
                if reset_counters:
                    self.total_ai_calls = 0
                    self.total_prompt_tokens = 0
                    self.total_thinking_tokens = 0
                    self.total_completion_tokens = 0
                    self.total_tokens = 0
                    self.tokens_by_model.clear()
                    self.tokens_by_operation.clear()

            if not reset_counters and self.db:
                self._load_from_db()

            return result

    def get_latest_item_record(self, item_keys: List[str]) -> Optional[Dict[str, Any]]:
        """Fetch the most recent processed item record for the given item keys."""
        with self._lock:
            if self.db:
                return self.db.get_latest_item_record(item_keys)
            for item in self.processed_history:
                if any(k in str(item.get("id")) or k in item.get("title", "") for k in item_keys):
                    return item
            return None

    def is_item_attempted(self, item_keys: List[str]) -> bool:
        """
        Check if any key in item_keys was previously attempted and NOT marked for retry.
        Uses persistent SQLite records so rejection memory survives reboots.
        """
        if self.db:
            return self.db.is_item_attempted(item_keys)
        return False

    def is_item_attempted_within_days(self, item_keys: List[str], days: int = 7) -> bool:
        """
        Check if any key in item_keys was attempted within the last `days` days and NOT marked for retry.
        """
        if self.db:
            return self.db.is_item_attempted_within_days(item_keys, days=days)
        return False

    def is_item_attempted_within_hours(self, item_keys: List[str], hours: int = 24) -> bool:
        """
        Check if any key in item_keys was attempted within the last `hours` hours and NOT marked for retry.
        """
        if self.db:
            return self.db.is_item_attempted_within_hours(item_keys, hours=hours)
        return False

    def mark_item_for_retry(self, item_id: int, allow_all: bool = False) -> bool:
        """Flag a specific retryable item (skipped, failed, or dry_run; plus imported/dismissed if allow_all) for retry."""
        with self._lock:
            retryable = ("skipped", "failed", "dry_run", "imported", "dismissed") if allow_all else ("skipped", "failed", "dry_run")
            matched_in_mem = False
            for item in self.processed_history:
                if item.get("id") == item_id:
                    if item.get("status") not in retryable:
                        return False
                    item["retry_requested"] = 1
                    matched_in_mem = True
                    break

            if self.db:
                return self.db.mark_item_for_retry(item_id, allow_all=allow_all)
            return matched_in_mem

    def mark_all_rejected_for_retry(self) -> int:
        """Flag all retryable items (skipped, failed, or dry_run) still in the queue for retry."""
        with self._lock:
            retryable = ("skipped", "failed", "dry_run")
            count = 0
            for item in self.processed_history:
                if item.get("status") in retryable and not item.get("retry_requested"):
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

    def set_fifo_queue_size(self, size: int) -> None:
        """Update the current number of items waiting/processing in the FIFO work queue."""
        with self._lock:
            self.fifo_queue_size = max(0, int(size))

    def record_archive_extracted(self) -> None:
        """Record a successful extraction of a media archive."""
        with self._lock:
            self.total_archives_extracted += 1

    def update_task_status(
        self,
        task_name: str,
        state: Optional[str] = None,
        progress: Optional[str] = None,
        items_grabbed: Optional[int] = None,
        duration_sec: Optional[float] = None,
        last_run: Optional[str] = None,
        next_run: Optional[str] = None,
    ) -> None:
        """Update live status, progress, and scheduling info for a background task."""
        with self._lock:
            if task_name not in self.tasks_status:
                self.tasks_status[task_name] = {
                    "state": "idle",
                    "last_run": None,
                    "last_duration_sec": None,
                    "items_grabbed_last_run": 0,
                    "current_progress": None,
                    "next_run": None,
                }
            task = self.tasks_status[task_name]
            if state is not None:
                task["state"] = state
            if progress is not None:
                task["current_progress"] = progress
            elif state in ("idle", "disabled"):
                task["current_progress"] = None
            if items_grabbed is not None:
                task["items_grabbed_last_run"] = items_grabbed
            if duration_sec is not None:
                task["last_duration_sec"] = round(duration_sec, 2)
            if last_run is not None:
                task["last_run"] = last_run
            if next_run is not None:
                task["next_run"] = next_run

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
        status: 'imported', 'dry_run', 'rejected', 'dismissed', 'failed', or 'skipped'
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
            # Remove any superseded in-memory entry for the same app+title
            self.processed_history = collections.deque(
                (r for r in self.processed_history if not (r.get("app") == app and r.get("title") == title)),
                maxlen=self.max_history,
            )
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
        thinking_tokens: int = 0,
        prompt_text: Optional[str] = None,
        response_text: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        """Record an AI token usage transaction in SQLite and in-memory summary."""
        with self._lock:
            # Bug 2 fix: Even if there's an error, as long as something was sent, prompt_tokens must not be 0
            p_tok = max(0, prompt_tokens)
            if p_tok == 0 and prompt_text:
                p_tok = estimate_tokens(prompt_text)

            c_tok = max(0, completion_tokens)
            th_tok = max(0, thinking_tokens)
            t_tok = max(total_tokens, p_tok + c_tok + th_tok)

            # Auto-derive error_code if not provided and call failed
            if not success and not error_code:
                if response_text:
                    m = re.search(r"\b(HTTP\s+\d+|TIMEOUT|CONNECT_ERROR|NETWORK_ERROR|JSON_DECODE_ERROR|SCHEMA_VALIDATION_ERROR)\b", response_text, re.IGNORECASE)
                    if m:
                        error_code = m.group(1).upper()
                    else:
                        error_code = "FAILED"
                else:
                    error_code = "FAILED"

            self.total_ai_calls += 1
            self.total_prompt_tokens += p_tok
            self.total_thinking_tokens += th_tok
            self.total_completion_tokens += c_tok
            self.total_tokens += t_tok

            # Model breakdown
            if model not in self.tokens_by_model:
                self.tokens_by_model[model] = {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "thinking_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }
            m = self.tokens_by_model[model]
            m["calls"] += 1
            m["prompt_tokens"] += p_tok
            m["thinking_tokens"] = m.get("thinking_tokens", 0) + th_tok
            m["completion_tokens"] += c_tok
            m["total_tokens"] += t_tok

            # Operation breakdown
            if operation not in self.tokens_by_operation:
                self.tokens_by_operation[operation] = {
                    "calls": 0,
                    "total_tokens": 0,
                }
            op = self.tokens_by_operation[operation]
            op["calls"] += 1
            op["total_tokens"] += t_tok

            tok_id = None
            if self.db:
                try:
                    tok_id = self.db.record_token_usage(
                        operation=operation,
                        model=model,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        total_tokens=t_tok,
                        duration_ms=duration_ms,
                        item_title=item_title,
                        success=success,
                        thinking_tokens=th_tok,
                        prompt_text=prompt_text,
                        response_text=response_text,
                        error_code=error_code,
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
                "prompt_tokens": p_tok,
                "thinking_tokens": th_tok,
                "completion_tokens": c_tok,
                "total_tokens": t_tok,
                "duration_ms": duration_ms,
                "item_title": item_title or "",
                "success": success,
                "error_code": error_code or "",
                "prompt_text": prompt_text or "",
                "response_text": response_text or "",
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
                "version": __version__,
                "status": "online",
                "uptime_seconds": uptime_sec,
                "uptime_formatted": uptime_str,
                "started_at": self.start_time.isoformat(),
                "last_scan_at": self.last_scan_time.isoformat() if self.last_scan_time else None,
                "total_scans": self.total_scans,
                "fifo_queue_size": self.fifo_queue_size,
                "total_items_processed": self.total_items_processed,
                "total_imported": self.total_imported,
                "total_dry_run": self.total_dry_run,
                "total_rejected": self.total_rejected,
                "total_dismissed": self.total_dismissed,
                "total_archives_extracted": self.total_archives_extracted,
                "total_ai_calls": self.total_ai_calls,
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_thinking_tokens": self.total_thinking_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_tokens,
                "tokens_by_model": dict(self.tokens_by_model),
                "tokens_by_operation": dict(self.tokens_by_operation),
                "items_by_category": dict(self.items_by_category),
                "items_by_status": dict(self.items_by_status),
                "tasks": {k: dict(v) for k, v in self.tasks_status.items()},
                "db_path": str(self.db.db_path) if self.db else None,
                "retention_days": self.db.retention_days if self.db else 0,
            }

    def get_processed_history(
        self,
        limit: int = 100,
        page: Optional[int] = None,
        search: Optional[str] = None,
        filter_status: Optional[str] = None,
    ) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
        """Return the most recent processed items with optional pagination, filter, and search."""
        with self._lock:
            if self.db:
                result = self.db.get_processed_history(
                    limit=limit,
                    page=page,
                    search=search,
                    filter_status=filter_status,
                )
                if isinstance(result, dict):
                    result["global_total"] = self.total_items_processed
                return result

            # In-memory fallback
            items = list(self.processed_history)
            if filter_status and filter_status != "all":
                if filter_status == "in_queue":
                    items = [it for it in items if it.get("status") in ("skipped", "failed", "dry_run")]
                elif filter_status == "rejected":
                    items = [it for it in items if it.get("status") in ("rejected", "blocked")]
                elif filter_status in ("season_backfill", "episode_retry", "missing_movie"):
                    items = [it for it in items if it.get("category") == filter_status]
                else:
                    items = [it for it in items if it.get("status") == filter_status]

            if search:
                q = search.lower()
                items = [
                    it for it in items
                    if q in (it.get("title") or "").lower()
                    or q in (it.get("category") or "").lower()
                    or q in (it.get("reasoning") or "").lower()
                    or q in (it.get("model") or "").lower()
                    or q in (it.get("details") or "").lower()
                ]

            if page is None:
                return items[:limit]

            total = len(items)
            page = max(1, page)
            limit = max(1, limit)
            offset = (page - 1) * limit
            total_pages = math.ceil(total / limit) if total > 0 else 1
            return {
                "items": items[offset : offset + limit],
                "total": total,
                "global_total": self.total_items_processed,
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
        """Return the most recent token transactions with optional pagination and search."""
        with self._lock:
            if self.db:
                result = self.db.get_token_history(
                    limit=limit,
                    page=page,
                    search=search,
                )
                if isinstance(result, dict):
                    result["global_total"] = self.total_ai_calls
                return result

            items = list(self.token_history)
            if search:
                q = search.lower()
                items = [
                    it for it in items
                    if q in (it.get("operation") or "").lower()
                    or q in (it.get("model") or "").lower()
                    or q in (it.get("item_title") or "").lower()
                    or q in (it.get("error_code") or "").lower()
                ]

            if page is None:
                return items[:limit]

            total = len(items)
            page = max(1, page)
            limit = max(1, limit)
            offset = (page - 1) * limit
            total_pages = math.ceil(total / limit) if total > 0 else 1
            return {
                "items": items[offset : offset + limit],
                "total": total,
                "global_total": self.total_ai_calls,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
            }

    def reset_all(self) -> None:
        """Clear all metrics and history (e.g. for testing)."""
        with self._lock:
            self.total_scans = 0
            self.total_items_processed = 0
            self.total_imported = 0
            self.total_dry_run = 0
            self.total_rejected = 0
            self.total_dismissed = 0
            self.total_archives_extracted = 0
            self.total_ai_calls = 0
            self.total_prompt_tokens = 0
            self.total_thinking_tokens = 0
            self.total_completion_tokens = 0
            self.total_tokens = 0
            self.tokens_by_model.clear()
            self.tokens_by_operation.clear()
            self.items_by_category.clear()
            self.items_by_status.clear()
            self.processed_history.clear()
            self.token_history.clear()
            self._fallback_id_seq = 0
            for t in self.tasks_status.values():
                t["state"] = "idle"
                t["last_run"] = None
                t["last_duration_sec"] = None
                t["items_grabbed_last_run"] = 0
                t["current_progress"] = None
                t["next_run"] = None

# Shared singleton instance
_tracker_instance: Optional[MetricsTracker] = None

def get_metrics() -> MetricsTracker:
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = MetricsTracker()
    return _tracker_instance

metrics = get_metrics()
