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
            # Extract model from URL
            # https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=...
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

    async def test_gemini_daily_quota_exhaustion_raises_special_error(self):
        """
        Verify that when a 429 response indicates daily quota exhaustion (PerDay),
        DailyQuotaExhaustedError is raised and provider state is updated.
        """
        prov = GeminiProvider(api_key="fake-key", model="gemini-3.6-flash")
        prov._min_call_interval = 0

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

    async def test_fallback_provider_daily_quota_only_policy(self):
        """
        Verify FallbackProvider:
        - When fallback_on_daily_quota_only=True:
            - Transient AIProviderError (timeout/rate limit): does NOT invoke fallback; returns safe default (confidence=0.0).
            - DailyQuotaExhaustedError: DOES invoke fallback.
        - When fallback_on_daily_quota_only=False:
            - Transient AIProviderError: DOES invoke fallback.
        """
        mock_primary = MagicMock()
        mock_fallback = MagicMock()
        mock_primary.model = "gemini-3.6-flash"
        mock_primary.name = "Gemini (gemini-3.6-flash)"
        mock_fallback.model = "qwen2.5:14b"
        mock_fallback.name = "Ollama (qwen2.5:14b)"
        mock_primary.is_daily_quota_exhausted.return_value = False

        fb_result = Stage2DecisionResult(
            decision="import",
            confidence=0.92,
            reasoning="Fallback Ollama decision",
            files_to_import=[]
        )
        mock_fallback.evaluate_import_decision = AsyncMock(return_value=fb_result)

        # 1. Policy: fallback_on_daily_quota_only = True
        fb_provider = FallbackProvider(
            primary=mock_primary,
            fallback=mock_fallback,
            fallback_on_daily_quota_only=True
        )

        # Case A: Transient error (e.g. timeout)
        mock_primary.evaluate_import_decision = AsyncMock(side_effect=AIProviderError("Gemini API request timed out after 60s"))
        res_transient = await fb_provider.evaluate_import_decision(
            media_type="series",
            release_title="Show.S01E01",
            folder_name="Show.S01E01",
            queue_messages=[],
            candidate_media={},
            library_candidates=[],
            manual_import_files=[],
            existing_disk_files=[],
        )

        # Fallback should NOT have been invoked
        mock_fallback.evaluate_import_decision.assert_not_called()
        # Safe 0.0 confidence returned
        self.assertEqual(res_transient.decision, "reject_keep_in_client")
        self.assertEqual(res_transient.confidence, 0.0)
        self.assertIn("daily quota not exhausted", res_transient.reasoning)

        # Case B: Daily Quota Exhausted error
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

        # Fallback SHOULD have been invoked
        mock_fallback.evaluate_import_decision.assert_called_once()
        self.assertEqual(res_daily.decision, "import")
        self.assertEqual(res_daily.confidence, 0.92)

        # 2. Policy: fallback_on_daily_quota_only = False
        mock_fallback.evaluate_import_decision.reset_mock()
        fb_provider_legacy = FallbackProvider(
            primary=mock_primary,
            fallback=mock_fallback,
            fallback_on_daily_quota_only=False
        )

        # Transient error triggers fallback when flag is False
        mock_primary.evaluate_import_decision = AsyncMock(side_effect=AIProviderError("Transient error"))
        res_legacy = await fb_provider_legacy.evaluate_import_decision(
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
        self.assertEqual(res_legacy.decision, "import")

    def test_config_and_web_ui_integration(self):
        """Verify configuration round-trip and presence of Web UI toggle."""
        # 1. Config defaults
        cfg = AIConfig()
        self.assertTrue(cfg.fallback_on_daily_quota_only)

        # 2. Serialization & update
        d = config_to_dict(AppConfig(ai=cfg))
        self.assertIn("fallback_on_daily_quota_only", d["ai"])
        self.assertTrue(d["ai"]["fallback_on_daily_quota_only"])

        d["ai"]["fallback_on_daily_quota_only"] = False
        app_cfg = AppConfig()
        update_config_from_dict(app_cfg, d)
        self.assertFalse(app_cfg.ai.fallback_on_daily_quota_only)

        # 3. Web UI HTML toggle
        self.assertIn("cfg-ai-fallback-daily-only", RENDERED_DASHBOARD_HTML)
        self.assertIn("Fallback Only on Daily Quota Exhaustion", RENDERED_DASHBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
