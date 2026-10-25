import json
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure dependencies mocked if not installed in local environment
try:
    import pydantic
except ImportError:
    pyd = MagicMock()
    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in list(getattr(self.__class__, "__dict__", {}).items()):
                if not k.startswith("_") and not callable(v):
                    setattr(self, k, v)
            for k, v in kwargs.items():
                setattr(self, k, v)
    def Field(*args, **kwargs):
        if "default_factory" in kwargs:
            return kwargs["default_factory"]()
        if "default" in kwargs:
            return kwargs["default"]
        return args[0] if args else None
    pyd.BaseModel = BaseModel
    pyd.Field = Field
    sys.modules["pydantic"] = pyd

try:
    import httpx
except ImportError:
    sys.modules["httpx"] = MagicMock()

from src.config import OllamaConfig
from src.ai.ollama import OllamaProvider
from src.ai.gemini import GeminiProvider


class OllamaChunkingAndReasoningTests(unittest.IsolatedAsyncioTestCase):
    def test_default_models(self):
        """Verify Ollama default model upgraded to qwen2.5:7b across config and provider."""
        cfg = OllamaConfig()
        self.assertEqual(cfg.model, "qwen2.5:7b")

        provider = OllamaProvider()
        self.assertEqual(provider.model, "qwen2.5:7b")
        self.assertEqual(provider.name, "Ollama (qwen2.5:7b)")

    async def test_season_pack_chunking_short_circuit(self):
        """
        Verify that season pack backfill splits candidates into chunks of 5
        and short-circuits on high-confidence match (>=0.85) in the first chunk.
        """
        provider = OllamaProvider(base_url="http://localhost:11434", model="qwen2.5:7b")

        # Create 12 candidate releases
        candidates = [
            {"guid": f"guid-{i}", "title": f"Series S01 Rel {i}", "quality": "1080p", "seeders": 10 - i}
            for i in range(12)
        ]

        # First chunk matches guid-0 with 0.95 confidence
        mock_resp = {
            "reasoning": "Valid S01 full pack with high quality and seeders.",
            "matched_guid": "guid-0",
            "confidence": 0.95,
        }

        with patch.object(
            provider,
            "_call_ollama_with_raw",
            new_callable=AsyncMock,
            return_value=(mock_resp, json.dumps(mock_resp), None),
        ) as mock_call:
            res = await provider.evaluate_backfill_season_pack(
                series_info={"id": 1, "title": "Test Show", "year": 2024},
                season_number=1,
                candidate_releases=candidates,
            )
            # Only 1 call made because chunk 0 matched with >= 0.85 confidence
            self.assertEqual(mock_call.call_count, 1)
            self.assertEqual(res.matched_guid, "guid-0")
            self.assertEqual(res.confidence, 0.95)

            # Check that prompt only received the first 5 candidates
            user_prompt = mock_call.call_args[0][1]
            self.assertIn("guid-0", user_prompt)
            self.assertIn("guid-4", user_prompt)
            self.assertNotIn("guid-5", user_prompt)

    async def test_season_pack_chunking_continuation_to_chunk_2(self):
        """
        Verify that if chunk 0 returns null/unmatched, it evaluates chunk 1.
        """
        provider = OllamaProvider(base_url="http://localhost:11434", model="qwen2.5:7b")

        candidates = [
            {"guid": f"guid-{i}", "title": f"Series S01 Rel {i}", "quality": "1080p", "seeders": 10 - i}
            for i in range(10)
        ]

        # Chunk 0 returns null; Chunk 1 returns match guid-6
        chunk0_resp = {
            "reasoning": "No clean S01 pack in this batch.",
            "matched_guid": None,
            "confidence": 0.0,
        }
        chunk1_resp = {
            "reasoning": "Found clean S01 pack.",
            "matched_guid": "guid-6",
            "confidence": 0.92,
        }

        with patch.object(
            provider,
            "_call_ollama_with_raw",
            new_callable=AsyncMock,
            side_effect=[
                (chunk0_resp, json.dumps(chunk0_resp), None),
                (chunk1_resp, json.dumps(chunk1_resp), None),
            ],
        ) as mock_call:
            res = await provider.evaluate_backfill_season_pack(
                series_info={"id": 1, "title": "Test Show", "year": 2024},
                season_number=1,
                candidate_releases=candidates,
            )
            self.assertEqual(mock_call.call_count, 2)
            self.assertEqual(res.matched_guid, "guid-6")
            self.assertEqual(res.confidence, 0.92)

    async def test_bulk_episodes_chunking_across_episodes(self):
        """
        Verify that bulk episode retry splits missing episodes into batches of 4
        and merges all episode match results cleanly.
        """
        provider = OllamaProvider(base_url="http://localhost:11434", model="qwen2.5:7b")

        # 6 missing episodes (E01 to E06) -> 2 batches (4 and 2)
        missing_eps = [
            {"episodeNumber": i, "title": f"Episode {i}"}
            for i in range(1, 7)
        ]
        candidates = [
            {"guid": f"rel-{i}", "title": f"Test Show S01E0{i}", "seeders": 5}
            for i in range(1, 7)
        ]

        batch1_resp = {
            "matches": [
                {"episode_number": 1, "reasoning": "Match Ep 1", "matched_guid": "rel-1", "confidence": 0.9},
                {"episode_number": 2, "reasoning": "Match Ep 2", "matched_guid": "rel-2", "confidence": 0.9},
                {"episode_number": 3, "reasoning": "Match Ep 3", "matched_guid": "rel-3", "confidence": 0.9},
                {"episode_number": 4, "reasoning": "Match Ep 4", "matched_guid": "rel-4", "confidence": 0.9},
            ]
        }
        batch2_resp = {
            "matches": [
                {"episode_number": 5, "reasoning": "Match Ep 5", "matched_guid": "rel-5", "confidence": 0.9},
                {"episode_number": 6, "reasoning": "Match Ep 6", "matched_guid": "rel-6", "confidence": 0.9},
            ]
        }

        with patch.object(
            provider,
            "_call_ollama_with_raw",
            new_callable=AsyncMock,
            side_effect=[
                (batch1_resp, json.dumps(batch1_resp), None),
                (batch2_resp, json.dumps(batch2_resp), None),
            ],
        ) as mock_call:
            res = await provider.evaluate_backfill_episodes_bulk(
                series_info={"id": 1, "title": "Test Show", "year": 2024},
                season_number=1,
                missing_episodes=missing_eps,
                candidate_releases=candidates,
            )
            # 2 calls made for 2 batches of episodes
            self.assertEqual(mock_call.call_count, 2)
            self.assertEqual(len(res.matches), 6)
            matched_guids = [m.matched_guid for m in res.matches]
            self.assertEqual(matched_guids, ["rel-1", "rel-2", "rel-3", "rel-4", "rel-5", "rel-6"])

    async def test_reasoning_first_prompt_format(self):
        """
        Verify that Ollama prompt schema specifies reasoning before matched_guid.
        """
        provider = OllamaProvider()

        with patch.object(
            provider,
            "_call_ollama_with_raw",
            new_callable=AsyncMock,
            return_value=({"reasoning": "ok", "matched_guid": None, "confidence": 0.0}, "{}", None),
        ) as mock_call:
            await provider.evaluate_backfill_season_pack(
                series_info={"id": 1, "title": "Test Show", "year": 2024},
                season_number=1,
                candidate_releases=[{"guid": "g1", "title": "Show S01", "seeders": 1}],
            )
            sys_prompt = mock_call.call_args[0][0]
            usr_prompt = mock_call.call_args[0][1]

            # In system prompt, keys should list reasoning before matched_guid
            self.assertIn("'reasoning'", sys_prompt)
            # In user prompt schema, reasoning should appear before matched_guid
            reasoning_idx = usr_prompt.find('"reasoning"')
            matched_idx = usr_prompt.find('"matched_guid"')
            self.assertNotEqual(reasoning_idx, -1)
            self.assertNotEqual(matched_idx, -1)
            self.assertLess(reasoning_idx, matched_idx, "reasoning must appear before matched_guid in prompt schema")
