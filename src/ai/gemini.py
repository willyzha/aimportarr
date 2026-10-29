import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple, Union
import httpx
from ..metrics import metrics
from .base import (
    BaseAIProvider,
    Stage1TriageResult,
    ApprovedFileItem,
    Stage2DecisionResult,
    BackfillPackMatchResult,
    EpisodeMatchItem,
    BackfillBulkEpisodesMatchResult,
    AliasVerificationResult,
    AIProviderError,
    DailyQuotaExhaustedError,
    compact_stage2_context,
    validate_stage1_payload,
    validate_stage2_payload,
    validate_backfill_payload,
    validate_bulk_episodes_payload,
    validate_alias_payload,
    build_stage3_repair_prompt,
)

logger = logging.getLogger("aimportarr.ai.gemini")


DEFAULT_FALLBACK_CANDIDATES: Tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-2.5-flash-lite",
)


def is_daily_quota_message(err_text: str) -> bool:
    """Check if error string specifically indicates daily token/request exhaustion."""
    low = str(err_text).lower()
    return any(
        k in low
        for k in (
            "per day",
            "per_day",
            "per-day",
            "perday",
            "requests per day",
            "tokens per day",
            "requestsperday",
            "tokensperday",
            "daily quota",
            "daily limit",
            "free tier limit reached for the day",
        )
    )


def get_daily_reset_epoch() -> float:
    """Calculate the epoch timestamp for the next Gemini daily quota reset (08:00 UTC / Midnight Pacific)."""
    now_utc = datetime.now(timezone.utc)
    target = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
    if now_utc >= target:
        target += timedelta(days=1)
    return target.timestamp()


def extract_gemini_error_details(e: Exception) -> Tuple[str, str]:
    """Extract structured (error_code, detailed_reason) from a Gemini exception."""
    if hasattr(e, "response") and getattr(e.response, "status_code", None) is not None:
        status = e.response.status_code
        err_body = getattr(e.response, "text", "")
        try:
            err_json = e.response.json()
            if isinstance(err_json, dict) and "error" in err_json:
                err_obj = err_json["error"]
                if isinstance(err_obj, dict):
                    g_status = err_obj.get("status")  # e.g. "RESOURCE_EXHAUSTED", "INVALID_ARGUMENT"
                    g_msg = err_obj.get("message")
                    code_label = f"HTTP {status} ({g_status})" if g_status else f"HTTP {status}"
                    return code_label, f"Gemini API returned {code_label}: {g_msg or err_body}"
        except Exception:
            pass
        return f"HTTP {status}", f"Gemini API returned HTTP {status}: {err_body or str(e)}"
    err_name = type(e).__name__.lower()
    if "timeout" in err_name:
        return "TIMEOUT", f"Gemini API request timed out: {e}"
    elif "connect" in err_name:
        return "CONNECT_ERROR", f"Failed to connect to Google Gemini API: {e}"
    elif "request" in err_name:
        return "NETWORK_ERROR", f"Network request error contacting Gemini: {e}"
    else:
        err_type = type(e).__name__
        return err_type, f"{err_type}: {e}"


