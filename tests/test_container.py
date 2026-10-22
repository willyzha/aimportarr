import unittest
import shutil
import subprocess
import tempfile
from pathlib import Path

class ContainerSmokeTests(unittest.TestCase):
    """
    Smoke tests designed to run inside the Docker container (or local test environment)
    to verify all modules, models, configuration parsing, and binaries work without errors.
    """

    def test_imports(self):
        """Verify all application modules import without NameError, SyntaxError, or ImportError."""
        try:
            from src.config import (
                load_config,
                AppConfig,
                PathMapping,
                GeneralConfig,
                AIConfig,
                CategoriesConfig,
                ExtractionConfig,
                WebConfig,
                DatabaseConfig,
            )
            from src.database import DatabaseManager
            from src.extractor import inspect_archive, extract_archive, find_archive_file, resolve_local_path
            from src.classifier import classify_issue, ErrorCategory
            from src.sonarr import SonarrClient
            from src.radarr import RadarrClient
            from src.main import main
            from src.metrics import MetricsTracker, metrics
            from src.web import WebServerRunner, DASHBOARD_HTML
            from src.ai.base import (
                BaseAIProvider,
                MediaMatchResult,
                EpisodeMatchResult,
                SampleEvaluationResult,
                LibraryMatchResult,
                MalwareEvaluationResult,
            )
            from src.ai.gemini import GeminiProvider
            from src.ai.ollama import OllamaProvider
            from src.ai.fallback import FallbackProvider
        except Exception as e:
            self.fail(f"Module import failed: {e}")

    def test_example_config_loading(self):
        """Verify that config.example.yml parses and passes all Pydantic validations."""
        from src.config import load_config
        example_path = Path("config.example.yml")
        if not example_path.exists():
            example_path = Path("/app/config.example.yml")

        if example_path.exists():
            config = load_config(str(example_path))
            self.assertIsNotNone(config)
            self.assertTrue(config.categories.is_enabled("fake_or_malware"))
            self.assertTrue(config.categories.is_enabled("id_match"))
            self.assertTrue(config.extraction.enabled)
            self.assertTrue(config.web.enabled)
            self.assertEqual(config.web.port, 9890)
            self.assertEqual(config.database.retention_days, 30)
            self.assertEqual(config.database.path, "/config/aimportarr.db")

    def test_pydantic_model_definitions(self):
        """Verify all Pydantic models instantiate properly with default factories."""
        from src.config import AppConfig, PathMapping, WebConfig, DatabaseConfig
        config = AppConfig()
        self.assertIsNotNone(config.general)
        self.assertIsNotNone(config.ai)
        self.assertIsNotNone(config.sonarr)
        self.assertIsNotNone(config.radarr)
        self.assertIsNotNone(config.categories)
        self.assertIsNotNone(config.extraction)
        self.assertIsNotNone(config.web)
        self.assertIsNotNone(config.database)
        self.assertEqual(config.web.port, 9890)
        self.assertTrue(config.web.enabled)
        self.assertEqual(config.database.retention_days, 30)
        self.assertEqual(config.database.path, "/config/aimportarr.db")
        self.assertIsInstance(config.path_mappings, list)

        mapping = PathMapping(**{"from": "/remote/path", "to": "/local/path"})
        self.assertEqual(mapping.from_path, "/remote/path")
        self.assertEqual(mapping.to_path, "/local/path")

    def test_7z_binary_present(self):
        """Verify 7z or 7za is available and responds to --help."""
        seven_z = shutil.which("7z") or shutil.which("7za")
        self.assertIsNotNone(seven_z, "7z or 7za utility must be installed in container PATH")
        proc = subprocess.run([seven_z, "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("7-Zip", proc.stdout)

    def test_archive_inspection_logic(self):
        """Create a temporary mock archive and verify that inspect_archive parses it accurately."""
        import asyncio
        from src.extractor import inspect_archive

        seven_z = shutil.which("7z") or shutil.which("7za")
        if not seven_z:
            self.skipTest("7z not installed")

        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            fake_exe = p / "vlc-win64_crypted.exe"
            fake_exe.write_text("dummy binary content")
            readme = p / "readme.txt"
            readme.write_text("instructions")
            arch = p / "test.7z"

            subprocess.run([seven_z, "a", str(arch), str(fake_exe), str(readme)], capture_output=True, check=True)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(inspect_archive(arch))
            loop.close()

            self.assertIsNotNone(result)
            self.assertTrue(result.has_suspicious_files)
            self.assertFalse(result.has_video_files)
            self.assertEqual(len(result.suspicious_files), 1)
            self.assertIn("vlc-win64_crypted.exe", result.suspicious_files[0].path)

    def test_metrics_tracker(self):
        """Verify MetricsTracker correctly aggregates counters, breakdowns, and bounded histories."""
        from src.metrics import MetricsTracker
        tracker = MetricsTracker(max_history=5)

        # Scans & archive extractions
        tracker.record_scan()
        tracker.record_scan()
        tracker.record_archive_extracted()

        # Processed items
        tracker.record_processed_item(
            app="Sonarr",
            title="Show.S01E01",
            category="id_match",
            status="imported",
            confidence=0.98,
            model="gemini-flash",
            reasoning="Matched official title",
            details="Imported 1 file"
        )
        tracker.record_processed_item(
            app="Radarr",
            title="Movie.2024",
            category="fake_or_malware",
            status="blocked",
            confidence=0.99,
            model="gemini-flash",
            reasoning="Executable disguised as movie",
            details="Deleted and blocklisted"
        )

        # AI token usage
        tracker.record_token_usage(
            operation="verify_series_match",
            model="gemini-flash",
            prompt_tokens=450,
            thinking_tokens=300,
            completion_tokens=50,
            total_tokens=800,
            duration_ms=320,
            item_title="Show.S01E01",
            success=True
        )

        summary = tracker.get_summary()
        self.assertEqual(summary["total_scans"], 2)
        self.assertEqual(summary["total_archives_extracted"], 1)
        self.assertEqual(summary["total_items_processed"], 2)
        self.assertEqual(summary["total_imported"], 1)
        self.assertEqual(summary["total_blocked"], 1)
        self.assertEqual(summary["total_ai_calls"], 1)
        self.assertEqual(summary["total_prompt_tokens"], 450)
        self.assertEqual(summary["total_thinking_tokens"], 300)
        self.assertEqual(summary["total_completion_tokens"], 50)
        self.assertEqual(summary["total_tokens"], 800)
        self.assertIn("gemini-flash", summary["tokens_by_model"])
        self.assertEqual(summary["tokens_by_model"]["gemini-flash"]["thinking_tokens"], 300)

        history = tracker.get_processed_history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["title"], "Movie.2024") # Newest first

        tokens = tracker.get_token_history()
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0]["total_tokens"], 800)
        self.assertEqual(tokens[0]["thinking_tokens"], 300)

    def test_web_server_endpoints(self):
        """Verify WebServerRunner serves dashboard HTML, metrics API, history API, and health API."""
        import time
        import urllib.request
        import json
        from src.metrics import MetricsTracker
        from src.web import WebServerRunner

        test_tracker = MetricsTracker()
        test_tracker.record_scan()
        test_tracker.record_processed_item(
            app="Sonarr",
            title="Test.Release.S01E01",
            category="id_match",
            status="imported",
            confidence=0.95
        )

        # Choose a high test port to avoid conflict
        port = 18992
        server = WebServerRunner(host="127.0.0.1", port=port, metrics_instance=test_tracker)
        server.start()

        try:
            time.sleep(0.1) # Wait for server to bind

            # 1. Dashboard UI
            req = urllib.request.Request(f"http://127.0.0.1:{port}/")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                html = resp.read().decode("utf-8")
                self.assertIn("AImportarr", html)
                self.assertIn("Processed History", html)
                self.assertIn("Token Usage History", html)

            # 2. REST API: Summary / Metrics
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/metrics")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["total_scans"], 1)
                self.assertEqual(data["total_imported"], 1)

            # 3. REST API: Processed History
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/history")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                items = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["title"], "Test.Release.S01E01")

            # 4. REST API: Health
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                health = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(health["status"], "ok")

            # 5. REST API: POST /api/retry for single item
            test_tracker.record_processed_item(
                app="Radarr",
                title="Failed.Movie.2024",
                category="unknown_media",
                status="rejected",
                details="Could not match"
            )
            hist = test_tracker.get_processed_history()
            failed_id = hist[0]["id"]

            post_data = json.dumps({"id": failed_id}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/retry",
                data=post_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                res = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(res["status"], "ok")
                self.assertTrue(res["success"])

            # 6. REST API: POST /api/retry for all items
            post_data = json.dumps({"all": True}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/retry",
                data=post_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                res = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(res["status"], "ok")
                self.assertIn("count", res)

            # 7. Official Logo Asset
            req = urllib.request.Request(f"http://127.0.0.1:{port}/assets/logo.png")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type", ""), "image/png")

        finally:
            server.stop()

    def test_database_persistence_and_retention(self):
        """Verify DatabaseManager persistent storage, key indexing, retry toggles, and retention purge."""
        from datetime import datetime, timedelta, timezone
        from src.database import DatabaseManager

        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "test.db"
            db = DatabaseManager(db_path=str(db_file), retention_days=30)

            # Initially not attempted
            item_keys = ["dl:abc123", "path:/data/downloads/Test.Show.S01E01", "item:12:Test.Show"]
            self.assertFalse(db.is_item_attempted(item_keys))

            # Record item as rejected
            item_id = db.record_processed_item(
                app="Sonarr",
                title="Test.Show.S01E01",
                category="id_match",
                status="rejected",
                confidence=0.45,
                model="gemini-flash",
                reasoning="Title does not match series",
                details="Rejected by AI",
                item_keys=item_keys
            )
            self.assertGreater(item_id, 0)

            # Now is_item_attempted should be True (retry not requested)
            self.assertTrue(db.is_item_attempted(item_keys))
            self.assertTrue(db.is_item_attempted(["dl:abc123"]))

            # Mark for retry
            ok = db.mark_item_for_retry(item_id)
            self.assertTrue(ok)

            # Now is_item_attempted should be False (allows re-scan!)
            self.assertFalse(db.is_item_attempted(item_keys))

            # Record again (simulating re-evaluation success)
            new_id = db.record_processed_item(
                app="Sonarr",
                title="Test.Show.S01E01",
                category="id_match",
                status="imported",
                confidence=0.99,
                model="gemini-flash",
                reasoning="Confirmed match",
                details="Imported 1 file",
                item_keys=item_keys
            )
            # Re-recording clears retry_requested, so it is once again marked as attempted
            self.assertTrue(db.is_item_attempted(item_keys))

            # Test mark_all_rejected_for_retry
            rej_id1 = db.record_processed_item(
                app="Radarr",
                title="Movie.Rejected.1",
                category="fake_or_malware",
                status="blocked",
                item_keys=["dl:rej1"]
            )
            rej_id2 = db.record_processed_item(
                app="Radarr",
                title="Movie.Rejected.2",
                category="unknown_media",
                status="rejected",
                item_keys=["dl:rej2"]
            )
            self.assertTrue(db.is_item_attempted(["dl:rej1"]))
            self.assertTrue(db.is_item_attempted(["dl:rej2"]))

            count = db.mark_all_rejected_for_retry()
            self.assertEqual(count, 3) # item_id (rejected) + rej_id1 (blocked) + rej_id2 (rejected)
            self.assertFalse(db.is_item_attempted(["dl:rej1"]))
            self.assertFalse(db.is_item_attempted(["dl:rej2"]))

            # Test token recording
            tok_id = db.record_token_usage(
                operation="test_op",
                model="gemini-flash",
                prompt_tokens=100,
                thinking_tokens=50,
                completion_tokens=20,
                total_tokens=170,
                duration_ms=150,
                item_title="Movie.Rejected.1",
                success=True
            )
            self.assertGreater(tok_id, 0)

            stats = db.get_summary_stats()
            self.assertGreaterEqual(stats["total_items_processed"], 3)
            self.assertEqual(stats["total_tokens"], 170)
            self.assertEqual(stats["total_thinking_tokens"], 50)
            self.assertEqual(stats["tokens_by_model"]["gemini-flash"]["thinking_tokens"], 50)

            history = db.get_token_history(limit=5)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["thinking_tokens"], 50)

            # Test retention purge (insert old records directly with timestamp 40 days ago)
            old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
            conn = db._get_connection()
            with conn:
                conn.execute(
                    "INSERT INTO processed_items (app, title, category, status, timestamp) VALUES (?, ?, ?, ?, ?)",
                    ("Sonarr", "Ancient.Item", "other", "rejected", old_time)
                )
                conn.execute(
                    "INSERT INTO token_usage (timestamp, operation, model, total_tokens) VALUES (?, ?, ?, ?)",
                    (old_time, "old_call", "gemini-flash", 50)
                )

            purged = db.purge_old_records(retention_days=30)
            self.assertGreaterEqual(purged, 2)

            db.close()

if __name__ == "__main__":
    unittest.main()
