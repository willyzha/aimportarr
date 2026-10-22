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
            from src.config import load_config, AppConfig, PathMapping, GeneralConfig, AIConfig, CategoriesConfig, ExtractionConfig
            from src.extractor import inspect_archive, extract_archive, find_archive_file, resolve_local_path
            from src.classifier import classify_issue, ErrorCategory
            from src.sonarr import SonarrClient
            from src.radarr import RadarrClient
            from src.main import main
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

    def test_pydantic_model_definitions(self):
        """Verify all Pydantic models instantiate properly with default factories."""
        from src.config import AppConfig, PathMapping
        config = AppConfig()
        self.assertIsNotNone(config.general)
        self.assertIsNotNone(config.ai)
        self.assertIsNotNone(config.sonarr)
        self.assertIsNotNone(config.radarr)
        self.assertIsNotNone(config.categories)
        self.assertIsNotNone(config.extraction)
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

if __name__ == "__main__":
    unittest.main()