class GeminiProvider(BaseAIProvider):
    def __init__(self, api_key: str, model: str = "auto"):
        self.api_key = api_key
        self.model = model.strip() if model else "auto"
        self._resolved_model: Optional[str] = None
        self._failed_models: set = set()
        self._rate_limited_models: Dict[str, float] = {}
        self._daily_exhausted_models: Dict[str, float] = {}
        self._project_daily_quota_exhausted: bool = False
        self._project_daily_quota_reset_time: float = 0.0
        self._discovered_candidates: List[str] = []
        self._candidates_last_fetched: float = 0.0
        self._last_call_time: float = 0.0
        self._min_call_interval: float = 1.0  # Pacing delay between calls to respect rate limits
        self._load_cached_catalog_from_db()

    def _load_cached_catalog_from_db(self) -> None:
        """Load previously discovered model catalog from persistent database if available."""
        try:
            if hasattr(metrics, "db") and metrics.db is not None:
                cached_json = metrics.db.get_metadata("gemini_model_catalog")
                if cached_json:
                    models = json.loads(cached_json)
                    if isinstance(models, list) and models:
                        self._discovered_candidates = [str(m) for m in models]
                        last_ts_str = metrics.db.get_metadata("gemini_model_catalog_ts")
                        self._candidates_last_fetched = float(last_ts_str) if last_ts_str else 0.0
                        logger.info(
                            "Loaded %d cached Gemini models from persistent storage: %s",
                            len(self._discovered_candidates),
                            self._discovered_candidates,
                        )
        except Exception as e:
            logger.debug("Could not load cached Gemini catalog from DB: %s", e)

    def _save_cached_catalog_to_db(self, candidates: List[str]) -> None:
        """Persist discovered model catalog to database so it survives restarts."""
        try:
            if hasattr(metrics, "db") and metrics.db is not None:
                metrics.db.set_metadata("gemini_model_catalog", json.dumps(candidates))
                metrics.db.set_metadata("gemini_model_catalog_ts", str(time.time()))
        except Exception as e:
            logger.debug("Could not save cached Gemini catalog to DB: %s", e)

    @property
    def name(self) -> str:
        active = self._resolved_model or self.model
        return f"Gemini ({active})"

    def is_daily_quota_exhausted(self) -> bool:
        """
        Return True if we are certain that all Gemini tokens/requests for the day are used up.
        True if project-level daily quota was hit or all candidate Gemini models
        have exhausted their daily quota for today.
        """
        now = time.time()
        if self._project_daily_quota_exhausted:
            if not self._project_daily_quota_reset_time or now < self._project_daily_quota_reset_time:
                return True
            self._project_daily_quota_exhausted = False
            self._project_daily_quota_reset_time = 0.0

        candidates = self._discovered_candidates
        if not candidates:
            return False

        active_exhausted = [
            m for m in candidates
            if m in self._daily_exhausted_models and self._daily_exhausted_models[m] > now
        ]
        return len(active_exhausted) >= len(candidates)

    @staticmethod
    def _is_daily_quota_error(resp: httpx.Response) -> Tuple[bool, str]:
        """
        Determine whether an HTTP 429 response indicates daily quota exhaustion
        vs a temporary per-minute rate limit (RPM/TPM).
        """
        try:
            err_json = resp.json()
            error_obj = err_json.get("error", {})
            msg = str(error_obj.get("message", ""))
            details = error_obj.get("details", [])

            for d in details:
                if isinstance(d, dict):
                    meta = d.get("metadata", {})
                    quota_metric = str(meta.get("quota_metric", "")).lower()
                    quota_limit = str(meta.get("quota_limit", "")).lower()
                    if any(k in quota_metric or k in quota_limit for k in ("day", "daily")):
                        return True, f"Daily quota metric reached: {meta.get('quota_metric') or meta.get('quota_limit')}"
                    for viol in d.get("violations", []):
                        desc = str(viol.get("description", "")).lower()
                        metric = str(viol.get("quotaMetric", "") or viol.get("quota_metric", "")).lower()
                        if any(k in desc or k in metric for k in ("per day", "per_day", "perday", "daily")):
                            return True, f"Daily quota violation: {viol.get('quotaMetric') or viol.get('description')}"

            if is_daily_quota_message(msg):
                return True, f"Daily quota message: {msg}"
        except Exception:
            pass

        if is_daily_quota_message(resp.text):
            return True, "Daily quota exceeded"

        return False, "Temporary per-minute rate limit (RPM/TPM)"

    @staticmethod
    def _parse_retry_after(resp: httpx.Response, default: int = 60) -> int:
        retry_hdr = resp.headers.get("retry-after")
        if retry_hdr:
            try:
                return max(5, min(300, int(retry_hdr)))
            except ValueError:
                pass
        return default

    async def get_candidate_models(
        self,
        raw_models: Optional[List[Union[str, dict]]] = None,
        force_refresh: bool = False
    ) -> List[str]:
        """
        Dynamically fetch and score candidate models from Google API catalog.
        Contains zero hardcoded model names; adapts automatically to newly released models.
        """
        if raw_models is not None:
            models_data = []
            for item in raw_models:
                if isinstance(item, str):
                    models_data.append({"name": item, "supportedGenerationMethods": ["generateContent"]})
                elif isinstance(item, dict):
                    models_data.append(item)
            data = {"models": models_data}
        else:
            now = time.time()
            if not self._discovered_candidates:
                self._load_cached_catalog_from_db()

            if self._discovered_candidates and not force_refresh and (now - self._candidates_last_fetched) < 86400.0:
                return self._discovered_candidates

            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}"
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as e:
                logger.warning("Failed to auto-discover Gemini models catalog: %s", e)
                if not self._discovered_candidates:
                    self._load_cached_catalog_from_db()

                if self._discovered_candidates:
                    logger.info(
                        "Using previously discovered Gemini models catalog (%d models): %s",
                        len(self._discovered_candidates),
                        self._discovered_candidates,
                    )
                    # Back off retry for 1 hour to prevent hammering Google's failing endpoint on every request
                    self._candidates_last_fetched = now - 82800.0
                    return self._discovered_candidates

                pref = self.model.strip() if self.model and self.model not in ("auto", "smallest", "") else None
                fallback_list = [pref] if pref else []
                for def_m in DEFAULT_FALLBACK_CANDIDATES:
                    if def_m not in fallback_list:
                        fallback_list.append(def_m)
                return fallback_list

        candidates = []
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            name = m.get("name", "").replace("models/", "")
            if not name.startswith("gemini-"):
                continue
            if "generateContent" not in methods:
                continue
            if any(x in name for x in ["image", "tts", "robotics", "clip", "banana", "transcribe", "argon", "computer-use", "customtools"]):
                continue
            candidates.append(name)

        def model_score(name: str):
            tier = 4
            if "flash" in name and "flash-lite" not in name:
                tier = 1
            elif "flash-lite" in name:
                tier = 2
            elif "pro" in name:
                tier = 3

            v_match = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
            version = float(v_match.group(1)) if v_match else 0.0
            is_preview = 1 if "preview" in name else 0
            is_generic_latest = 1 if "latest" in name else 0
            return (tier, is_generic_latest, -version, is_preview, name)

        candidates.sort(key=model_score)
        self._discovered_candidates = candidates
        self._candidates_last_fetched = time.time()
        if raw_models is None:
            self._save_cached_catalog_to_db(candidates)
        return candidates

    async def get_ordered_models_to_try(self, preferred: Optional[str] = None) -> List[str]:
        """
        Return list of candidate Gemini models ordered by preference and availability.
        The user's preferred model (if specified) is placed first.
        Models with expired rate-limits or daily quotas are refreshed.
        Active rate-limited models are deprioritized after unconstrained models.
        """
        now = time.time()
        self._rate_limited_models = {m: exp for m, exp in self._rate_limited_models.items() if exp > now}
        self._daily_exhausted_models = {m: exp for m, exp in self._daily_exhausted_models.items() if exp > now}
        if self._project_daily_quota_reset_time and now > self._project_daily_quota_reset_time:
            self._project_daily_quota_exhausted = False
            self._project_daily_quota_reset_time = 0.0

        candidates = await self.get_candidate_models()
        pref = preferred.strip() if preferred and preferred not in ("auto", "smallest", "") else None

        ordered: List[str] = []
        if pref:
            ordered.append(pref)
        for c in candidates:
            if c not in ordered:
                ordered.append(c)

        active_models = [m for m in ordered if m not in self._failed_models]
        daily_available = [m for m in active_models if m not in self._daily_exhausted_models]

        if not daily_available:
            return []

        ready = [m for m in daily_available if m not in self._rate_limited_models]
        cooling = sorted(
            [m for m in daily_available if m in self._rate_limited_models],
            key=lambda m: self._rate_limited_models[m]
        )

        return ready + cooling

    async def resolve_smallest_model(self, force_refresh: bool = False) -> str:
        """
        Select the best available model for text generation without wasting API calls on probe pings.
        Filters out models that returned 404 (retired), exhausted daily quota, or are currently on rate-limit cooldown.
        """
        ordered = await self.get_ordered_models_to_try(preferred=self.model)
        if ordered:
            chosen = ordered[0]
            if chosen != self._resolved_model:
                logger.info("Active Gemini model selected: '%s'", chosen)
            self._resolved_model = chosen
            return chosen

        fallback = self.model if self.model not in ("auto", "smallest", "") else (self._discovered_candidates[0] if self._discovered_candidates else "gemini-flash")
        self._resolved_model = fallback
        return fallback

    async def _call_gemini_with_raw(
        self,
        prompt: str,
        schema_desc: str,
        operation: str = "ai_query",
        item_title: Optional[str] = None,
        system_instruction: Optional[str] = None,
    ) -> Tuple[Optional[dict], str, Optional[str]]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        default_persona = (
            "You are an expert TV and Movie media parser assistant for the *arr suite (Sonarr/Radarr).\n"
            f"Current Date: {today_str}.\n"
            "Your job is to accurately match messy release filenames, Chinese/Asian Pinyin titles, and foreign characters to official media."
        )
        sys_text = f"{default_persona}\n\n{system_instruction}" if system_instruction else default_persona
        full_prompt = f"[System Instruction]\n{sys_text}\n\n[User]\n{schema_desc}\n\n{prompt}"

        payload = {
            "systemInstruction": {
                "parts": [{"text": sys_text}]
            },
            "contents": [
                {
                    "parts": [
                        {
                            "text": f"{schema_desc}\n\n{prompt}" if schema_desc else prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1
            }
        }

        models_to_try = await self.get_ordered_models_to_try(preferred=self.model)
        if not models_to_try:
            p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
            is_daily = self.is_daily_quota_exhausted()
            err_code = "DAILY_QUOTA_EXHAUSTED" if is_daily else "NO_MODELS"
            err_msg = "All Gemini models have exhausted their daily token/request quota." if is_daily else "No candidate Gemini models are available."
            metrics.record_token_usage(
                operation=operation,
                model=self.model or "unknown",
                prompt_tokens=p_tok,
                completion_tokens=0,
                total_tokens=p_tok,
                duration_ms=0,
                item_title=item_title,
                success=False,
                error_code=err_code,
                prompt_text=full_prompt,
                response_text=err_msg,
            )
            if is_daily:
                raise DailyQuotaExhaustedError(err_msg)
            raise AIProviderError(err_msg)

        last_error = None
        last_model_used = models_to_try[0]
        overall_start_time = time.time()
        total_attempts = 0

        for pass_num in (1, 2):
            if pass_num == 2:
                models_to_try = await self.get_ordered_models_to_try(preferred=self.model)
                if not models_to_try:
                    break

            for current_model in models_to_try:
                total_attempts += 1
                last_model_used = current_model
                now = time.time()
                elapsed = now - self._last_call_time
                if elapsed < self._min_call_interval:
                    await asyncio.sleep(self._min_call_interval - elapsed)
                self._last_call_time = time.time()

                url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={self.api_key}"

                try:
                    start_time = time.time()
                    async with httpx.AsyncClient(timeout=25.0) as client:
                        resp = await client.post(url, json=payload)
                        status = resp.status_code

                        if status == 404:
                            attempt_dur_ms = int((time.time() - start_time) * 1000)
                            p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
                            metrics.record_token_usage(
                                operation=operation,
                                model=current_model,
                                prompt_tokens=p_tok,
                                completion_tokens=0,
                                total_tokens=p_tok,
                                duration_ms=attempt_dur_ms,
                                item_title=item_title,
                                success=False,
                                error_code="HTTP_404",
                                prompt_text=full_prompt,
                                response_text=f"HTTP 404 Not Found for {current_model}",
                            )
                            self._failed_models.add(current_model)
                            logger.warning(
                                "Gemini model '%s' returned 404 (retired). Trying next candidate Gemini model...",
                                current_model
                            )
                            last_error = httpx.HTTPStatusError(f"404 Not Found for {current_model}", request=resp.request, response=resp)
                            continue

                        if status == 429:
                            attempt_dur_ms = int((time.time() - start_time) * 1000)
                            p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
                            is_daily, reason = self._is_daily_quota_error(resp)
                            metrics.record_token_usage(
                                operation=operation,
                                model=current_model,
                                prompt_tokens=p_tok,
                                completion_tokens=0,
                                total_tokens=p_tok,
                                duration_ms=attempt_dur_ms,
                                item_title=item_title,
                                success=False,
                                error_code="DAILY_QUOTA_EXHAUSTED" if is_daily else "HTTP_429",
                                prompt_text=full_prompt,
                                response_text=f"HTTP 429 Quota/Rate Limit for {current_model}: {reason}",
                            )
                            if is_daily:
                                reset_epoch = get_daily_reset_epoch()
                                self._daily_exhausted_models[current_model] = reset_epoch
                                if (
                                    "project" in reason.lower()
                                    or "requestsperday" in resp.text.lower().replace("_", "")
                                    or "generatecontentrequestsperday" in resp.text.lower().replace("_", "")
                                ):
                                    self._project_daily_quota_exhausted = True
                                    self._project_daily_quota_reset_time = reset_epoch
                                    logger.warning(
                                        "Gemini project-level daily quota exhausted (%s). Halting cloud attempts for today.",
                                        reason
                                    )
                                    break
                                logger.warning(
                                    "Gemini model '%s' exhausted DAILY quota (%s). Trying next candidate Gemini model...",
                                    current_model, reason
                                )
                            else:
                                cooldown = self._parse_retry_after(resp, default=60)
                                self._rate_limited_models[current_model] = time.time() + cooldown
                                logger.warning(
                                    "Gemini model '%s' hit temporary rate limit (HTTP 429, retry after %ds). Trying next candidate Gemini model...",
                                    current_model, cooldown
                                )
                            last_error = httpx.HTTPStatusError(f"429 Quota/Rate Limit for {current_model}: {reason}", request=resp.request, response=resp)
                            continue

                        if status in (408, 500, 502, 503, 504, 520, 521, 522, 524):
                            attempt_dur_ms = int((time.time() - start_time) * 1000)
                            p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
                            err_code = "TIMEOUT" if status == 408 else f"HTTP_{status}"
                            metrics.record_token_usage(
                                operation=operation,
                                model=current_model,
                                prompt_tokens=p_tok,
                                completion_tokens=0,
                                total_tokens=p_tok,
                                duration_ms=attempt_dur_ms,
                                item_title=item_title,
                                success=False,
                                error_code=err_code,
                                prompt_text=full_prompt,
                                response_text=f"HTTP {status} ({resp.reason_phrase})",
                            )
                            self._rate_limited_models[current_model] = time.time() + 60
                            logger.warning(
                                "Gemini model '%s' returned HTTP %d (%s). Trying next candidate Gemini model...",
                                current_model, status, resp.reason_phrase
                            )
                            last_error = httpx.HTTPStatusError(f"{status} Server Error for {current_model}", request=resp.request, response=resp)
                            continue

                        if status in (401, 403):
                            dur_ms = int((time.time() - start_time) * 1000)
                            err_code, err_msg = extract_gemini_error_details(httpx.HTTPStatusError(f"{status} Unauthorized", request=resp.request, response=resp))
                            p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
                            metrics.record_token_usage(
                                operation=operation,
                                model=current_model,
                                prompt_tokens=p_tok,
                                completion_tokens=0,
                                total_tokens=p_tok,
                                duration_ms=dur_ms,
                                item_title=item_title,
                                success=False,
                                error_code=err_code,
                                prompt_text=full_prompt,
                                response_text=err_msg,
                            )
                            raise AIProviderError(f"Gemini API Key is invalid or unauthorized ({err_code})")

                        resp.raise_for_status()
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        if not candidates:
                            raise AIProviderError(f"No response candidates returned from Gemini: {data}")
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if not parts:
                            raise AIProviderError("Empty response parts from Gemini")

                        raw_text = parts[0].get("text", "").strip()
                        clean_json = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.MULTILINE)
                        clean_json = re.sub(r"\s*```$", "", clean_json, flags=re.MULTILINE)

                        usage = data.get("usageMetadata", {})
                        p_tok = usage.get("promptTokenCount", 0)
                        if p_tok == 0 and full_prompt:
                            p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
                        c_tok = usage.get("candidatesTokenCount", 0)
                        th_tok = usage.get("thoughtsTokenCount", 0)
                        t_tok = usage.get("totalTokenCount", p_tok + c_tok + th_tok)
                        if th_tok == 0 and t_tok > (p_tok + c_tok):
                            th_tok = t_tok - (p_tok + c_tok)
                        duration_ms = int((time.time() - start_time) * 1000)

                        self._resolved_model = current_model

                        metrics.record_token_usage(
                            operation=operation,
                            model=current_model,
                            prompt_tokens=p_tok,
                            thinking_tokens=th_tok,
                            completion_tokens=c_tok,
                            total_tokens=t_tok,
                            duration_ms=duration_ms,
                            item_title=item_title,
                            success=True,
                            prompt_text=full_prompt,
                            response_text=raw_text,
                        )

                        try:
                            return json.loads(clean_json), raw_text, None
                        except json.JSONDecodeError as e:
                            return None, raw_text, f"Invalid JSON syntax: {e}"

                except (httpx.RequestError, httpx.TimeoutException) as e:
                    attempt_dur_ms = int((time.time() - start_time) * 1000)
                    p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
                    err_code, err_msg = extract_gemini_error_details(e)
                    metrics.record_token_usage(
                        operation=operation,
                        model=current_model,
                        prompt_tokens=p_tok,
                        completion_tokens=0,
                        total_tokens=p_tok,
                        duration_ms=attempt_dur_ms,
                        item_title=item_title,
                        success=False,
                        error_code=err_code,
                        prompt_text=full_prompt,
                        response_text=err_msg,
                    )
                    self._rate_limited_models[current_model] = time.time() + 60
                    logger.warning(
                        "Gemini model '%s' timed out or encountered network error: %s. Trying next candidate Gemini model...",
                        current_model, e
                    )
                    last_error = e
                    continue
                except Exception as e:
                    attempt_dur_ms = int((time.time() - start_time) * 1000)
                    p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
                    err_code, err_msg = extract_gemini_error_details(e)
                    metrics.record_token_usage(
                        operation=operation,
                        model=current_model,
                        prompt_tokens=p_tok,
                        completion_tokens=0,
                        total_tokens=p_tok,
                        duration_ms=attempt_dur_ms,
                        item_title=item_title,
                        success=False,
                        error_code=err_code,
                        prompt_text=full_prompt,
                        response_text=err_msg,
                    )
                    logger.error("Unexpected error calling Gemini model '%s': %s", current_model, e)
                    last_error = e
                    break

            if self._project_daily_quota_exhausted:
                break

            if pass_num == 1:
                wait_time = 12.0
                logger.info(
                    "All candidate Gemini models failed in Pass 1. Pausing %.1fs before Pass 2 retry...",
                    wait_time
                )
                await asyncio.sleep(wait_time)

        if total_attempts == 0:
            dur_ms = int((time.time() - overall_start_time) * 1000)
            p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
            err_code, err_msg = ("NO_MODELS", "No candidate Gemini models were attempted")
            metrics.record_token_usage(
                operation=operation,
                model=last_model_used,
                prompt_tokens=p_tok,
                completion_tokens=0,
                total_tokens=p_tok,
                duration_ms=dur_ms,
                item_title=item_title,
                success=False,
                error_code=err_code,
                prompt_text=full_prompt,
                response_text=err_msg,
            )

        err_code, err_msg = extract_gemini_error_details(last_error) if last_error else ("NO_MODELS", "All candidate Gemini models failed")

        if self.is_daily_quota_exhausted():
            raise DailyQuotaExhaustedError(
                f"All Gemini models have exhausted their daily token/request quota for today ({err_code}: {err_msg})"
            )

        raise AIProviderError(
            f"All candidate Gemini models failed across 2 retry passes ({total_attempts} attempts) ({err_code}: {err_msg})"
        )

    async def _call_gemini(
        self,
        prompt: str,
        schema_desc: str,
        operation: str = "ai_query",
        item_title: Optional[str] = None
    ) -> dict:
        parsed, raw_text, err = await self._call_gemini_with_raw(
            prompt=prompt,
            schema_desc=schema_desc,
            operation=operation,
            item_title=item_title,
        )
        if err or parsed is None:
            raise AIProviderError(f"Failed to parse JSON response from Gemini: {raw_text}")
        return parsed

    async def _repair_with_stage3(
        self,
        stage_name: str,
        raw_output: str,
        validation_errors: List[str],
        target_schema: str,
        reference_context: Dict[str, Any],
        item_title: Optional[str] = None,
    ) -> dict:
        logger.warning(
            "Stage '%s' output from '%s' failed schema validation (%s). Running Stage 3 AI Output Interpreter...",
            stage_name,
            self.name,
            "; ".join(validation_errors),
        )
        sys_p, usr_p = build_stage3_repair_prompt(
            stage_name=stage_name,
            raw_output=raw_output,
            validation_errors=validation_errors,
            target_schema=target_schema,
            reference_context=reference_context,
        )
        return await self._call_gemini(
            prompt=usr_p,
            schema_desc=sys_p,
            operation="stage3_output_interpreter",
            item_title=item_title,
        )

    async def evaluate_stage1_triage(
        self,
        release_title: str,
        error_messages: List[str],
        disk_files: List[str],
        archive_name: Optional[str] = None,
        archive_files: Optional[List[str]] = None,
    ) -> Stage1TriageResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "reasoning": "brief explanation of triage evaluation",\n'
            '  "action": "extract" | "no_extraction" | "reject_delete",\n'
            '  "category_tag": "malware_or_corrupt" | "sample_only" | "archive_extracted" | "triage_passed",\n'
            '  "confidence": float (0.0 to 1.0)\n'
            "}"
        )
        prompt = (
            f"Release Title: {release_title}\n"
            f"Queue Error / Warning Messages: {json.dumps(error_messages, ensure_ascii=False)}\n"
            f"Files on Disk: {json.dumps(disk_files[:30], ensure_ascii=False)}\n"
            f"Archive Detected: {archive_name or 'None'}\n"
            f"Files Inside Archive: {json.dumps((archive_files or [])[:30], ensure_ascii=False)}\n\n"
            "Stage 1 Triage Task:\n"
            "1. Choose 'reject_delete' ONLY if the release is a fake/malware/virus release (e.g. .exe, .scr, .bat, .lnk, .iso, crypted binaries, password-protected scam archive), corrupt/empty archive, or contains ONLY disposable sample/junk files with no real video.\n"
            "2. Choose 'extract' if there is a valid archive containing real video media files that need unpacking.\n"
            "3. Choose 'no_extraction' if valid unpacked video files are already on disk.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_stage1_triage",
            item_title=release_title
        )
        val_errors = [parse_err] if parse_err else validate_stage1_payload(data)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_stage1_triage",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "release_title": release_title,
                    "error_messages": error_messages[:10],
                    "disk_files": disk_files[:15],
                    "archive_name": archive_name,
                },
                item_title=release_title,
            )
            post_errors = validate_stage1_payload(data)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair Stage 1 output: {'; '.join(post_errors)}"
                )

        assert isinstance(data, dict)
        return Stage1TriageResult(
            action=str(data.get("action", "no_extraction")),
            category_tag=str(data.get("category_tag", "malware_or_corrupt")),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name
        )

    async def evaluate_import_decision(
        self,
        media_type: str,
        release_title: str,
        folder_name: str,
        queue_messages: List[str],
        candidate_media: Optional[Dict[str, Any]],
        library_candidates: Optional[List[Dict[str, Any]]],
        manual_import_files: List[Dict[str, Any]],
        existing_disk_files: List[Dict[str, Any]],
        known_episodes: Optional[List[Dict[str, Any]]] = None,
        raw_disk_files: Optional[List[str]] = None,
    ) -> Stage2DecisionResult:
        comp_candidates, comp_existing, comp_known = compact_stage2_context(
            manual_import_files=manual_import_files,
            existing_disk_files=existing_disk_files,
            known_episodes=known_episodes,
        )
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "reasoning": "clear step-by-step explanation of identity match, file mapping, and quality upgrade/downgrade check",\n'
            '  "decision": "import" | "reject_keep_in_client" | "reject_delete" | "dismiss_pack",\n'
            '  "matched_media_id": integer or null,\n'
            '  "files_to_import": [\n'
            '    {\n'
            '      "path": "exact file path from candidate files",\n'
            '      "season_number": integer or null,\n'
            '      "episode_numbers": [integer]\n'
            '    }\n'
            '  ],\n'
            '  "category_tag": "id_match" | "unknown_media" | "episode_parsed" | "quality_downgrade" | "wrong_media" | "sample_only" | "malware_or_corrupt" | "season_pack",\n'
            '  "confidence": float (0.0 to 1.0)\n'
            "}"
        )
        prompt_parts = [
            f"Media Type: {media_type} ({'Sonarr TV Series' if media_type == 'series' else 'Radarr Movie'})",
            f"Release Title: {release_title}",
            f"Folder Name: {folder_name}",
            f"Queue Status Messages: {json.dumps(queue_messages[:15], ensure_ascii=False)}",
            f"Candidate Media (if known by *arr): {json.dumps(candidate_media, ensure_ascii=False)}",
        ]
        if library_candidates:
            prompt_parts.append(
                f"Library Candidates (if media unknown): {json.dumps(library_candidates[:25], ensure_ascii=False)}"
            )
        prompt_parts.append(
            f"Candidate Files from /manualimport: {json.dumps(comp_candidates, ensure_ascii=False, indent=2)}"
        )
        if not comp_candidates and raw_disk_files:
            prompt_parts.append(
                f"Raw Files on Disk (if /manualimport found 0): {json.dumps(raw_disk_files[:30], ensure_ascii=False)}"
            )
        prompt_parts.append(
            f"Existing Imported Files Already on Disk: {json.dumps(comp_existing, ensure_ascii=False)}"
        )
        if comp_known is not None:
            prompt_parts.append(
                f"Known Episodes in Series (if TV): {json.dumps(comp_known, ensure_ascii=False)}"
            )
        prompt_parts.append(f"\nREQUIRED JSON OUTPUT SCHEMA:\n{schema}")
        prompt = "\n".join(prompt_parts)
        system_instruction = (
            "Stage 2 Unified Import vs. Reject Evaluation Rules:\n"
            "1. Identity & Matching Verification (CRITICAL):\n"
            "   - Verify whether the release title genuinely corresponds to Candidate Media (or one of Library Candidates, accounting for aliases, foreign/translated titles, Pinyin, Romaji, and festival/theatrical release year +/- 1).\n"
            "   - ANTI-HALLUCINATION CAUTION: Do NOT assume a release belongs to Candidate Media simply because Sonarr/Radarr matched it by ID or grab history! Indexers frequently suffer from ID collisions or incorrect grab mappings. If the release title represents a DIFFERENT show or movie (e.g. Release title 'A Love Other Than Yours' vs Candidate Media '100 Days of Deception'), they are distinct works.\n"
            "   - If the release does not match Candidate Media, check whether it clearly belongs to another entry in Library Candidates (for example, if *arr grabbed the release under the wrong ID). If it matches a library candidate needing this content, you may set 'matched_media_id' to that candidate's ID and import it. If it does not belong to any monitored media in the library, choose 'reject_keep_in_client' with category_tag 'wrong_media'.\n"
            "   - In your 'reasoning', you MUST explicitly explain the title correspondence, translation, or alias. Merely stating 'matched via grab history' or 'matched by ID' is strictly forbidden as justification for import.\n"
            "2. Upgrade vs. Missing Content Evaluation:\n"
            "   - If the target episode or movie is MISSING on disk (hasFile == false), prioritize importing it to satisfy the missing media unless the file is corrupt, a disposable sample, or an unwanted language.\n"
            "   - If the target episode or movie is ALREADY on disk, compare candidate file quality, Custom Format score, resolution, and size against existing files on disk. If the candidate is a genuine quality upgrade, choose 'import'. If it is equal, lower quality, or not a Custom Format upgrade over the disk file (or Sonarr/Radarr rejected it as 'Existing file on disk has equal or higher quality' or 'Not a Custom Format upgrade'), DO NOT import it—choose 'reject_keep_in_client'.\n"
            "3. Sample & Extra Filtering:\n"
            "   - Inspect candidate file sizes and names. Exclude disposable preview clips, trailers, promos, or sample files (often under 100MB or named 'sample', 'trailer', 'extra') from files_to_import, while approving genuine full episodes or movies.\n"
            "4. Choose ONE decision:\n"
            "   - 'import': The release matches the target media AND at least one valid video file is either missing on disk or a genuine quality upgrade. Populate matched_media_id and files_to_import (with season_number and episode_numbers for TV series).\n"
            "   - 'reject_keep_in_client': The release is a quality downgrade/not an upgrade over existing disk files, OR is the wrong series/movie or unwanted language. (Preserves files in download client while blocklisting in *arr).\n"
            "   - 'reject_delete': ONLY when the files are virus/malware, 100% disposable samples/trailers with no real media, or corrupt/unusable files. (Deletes from client and blocklists).\n"
            "   - 'dismiss_pack': A multi-episode/season pack where needed episodes were already imported and remaining episodes are missing or already satisfied on disk."
        )
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_import_decision",
            item_title=release_title,
            system_instruction=system_instruction,
        )
        val_errors = (
            [parse_err]
            if parse_err
            else validate_stage2_payload(data, candidate_media, library_candidates, manual_import_files)
        )
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_import_decision",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "release_title": release_title,
                    "candidate_media": candidate_media,
                    "library_candidates": (library_candidates or [])[:20],
                    "valid_candidate_files": [
                        {
                            "path": m.get("path"),
                            "name": m.get("name"),
                            "episodes": m.get("episodes", []),
                        }
                        for m in comp_candidates
                    ],
                },
                item_title=release_title,
            )
            post_errors = validate_stage2_payload(data, candidate_media, library_candidates, manual_import_files)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair Stage 2 output: {'; '.join(post_errors)}"
                )

        assert isinstance(data, dict)
        files = []
        for f in data.get("files_to_import") or []:
            if isinstance(f, dict) and f.get("path"):
                ep_nums = f.get("episode_numbers") or []
                if isinstance(ep_nums, int):
                    ep_nums = [ep_nums]
                files.append(
                    ApprovedFileItem(
                        path=str(f["path"]),
                        season_number=int(f["season_number"]) if f.get("season_number") is not None else None,
                        episode_numbers=[int(x) for x in ep_nums],
                    )
                )
        return Stage2DecisionResult(
            decision=str(data.get("decision", "reject_keep_in_client")),
            matched_media_id=int(data["matched_media_id"]) if data.get("matched_media_id") is not None else None,
            files_to_import=files,
            category_tag=str(data.get("category_tag", "ai_decision")),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name,
        )

    async def evaluate_backfill_season_pack(
        self,
        series_info: Dict[str, Any],
        season_number: int,
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillPackMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "reasoning": "step-by-step analysis: 1. check series title & aliases; 2. check exact season specification; 3. quality comparison and conclusion",\n'
            '  "matched_guid": "exact guid from candidate_releases or null if none match",\n'
            '  "confidence": float (0.0 to 1.0)\n'
            "}"
        )
        prompt = (
            f"Target Series: {json.dumps(series_info, ensure_ascii=False)}\n"
            f"Target Season Number: Season {season_number:02d}\n"
            f"Candidate Season Pack Releases (flagged by Sonarr as 'Unknown Series'):\n"
            f"{json.dumps(candidate_releases[:25], ensure_ascii=False, indent=2)}\n\n"
            "Task:\n"
            "1. Series Identity: Verify that the candidate release belongs to Target Series (accounting for alternate titles, Chinese Pinyin, Romaji, foreign titles, or year variations).\n"
            f"2. Season Verification: Candidate release MUST be a full-season pack specifically for Season {season_number:02d} (e.g. 'S{season_number:02d}', 'Season {season_number}', or 'Season {season_number:02d}').\n"
            "   - CAUTION: Reject multi-season packs or complete series packs (e.g. 'S01-S02', 'complete S01 and 2', 'Seasons 1-4', 'Complete Series') when searching for a single target season, as they span multiple seasons or may be misidentified.\n"
            "   - Reject releases belonging to other seasons (e.g. S02, S03).\n"
            f"3. If multiple candidates are valid packs for Season {season_number:02d}, pick the one with highest customFormatScore / qualityWeight / seeders.\n"
            f"4. If none of the candidates are valid packs for Season {season_number:02d}, return matched_guid: null with confidence 0.0.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        series_label = f"{series_info.get('title', 'Series')} S{season_number:02d}"
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_backfill_season_pack",
            item_title=series_label,
        )
        val_errors = [parse_err] if parse_err else validate_backfill_payload(data, candidate_releases)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_backfill_season_pack",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "target_series": series_info,
                    "season_number": season_number,
                    "candidate_releases": candidate_releases[:25],
                },
                item_title=series_label,
            )
            post_errors = validate_backfill_payload(data, candidate_releases)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair backfill output: {'; '.join(post_errors)}"
                )
        assert isinstance(data, dict)
        return BackfillPackMatchResult(
            matched_guid=str(data["matched_guid"]) if data.get("matched_guid") else None,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name,
        )

    async def evaluate_backfill_episode(
        self,
        series_info: Dict[str, Any],
        season_number: int,
        episode_number: int,
        episode_title: str,
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillPackMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "reasoning": "step-by-step analysis: 1. check series title & aliases; 2. verify exact season and episode; 3. quality comparison and conclusion",\n'
            '  "matched_guid": "exact guid from candidate_releases or null if none match",\n'
            '  "confidence": float (0.0 to 1.0)\n'
            "}"
        )
        prompt = (
            f"Target Series: {json.dumps(series_info, ensure_ascii=False)}\n"
            f"Target Episode: S{season_number:02d}E{episode_number:02d} ({episode_title})\n"
            f"Candidate Episode Releases (flagged by Sonarr as 'Unknown Series'):\n"
            f"{json.dumps(candidate_releases[:25], ensure_ascii=False, indent=2)}\n\n"
            "Task:\n"
            "1. Series Identity: Verify that the candidate release belongs to Target Series (accounting for alternate titles, Chinese Pinyin, Romaji, foreign titles, or year variations).\n"
            f"2. Episode Verification: Candidate release MUST be a genuine episode release specifically for S{season_number:02d}E{episode_number:02d} "
            "(or a multi-episode release containing this episode). Reject releases belonging to different seasons or episodes.\n"
            "3. If multiple candidates match the series and episode, pick the one with the highest customFormatScore / qualityWeight / seeders.\n"
            "4. If none of the candidates actually belong to this series and episode, return matched_guid: null with confidence 0.0.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        ep_label = f"{series_info.get('title', 'Series')} S{season_number:02d}E{episode_number:02d}"
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_backfill_episode",
            item_title=ep_label,
        )
        val_errors = [parse_err] if parse_err else validate_backfill_payload(data, candidate_releases)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_backfill_episode",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "target_series": series_info,
                    "season_number": season_number,
                    "episode_number": episode_number,
                    "candidate_releases": candidate_releases[:25],
                },
                item_title=ep_label,
            )
            post_errors = validate_backfill_payload(data, candidate_releases)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair episode retry output: {'; '.join(post_errors)}"
                )
        assert isinstance(data, dict)
        return BackfillPackMatchResult(
            matched_guid=str(data["matched_guid"]) if data.get("matched_guid") else None,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name,
        )

    async def evaluate_backfill_episodes_bulk(
        self,
        series_info: Dict[str, Any],
        season_number: int,
        missing_episodes: List[Dict[str, Any]],
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillBulkEpisodesMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "matches": [\n'
            '    {\n'
            '      "episode_number": integer,\n'
            '      "reasoning": "step-by-step rationale for matching or rejecting candidate releases for this episode",\n'
            '      "matched_guid": "exact guid from candidate_releases or null if none match",\n'
            '      "confidence": float (0.0 to 1.0)\n'
            '    }\n'
            '  ]\n'
            "}"
        )
        prompt = (
            f"Target Series: {json.dumps(series_info, ensure_ascii=False)}\n"
            f"Target Season: Season {season_number:02d}\n"
            f"Missing Episodes to Match: {json.dumps([{'episode_number': e.get('episodeNumber'), 'title': e.get('title')} for e in missing_episodes], ensure_ascii=False)}\n"
            f"Candidate Releases from Season Search:\n{json.dumps(candidate_releases[:30], ensure_ascii=False, indent=2)}\n\n"
            "Task:\n"
            "1. For each missing episode, determine if any candidate release is a genuine release for that episode of the Target Series "
            "(accounting for aliases, Chinese Pinyin, Romaji, and foreign release titles).\n"
            "2. Note: A multi-episode release (e.g. EP01-EP02 or S01E01-E02) may match multiple missing episodes.\n"
            "3. If multiple releases match an episode, pick the one with highest qualityWeight / customFormatScore / seeders.\n"
            "4. If no candidate release matches an episode, set matched_guid to null with confidence 0.0.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        series_label = f"{series_info.get('title', 'Series')} S{season_number:02d} Bulk ({len(missing_episodes)} eps)"
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_backfill_episodes_bulk",
            item_title=series_label,
        )
        val_errors = [parse_err] if parse_err else validate_bulk_episodes_payload(data, candidate_releases, missing_episodes)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_backfill_episodes_bulk",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "target_series": series_info,
                    "season_number": season_number,
                    "missing_episodes": missing_episodes,
                    "candidate_releases": candidate_releases[:30],
                },
                item_title=series_label,
            )
            post_errors = validate_bulk_episodes_payload(data, candidate_releases, missing_episodes)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair bulk episode retry output: {'; '.join(post_errors)}"
                )
        assert isinstance(data, dict)
        matches = [
            EpisodeMatchItem(
                episode_number=int(m["episode_number"]),
                matched_guid=str(m["matched_guid"]) if m.get("matched_guid") else None,
                confidence=float(m.get("confidence", 0.0)),
                reasoning=str(m.get("reasoning", "")),
            )
            for m in (data.get("matches") or [])
            if isinstance(m, dict) and m.get("episode_number") is not None
        ]
        return BackfillBulkEpisodesMatchResult(matches=matches, model_name=self.name)

    async def evaluate_missing_movie(
        self,
        movie_info: Dict[str, Any],
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillPackMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "reasoning": "step-by-step analysis: 1. check movie title & aliases; 2. check release year (allow ±1 year for festival/streaming/theatrical variance); 3. verify full movie release (not trailer/sample/OST); 4. quality comparison and conclusion",\n'
            '  "matched_guid": "exact guid from candidate_releases or null if none match",\n'
            '  "confidence": float (0.0 to 1.0)\n'
            "}"
        )
        prompt = (
            f"Target Movie: {json.dumps(movie_info, ensure_ascii=False)}\n"
            f"Candidate Movie Releases (flagged by Radarr as 'Unknown Movie' or rejected):\n"
            f"{json.dumps(candidate_releases[:25], ensure_ascii=False, indent=2)}\n\n"
            "Task:\n"
            "1. Movie Identity: Verify that the candidate release belongs to Target Movie (accounting for alternate titles, original foreign titles, Romaji, Chinese Pinyin, translated names, or year variations ±1 year).\n"
            "2. Content Verification: Candidate release MUST be a full movie release. Reject trailers, samples, soundtracks/OST, featurettes, or different movies.\n"
            "3. If multiple candidates are valid matches for Target Movie, pick the one with highest customFormatScore / qualityWeight / seeders.\n"
            "4. If none of the candidates match Target Movie, return matched_guid: null with confidence 0.0.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        movie_label = f"{movie_info.get('title', 'Movie')} ({movie_info.get('year', '')})"
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_missing_movie",
            item_title=movie_label,
        )
        val_errors = [parse_err] if parse_err else validate_backfill_payload(data, candidate_releases)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_missing_movie",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "target_movie": movie_info,
                    "candidate_releases": candidate_releases[:25],
                },
                item_title=movie_label,
            )
            post_errors = validate_backfill_payload(data, candidate_releases)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair movie match output: {'; '.join(post_errors)}"
                )
        assert isinstance(data, dict)
        return BackfillPackMatchResult(
            matched_guid=str(data["matched_guid"]) if data.get("matched_guid") else None,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name,
        )

    async def verify_media_alias(
        self,
        release_title: str,
        candidate_media: Dict[str, Any],
        media_type: str = "series",
    ) -> AliasVerificationResult:
        """
        Focused zero-shot identity verification: Confirm whether a release title is genuinely an
        alternate title, translation, or alias of candidate_media, or an entirely different show/movie.
        """
        media_label = "TV Series" if media_type == "series" else "Movie"
        target_title = candidate_media.get("title", "")
        target_year = candidate_media.get("year", "")
        target_aliases = candidate_media.get("aliases", [])

        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "is_same_media": boolean,\n'
            '  "media_relationship": "identical" | "official_translation" | "alternate_alias" | "completely_different_media",\n'
            '  "confidence": float (0.0 to 1.0),\n'
            '  "reasoning": "step-by-step verification explaining whether the release title refers to this specific production or a different work"\n'
            "}"
        )
        prompt = (
            f"You are an expert entertainment metadata investigator specializing in global film and television titles.\n"
            f"Current Date: {datetime.now().strftime('%Y-%m-%d')}.\n\n"
            f"Task: Determine if the release filename refers to the Target {media_label}, OR if it is an entirely DIFFERENT production.\n\n"
            f"Target {media_label}: {target_title} ({target_year})\n"
            f"Known Aliases/Alternate Titles: {json.dumps(target_aliases, ensure_ascii=False)}\n"
            f"Release Filename: {release_title}\n\n"
            "Investigation Instructions:\n"
            f"1. Is '{release_title}' the original title, an official localized title, an international translation (e.g. English, CJK, Cyrillic, Pinyin, Romaji), or a verified working title of the {media_label} '{target_title}' ({target_year})?\n"
            f"2. CRITICAL CAUTION: Different productions can have separate releases released in the same year or on similar networks. If the release title refers to a distinct, separate {media_label} (for example, 'A Love Other Than Yours' vs '100 Days of Deception'), they are TWO COMPLETELY DIFFERENT PRODUCTIONS.\n"
            "3. If they are different productions, set is_same_media: false and media_relationship: 'completely_different_media'.\n"
            "4. Only set is_same_media: true if you have high confidence that this release is genuinely the exact same production.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="verify_media_alias",
            item_title=f"{target_title} vs {release_title}",
        )
        val_errors = [parse_err] if parse_err else validate_alias_payload(data)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="verify_media_alias",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "target_media": candidate_media,
                    "release_title": release_title,
                },
                item_title=f"{target_title} vs {release_title}",
            )
            post_errors = validate_alias_payload(data)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair alias verification output: {'; '.join(post_errors)}"
                )

        assert isinstance(data, dict)
        return AliasVerificationResult(
            is_same_media=bool(data.get("is_same_media", False)),
            media_relationship=str(data.get("media_relationship", "completely_different_media")),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name,
        )

