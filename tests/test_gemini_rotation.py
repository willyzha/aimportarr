import json
import time
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

import httpx

from src.ai.base import (
    AIProviderError,
    DailyQuotaExhaustedError,
    Stage1TriageResult,
    Stage2DecisionResult,
)
from src.ai.gemini import GeminiProvider, is_daily_quota_message, get_daily_reset_epoch
from src.ai.fallback import FallbackProvider
from src.config import AIConfig, AppConfig, config_to_dict, update_config_from_dict
from src.web import RENDERED_DASHBOARD_HTML


class GeminiRotationAndDailyQuotaTests(unittest.IsolatedAsyncioTestCase):

    def test_daily_quota_message_detection(self):
        """Test heuristic detection of daily quota vs per-minute rate limits."""
        # Daily quota indicators
        self.assertTrue(is_daily_quota_message("Resource has been exhausted: GenerateContentRequestsPerDay"))
        self.assertTrue(is_daily_quota_message("Quota exceeded for quota metric 'RequestsPerDay'"))
        self.assertTrue(is_daily_quota_message("Daily quota limit exceeded for project 123456"))
        self.assertTrue(is_daily_quota_message("Quota exceeded for metric: per_day_requests"))
        self.assertTrue(is_daily_quota_message("Exceeded maximum requests per day"))

        # Per-minute / transient indicators
        self.assertFalse(is_daily_quota_message("Resource has been exhausted: GenerateContentRequestsPerMinute"))
        self.assertFalse(is_daily_quota_message("Rate limit exceeded. Please wait 15 seconds."))
        self.assertFalse(is_daily_quota_message("Quota metric: RequestsPerMinute"))
        self.assertFalse(is_daily_quota_message("503 Service Unavailable"))

    def test_daily_reset_epoch_calculation(self):
        """Verify get_daily_reset_epoch calculates next 08:00 UTC."""
        epoch = get_daily_reset_epoch()
        now = time.time()
        self.assertGreater(epoch, now)
        # Reset epoch should be within 24 hours + margin
        self.assertLessEqual(epoch - now, 86400 + 3600)

    async def test_candidate_models_filtering_and_ranking(self):
        """Verify non-text models are filtered and models are sorted by tier (Flash -> Lite -> Pro)."""
        prov = GeminiProvider(api_key="fake-key", model="gemini-3.6-flash")

        raw_models = [
            "gemini-2.5-pro",
            "gemini-3.6-flash",
            "gemini-2.5-flash-image",
            "gemini-3.5-flash-lite",
            "gemini-2.5-flash-tts",
            "gemini-flash-banana",
            "gemini-2.5-flash",
            "gemini-3.5-flash",
            "text-embedding-004",
            "aqa",
        ]

        candidates = await prov.get_candidate_models(raw_models)

        # Non-text models filtered
        self.assertNotIn("gemini-2.5-flash-image", candidates)
        self.assertNotIn("gemini-2.5-flash-tts", candidates)
        self.assertNotIn("gemini-flash-banana", candidates)
        self.assertNotIn("text-embedding-004", candidates)
        self.assertNotIn("aqa", candidates)

        # Standard text models kept
        self.assertIn("gemini-3.6-flash", candidates)
        self.assertIn("gemini-3.5-flash", candidates)
        self.assertIn("gemini-2.5-flash", candidates)
        self.assertIn("gemini-3.5-flash-lite", candidates)
        self.assertIn("gemini-2.5-pro", candidates)

        # Flash models should precede Pro models
        idx_flash = candidates.index("gemini-3.6-flash")
        idx_pro = candidates.index("gemini-2.5-pro")
        self.assertLess(idx_flash, idx_pro)

    async def test_ordered_models_puts_preferred_first(self):
        """Verify get_ordered_models_to_try puts the user preferred model first."""
        prov = GeminiProvider(api_key="fake-key", model="gemini-3.5-flash")
        prov._discovered_candidates = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"]
        ordered = await prov.get_ordered_models_to_try(preferred="gemini-3.5-flash")
        self.assertEqual(ordered[0], "gemini-3.5-flash")
        self.assertIn("gemini-3.6-flash", ordered)

    async def test_gemini_rotates_model_on_transient_rate_limit(self):
        """
        Verify that if preferred model hits 429 RPM, GeminiProvider immediately rotates
        to the next candidate model and returns its response.
        """
        prov = GeminiProvider(api_key="fake-key", model="gemini-3.6-flash")
        prov._min_call_interval = 0  # Disable sleep for fast unit tests
        prov._discovered_candidates = ["gemini-3.6-flash", "gemini-3.5-flash"]

        calls = []

        class Mock429Resp:
            status_code = 429
            reason_phrase = "Too Many Requests"
            text = '{"error": {"code": 429, "message": "Resource has been exhausted.", "details": [{"violations": [{"quotaMetric": "GenerateContentRequestsPerMinute"}]}]}}'
            headers = {"retry-after": "15"}
            request = MagicMock()

            def json(self):
                return json.loads(self.text)

            def raise_for_status(self):
                raise httpx.HTTPStatusError("429 Too Many Requests", request=self.request, response=self)

        class Mock200Resp:
            status_code = 200
            reason_phrase = "OK"
            request = MagicMock()

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "candidates": [{
                        "content": {
                            "parts": [{"text": '{"decision": "import", "confidence": 0.95, "reasoning": "Rotated model succeeded", "files_to_import": []}'}]
                        }
                    }]
                }

        async def mock_post(client, url, json=None):
            model_name = url.split("/models/")[1].split(":")[0]
            calls.append(model_name)
            if model_name == "gemini-3.6-flash":
                return Mock429Resp()
            return Mock200Resp()

        with patch("httpx.AsyncClient.post", new=mock_post):
            data, raw, think = await prov._call_gemini_with_raw(
                prompt="test prompt",
                schema_desc="test schema",
                operation="test_rotate"
            )

            # Verification:
            # 1. First model was attempted
            self.assertEqual(calls[0], "gemini-3.6-flash")
            # 2. Rotated to second model
            self.assertGreater(len(calls), 1)
            self.assertNotEqual(calls[1], "gemini-3.6-flash")
            # 3. Success data was parsed from the rotated model
            self.assertEqual(data["decision"], "import")
            self.assertEqual(data["confidence"], 0.95)
            self.assertEqual(data["reasoning"], "Rotated model succeeded")
            # 4. gemini-3.6-flash is in rate-limit cooldown
            self.assertIn("gemini-3.6-flash", prov._rate_limited_models)
            self.assertGreater(prov._rate_limited_models["gemini-3.6-flash"], time.time())

    async def test_gemini_attempts_all_models_without_cap_and_two_passes(self):
        """
        Verify that GeminiProvider attempts ALL candidate models (e.g. 10 models) without any
        hardcoded cap (like 6), pauses between passes, and attempts Pass 2.
        """
        prov = GeminiProvider(api_key="fake-key", model="gemini-model-0")
        prov._min_call_interval = 0
        prov._discovered_candidates = [f"gemini-model-{i}" for i in range(10)]

        calls = []

        class Mock429Resp:
            status_code = 429
            reason_phrase = "Too Many Requests"
            text = '{"error": {"code": 429, "message": "Resource has been exhausted: GenerateContentRequestsPerMinute."}}'
            headers = {"retry-after": "5"}
            request = MagicMock()

            def json(self):
                return json.loads(self.text)

            def raise_for_status(self):
                raise httpx.HTTPStatusError("429 Too Many Requests", request=self.request, response=self)

        class Mock200Resp:
            status_code = 200
            reason_phrase = "OK"
            request = MagicMock()

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "candidates": [{
                        "content": {
                            "parts": [{"text": '{"decision": "import", "confidence": 0.99, "reasoning": "Pass 2 succeeded", "files_to_import": []}'}]
                        }
                    }]
                }

        async def mock_post(client, url, json=None):
            model_name = url.split("/models/")[1].split(":")[0]
            calls.append(model_name)
            # In Pass 1 (first 10 calls), fail all of them
            if len(calls) < 11:
                return Mock429Resp()
            # On 11th call (Pass 2), succeed
            return Mock200Resp()

        with patch("httpx.AsyncClient.post", new=mock_post), patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            data, raw, think = await prov._call_gemini_with_raw(
                prompt="test prompt",
                schema_desc="test schema",
                operation="test_all_models"
            )

            # All 10 models must have been attempted in Pass 1 (verifying no cap of 6)
            self.assertGreaterEqual(len(calls), 11)
            for i in range(10):
                self.assertIn(f"gemini-model-{i}", calls[:10])

            # Sleep between passes was called
            mock_sleep.assert_any_call(12.0)
            self.assertEqual(data["decision"], "import")
            self.assertEqual(data["reasoning"], "Pass 2 succeeded")

    async def test_dynamic_discovery_from_api_without_hardcoded_names(self):
        """
        Verify that models are discovered dynamically via Google API without hardcoded model names,
        and dynamically ranked and selected.
        """
        prov = GeminiProvider(api_key="fake-key", model="gemini-future-4.0-flash")

        class MockCatalogResp:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return {
                    "models": [
                        {"name": "models/gemini-future-pro", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/gemini-future-4.0-flash", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/gemini-future-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]}
                    ]
                }

        async def mock_get(client, url):
            return MockCatalogResp()

        with patch("httpx.AsyncClient.get", new=mock_get):
            candidates = await prov.get_candidate_models()
            self.assertIn("gemini-future-4.0-flash", candidates)
            self.assertIn("gemini-future-flash-lite", candidates)
            self.assertIn("gemini-future-pro", candidates)
            self.assertNotIn("embedding-001", candidates)
            # Flash tier prioritized over Pro
            idx_flash = candidates.index("gemini-future-4.0-flash")
            idx_pro = candidates.index("gemini-future-pro")
            self.assertLess(idx_flash, idx_pro)

    async def test_gemini_daily_quota_exhaustion_raises_special_error_and_breaks_early(self):
        """
        Verify that when a 429 response indicates project daily quota exhaustion (PerDay),
        GeminiProvider breaks early, raises DailyQuotaExhaustedError, and sets is_daily_quota_exhausted().
        """
        prov = GeminiProvider(api_key="fake-key", model="gemini-3.6-flash")
        prov._min_call_interval = 0
        prov._discovered_candidates = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"]

        calls = []

        class MockDaily429Resp:
            status_code = 429
            reason_phrase = "Too Many Requests"
            text = '{"error": {"code": 429, "message": "Resource has been exhausted: Quota exceeded for GenerateContentRequestsPerDay.", "status": "RESOURCE_EXHAUSTED", "details": [{"violations": [{"quotaMetric": "GenerateContentRequestsPerDay"}]}]}}'
            headers = {}
            request = MagicMock()

            def json(self):
                return json.loads(self.text)

            def raise_for_status(self):
                raise httpx.HTTPStatusError("429 Daily Quota Exceeded", request=self.request, response=self)

        async def mock_post(client, url, json=None):
            model_name = url.split("/models/")[1].split(":")[0]
            calls.append(model_name)
            return MockDaily429Resp()

        with patch("httpx.AsyncClient.post", new=mock_post):
            with self.assertRaises(DailyQuotaExhaustedError):
                await prov._call_gemini_with_raw(
                    prompt="test prompt",
                    schema_desc="test schema",
                    operation="test_daily"
                )

        # Provider should recognize daily exhaustion
        self.assertTrue(prov.is_daily_quota_exhausted())
        # Should fast-break: only 1 call was needed to detect project daily quota
        self.assertEqual(len(calls), 1)

    async def test_fallback_provider_automatic_fallback_on_gemini_failure(self):
        """
        Verify FallbackProvider seamlessly delegates to Ollama when Gemini fails after exhausting
        all models/retries or upon daily quota exhaustion.
        """
        mock_primary = MagicMock()
        mock_fallback = MagicMock()
        mock_primary.model = "gemini-3.6-flash"
        mock_primary.name = "Gemini (gemini-3.6-flash)"
        mock_fallback.model = "qwen2.5:14b"
        mock_fallback.name = "Ollama (qwen2.5:14b)"

        fb_result = Stage2DecisionResult(
            decision="import",
            confidence=0.92,
            reasoning="Fallback Ollama decision",
            files_to_import=[]
        )
        mock_fallback.evaluate_import_decision = AsyncMock(return_value=fb_result)

        fb_provider = FallbackProvider(primary=mock_primary, fallback=mock_fallback)

        # Case A: Gemini exhausts retries and raises AIProviderError
        mock_primary.evaluate_import_decision = AsyncMock(side_effect=AIProviderError("All Gemini models failed across 2 retry passes"))
        res_error = await fb_provider.evaluate_import_decision(
            media_type="series",
            release_title="Show.S01E01",
            folder_name="Show.S01E01",
            queue_messages=[],
            candidate_media={},
            library_candidates=[],
            manual_import_files=[],
            existing_disk_files=[],
        )
        mock_fallback.evaluate_import_decision.assert_called_once()
        self.assertEqual(res_error.decision, "import")
        self.assertEqual(res_error.confidence, 0.92)

        # Case B: Gemini raises DailyQuotaExhaustedError
        mock_fallback.evaluate_import_decision.reset_mock()
        mock_primary.evaluate_import_decision = AsyncMock(side_effect=DailyQuotaExhaustedError("Daily quota exhausted for Gemini"))
        res_daily = await fb_provider.evaluate_import_decision(
            media_type="series",
            release_title="Show.S01E01",
            folder_name="Show.S01E01",
            queue_messages=[],
            candidate_media={},
            library_candidates=[],
            manual_import_files=[],
            existing_disk_files=[],
        )
        mock_fallback.evaluate_import_decision.assert_called_once()
        self.assertEqual(res_daily.decision, "import")
        self.assertEqual(res_daily.confidence, 0.92)

    async def test_gemini_rotates_model_on_timeout_and_records_intermediate_metrics(self):
        """
        Verify that when a model times out, Gemini rotates to the next candidate model,
        and accurately records the timeout attempt in metrics with model name, duration, and TIMEOUT code.
        """
        prov = GeminiProvider(api_key="fake-key", model="gemini-3.6-flash")
        prov._min_call_interval = 0
        prov._discovered_candidates = ["gemini-3.6-flash", "gemini-3.5-flash"]

        calls = []

        class Mock200Resp:
            status_code = 200
            reason_phrase = "OK"
            request = MagicMock()

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "candidates": [{
                        "content": {
                            "parts": [{"text": '{"decision": "import", "confidence": 0.95, "reasoning": "Model 2 succeeded", "files_to_import": []}'}]
                        }
                    }]
                }

        async def mock_post(client, url, json=None):
            model_name = url.split("/models/")[1].split(":")[0]
            calls.append(model_name)
            if model_name == "gemini-3.6-flash":
                raise httpx.TimeoutException("Read timed out after 25.0s")
            return Mock200Resp()

        with patch("httpx.AsyncClient.post", new=mock_post), patch("src.ai.gemini.metrics.record_token_usage") as mock_record:
            data, raw, think = await prov._call_gemini_with_raw(
                prompt="test prompt",
                schema_desc="test schema",
                operation="test_timeout_rotate"
            )

            # Both models attempted
            self.assertEqual(calls, ["gemini-3.6-flash", "gemini-3.5-flash"])
            self.assertEqual(data["decision"], "import")

            # Verify metrics logged both the failure and the rotated success
            self.assertEqual(mock_record.call_count, 2)
            # Call 1: gemini-3.6-flash timeout
            call1_kwargs = mock_record.call_args_list[0].kwargs
            self.assertEqual(call1_kwargs["model"], "gemini-3.6-flash")
            self.assertFalse(call1_kwargs["success"])
            self.assertEqual(call1_kwargs["error_code"], "TIMEOUT")

            # Call 2: gemini-3.5-flash success
            call2_kwargs = mock_record.call_args_list[1].kwargs
            self.assertEqual(call2_kwargs["model"], "gemini-3.5-flash")
            self.assertTrue(call2_kwargs["success"])

            # gemini-3.6-flash placed in cooldown
            self.assertIn("gemini-3.6-flash", prov._rate_limited_models)

    async def test_gemini_all_models_timeout_records_each_attempt_accurately(self):
        """
        Verify that when all candidate models time out across passes, every single attempt
        is recorded in token metrics with its actual model name and TIMEOUT error code,
        and no duplicate phantom summary record is created.
        """
        prov = GeminiProvider(api_key="fake-key", model="gemini-model-a")
        prov._min_call_interval = 0
        prov._discovered_candidates = ["gemini-model-a", "gemini-model-b"]

        async def mock_post(client, url, json=None):
            raise httpx.TimeoutException("Gateway timed out after 25.0s")

        with patch("httpx.AsyncClient.post", new=mock_post), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("src.ai.gemini.metrics.record_token_usage") as mock_record:

            with self.assertRaises(AIProviderError):
                await prov._call_gemini_with_raw(
                    prompt="test prompt",
                    schema_desc="test schema",
                    operation="test_all_timeout"
                )

            # All attempts (2 in Pass 1, 2 in Pass 2 = 4 total attempts)
            self.assertEqual(mock_record.call_count, 4)
            for call in mock_record.call_args_list:
                kw = call.kwargs
                self.assertIn(kw["model"], ["gemini-model-a", "gemini-model-b"])
                self.assertFalse(kw["success"])
                self.assertEqual(kw["error_code"], "TIMEOUT")

    async def test_gemini_auto_discovery_fallback_when_catalog_api_fails(self):
        """
        Verify that if Google catalog API discovery fails, get_candidate_models returns
        the user preferred model plus the fallback candidate models rather than just 1 or 0 models.
        """
        prov = GeminiProvider(api_key="fake-key", model="gemini-user-pref")

        async def mock_get(client, url):
            raise httpx.ConnectTimeout("Connection timed out after 8.0s")

        with patch("httpx.AsyncClient.get", new=mock_get):
            candidates = await prov.get_candidate_models()
            self.assertIn("gemini-user-pref", candidates)
            self.assertIn("gemini-2.5-flash", candidates)
            self.assertIn("gemini-2.0-flash", candidates)
            self.assertIn("gemini-1.5-flash", candidates)
            self.assertGreaterEqual(len(candidates), 4)

    async def test_gemini_uses_previously_discovered_catalog_on_timeout_or_error(self):
        """
        Verify that if a model catalog was previously discovered, and subsequent catalog
        queries fail or time out, GeminiProvider automatically returns the previously discovered catalog.
        """
        prov = GeminiProvider(api_key="fake-key", model="gemini-cached-pref")

        class MockCatalogResp:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return {
                    "models": [
                        {"name": "models/gemini-discovered-flash", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/gemini-discovered-pro", "supportedGenerationMethods": ["generateContent"]},
                    ]
                }

        # 1. Successful initial discovery
        async def mock_get_success(client, url):
            return MockCatalogResp()

        with patch("httpx.AsyncClient.get", new=mock_get_success):
            initial = await prov.get_candidate_models()
            self.assertIn("gemini-discovered-flash", initial)
            self.assertIn("gemini-discovered-pro", initial)

        # 2. Subsequent call times out (with force_refresh=True)
        async def mock_get_timeout(client, url):
            raise httpx.TimeoutException("Catalog discovery timed out after 8.0s")

        with patch("httpx.AsyncClient.get", new=mock_get_timeout):
            recovered = await prov.get_candidate_models(force_refresh=True)
            # Must return previously discovered catalog
            self.assertEqual(recovered, initial)
            self.assertIn("gemini-discovered-flash", recovered)

    async def test_gemini_persistent_catalog_cache_in_db(self):
        """
        Verify that discovered model catalog is persisted to DatabaseManager metadata
        and seamlessly reloaded upon re-instantiation without requiring any network calls.
        """
        mock_db = MagicMock()
        cached_data = json.dumps(["gemini-saved-1.0", "gemini-saved-2.0"])
        mock_db.get_metadata.side_effect = lambda k: cached_data if k == "gemini_model_catalog" else "12345678.0"

        with patch("src.ai.gemini.metrics.db", new=mock_db):
            prov = GeminiProvider(api_key="fake-key", model="auto")
            # Should have loaded from DB
            self.assertEqual(prov._discovered_candidates, ["gemini-saved-1.0", "gemini-saved-2.0"])
            self.assertEqual(prov._candidates_last_fetched, 12345678.0)

            # When calling get_candidate_models with API failure, it returns the DB cached catalog
            async def mock_get_fail(client, url):
                raise httpx.ConnectError("Network is down")

            with patch("httpx.AsyncClient.get", new=mock_get_fail):
                candidates = await prov.get_candidate_models(force_refresh=True)
                self.assertEqual(candidates, ["gemini-saved-1.0", "gemini-saved-2.0"])

    def test_config_and_web_ui_clean(self):
        """Verify fallback_on_daily_quota_only is completely removed from config and Web UI."""
        cfg = AIConfig()
        self.assertFalse(hasattr(cfg, "fallback_on_daily_quota_only"))

        d = config_to_dict(AppConfig(ai=cfg))
        self.assertNotIn("fallback_on_daily_quota_only", d["ai"])

        self.assertNotIn("cfg-ai-fallback-daily-only", RENDERED_DASHBOARD_HTML)
        self.assertNotIn("Fallback Only on Daily Quota Exhaustion", RENDERED_DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
