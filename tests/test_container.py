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
                save_config,
                config_to_dict,
                update_config_from_dict,
                AppConfig,
                PathMapping,
                GeneralConfig,
                AIConfig,
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
                Stage1TriageResult,
                ApprovedFileItem,
                Stage2DecisionResult,
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
            self.assertFalse(config.general.dry_run)
            self.assertFalse(config.general.reject_on_low_confidence)
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
        self.assertIsNotNone(config.extraction)
        self.assertIsNotNone(config.web)
        self.assertIsNotNone(config.database)
        self.assertFalse(config.general.dry_run)
        self.assertFalse(config.general.reject_on_low_confidence)
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

    def test_path_mappings_and_autodiscovery(self):
        """Verify resolve_local_path handles path_mappings and returns original path if not found."""
        from src.extractor import resolve_local_path

        with tempfile.TemporaryDirectory() as td:
            local_target = Path(td) / "local_downloads"
            local_target.mkdir()
            sample_file = local_target / "release.mkv"
            sample_file.touch()

            # Test path mapping translation
            mappings = [{"from": "/remote/downloads", "to": str(local_target)}]
            resolved = resolve_local_path(f"/remote/downloads/release.mkv", mappings)
            self.assertEqual(resolved, sample_file)

            # Test non-existent path fallback
            non_existent = resolve_local_path("/non/existent/path/nowhere.mkv")
            self.assertEqual(non_existent, Path("/non/existent/path/nowhere.mkv"))

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
            status="rejected",
            confidence=0.99,
            model="gemini-flash",
            reasoning="Executable disguised as movie",
            details="Deleted and blocklisted"
        )

        # AI token usage
        tracker.record_token_usage(
            operation="evaluate_import_decision",
            model="gemini-flash",
            prompt_tokens=450,
            thinking_tokens=300,
            completion_tokens=50,
            total_tokens=800,
            duration_ms=320,
            item_title="Show.S01E01",
            success=True,
            prompt_text="Evaluate Show.S01E01",
            response_text='{"decision": "IMPORT"}',
        )

        summary = tracker.get_summary()
        self.assertEqual(summary["total_scans"], 2)
        self.assertEqual(summary["total_archives_extracted"], 1)
        self.assertEqual(summary["total_items_processed"], 2)
        self.assertEqual(summary["total_imported"], 1)
        self.assertEqual(summary["total_rejected"], 1)
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
        self.assertEqual(tokens[0]["prompt_text"], "Evaluate Show.S01E01")
        self.assertEqual(tokens[0]["response_text"], '{"decision": "IMPORT"}')

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
        with tempfile.TemporaryDirectory() as td:
            test_cfg_path = str(Path(td) / "test_config.yml")
            port = 18992
            triggered_sources = []
            server = WebServerRunner(
                host="127.0.0.1",
                port=port,
                metrics_instance=test_tracker,
                on_trigger_scan=lambda src: triggered_sources.append(src),
                config_path=test_cfg_path,
            )
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
                    self.assertIn("Settings", html)

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

                # 5. REST API: GET & POST /api/config
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/config")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)
                    cfg_data = json.loads(resp.read().decode("utf-8"))
                    self.assertIn("general", cfg_data)
                    self.assertIn("reject_on_low_confidence", cfg_data["general"])

                cfg_update = {
                    "general": {
                        "dry_run": True,
                        "reject_on_low_confidence": True,
                        "min_confidence": 0.9,
                    }
                }
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/config",
                    data=json.dumps(cfg_update).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)
                    res_cfg = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(res_cfg["status"], "ok")
                    self.assertTrue(res_cfg["config"]["general"]["dry_run"])
                    self.assertTrue(res_cfg["config"]["general"]["reject_on_low_confidence"])
                    self.assertEqual(res_cfg["config"]["general"]["min_confidence"], 0.9)

                # 6. REST API: POST /api/retry for single retryable item (skipped/failed/dry_run)
                test_tracker.record_processed_item(
                    app="Radarr",
                    title="Skipped.Movie.2024",
                    category="low_confidence",
                    status="skipped",
                    details="Left in queue"
                )
                hist = test_tracker.get_processed_history()
                skipped_id = hist[0]["id"]

                post_data = json.dumps({"id": skipped_id}).encode("utf-8")
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

                # Verify that a rejected (deleted) item CANNOT be retried
                test_tracker.record_processed_item(
                    app="Radarr",
                    title="Deleted.Malware.2024",
                    category="malware_or_corrupt",
                    status="rejected",
                    details="Deleted from client & blocklisted"
                )
                rejected_id = test_tracker.get_processed_history()[0]["id"]
                self.assertFalse(test_tracker.mark_item_for_retry(rejected_id))

                # 7. REST API: POST /api/retry for all items
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

                # 7b. REST API: POST /api/scan, POST /api/backfill, and POST /api/webhook
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/scan", method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)
                    res = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(res["status"], "ok")

                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/backfill", method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)
                    res = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(res["status"], "ok")

                wh_test = json.dumps({"eventType": "Test"}).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/webhook",
                    data=wh_test,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)

                wh_live = json.dumps({"eventType": "ManualInteractionRequired"}).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/webhook/sonarr",
                    data=wh_live,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200)

                self.assertIn(f"retry:{skipped_id}", triggered_sources)
                self.assertIn("manual", triggered_sources)
                self.assertIn("backfill", triggered_sources)
                self.assertIn("webhook:ManualInteractionRequired", triggered_sources)

                # 8. Official Logo Asset
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

            # Record item as skipped (still in queue, eligible for retry)
            item_id = db.record_processed_item(
                app="Sonarr",
                title="Test.Show.S01E01",
                category="low_confidence",
                status="skipped",
                confidence=0.45,
                model="gemini-flash",
                reasoning="Low confidence",
                details="Left in queue",
                item_keys=item_keys
            )
            self.assertGreater(item_id, 0)

            # Now is_item_attempted should be True (retry not requested)
            self.assertTrue(db.is_item_attempted(item_keys))
            self.assertTrue(db.is_item_attempted(["dl:abc123"]))
            self.assertTrue(db.is_item_attempted_within_days(["dl:abc123"], days=7))

            # Mark for retry
            ok = db.mark_item_for_retry(item_id)
            self.assertTrue(ok)

            # Now is_item_attempted should be False (allows re-scan!)
            self.assertFalse(db.is_item_attempted(item_keys))
            self.assertFalse(db.is_item_attempted_within_days(["dl:abc123"], days=7))

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
            # Imported item cannot be marked for retry
            self.assertFalse(db.mark_item_for_retry(new_id))

            # Test mark_all_rejected_for_retry only marks retryable (skipped/failed/dry_run) items, not rejected/dismissed
            del_id = db.record_processed_item(
                app="Radarr",
                title="Movie.Deleted.1",
                category="malware_or_corrupt",
                status="rejected",
                item_keys=["dl:del1"]
            )
            dis_id = db.record_processed_item(
                app="Radarr",
                title="Movie.Dismissed.1",
                category="quality_downgrade",
                status="dismissed",
                item_keys=["dl:dis1"]
            )
            skip_id1 = db.record_processed_item(
                app="Radarr",
                title="Movie.Skipped.1",
                category="low_confidence",
                status="skipped",
                item_keys=["dl:skip1"]
            )
            fail_id2 = db.record_processed_item(
                app="Radarr",
                title="Movie.Failed.2",
                category="id_match",
                status="failed",
                item_keys=["dl:fail2"]
            )
            self.assertFalse(db.mark_item_for_retry(del_id))
            self.assertFalse(db.mark_item_for_retry(dis_id))

            count = db.mark_all_rejected_for_retry()
            self.assertEqual(count, 2) # skip_id1 + fail_id2
            self.assertTrue(db.is_item_attempted(["dl:del1"]))
            self.assertTrue(db.is_item_attempted(["dl:dis1"]))
            self.assertFalse(db.is_item_attempted(["dl:skip1"]))
            self.assertFalse(db.is_item_attempted(["dl:fail2"]))

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
                success=True,
                prompt_text="Test prompt",
                response_text="Test response",
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
            self.assertEqual(history[0]["prompt_text"], "Test prompt")
            self.assertEqual(history[0]["response_text"], "Test response")

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
