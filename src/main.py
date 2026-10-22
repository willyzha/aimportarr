import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import sys
import httpx
from .config import load_config
from .ai import get_ai_provider
from .sonarr import SonarrClient
from .radarr import RadarrClient
from .metrics import metrics
from .web import WebServerRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("aimportarr")

running = True

def handle_stop_signal(sig, frame):
    global running
    logger.info("Received termination signal. Shutting down gracefully...")
    running = False

async def main():
    global running
    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)

    logger.info("Starting AImportarr - AI-Powered *arr Media Importer (Sonarr & Radarr)")
    config = load_config()

    # Set log level
    log_level = getattr(logging, config.general.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(log_level)

    # Silence verbose 3rd-party HTTP client logs unless in DEBUG mode
    http_log_level = logging.DEBUG if log_level == logging.DEBUG else logging.WARNING
    logging.getLogger("httpx").setLevel(http_log_level)
    logging.getLogger("httpcore").setLevel(http_log_level)

    # Configure rotating file logging if config directory is available
    log_dir = Path("/config") if Path("/config").is_dir() else Path(config.database.path).parent
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "aimportarr.log"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logging.getLogger().addHandler(file_handler)
        logger.info(f"File logging enabled: {log_file}")
    except Exception as e:
        logger.warning(f"Could not initialize file logger in '{log_dir}': {e}")

    logger.info(f"Configuration loaded (Check Interval: {config.general.check_interval_seconds}s)")
    metrics.init_database(
        db_path=config.database.path,
        retention_days=config.database.retention_days
    )

    all_cat_names = [
        "fake_or_malware",
        "id_match",
        "unknown_media",
        "unknown_episode",
        "sample_or_extra",
        "quality_or_language",
        "missing_episodes",
        "other",
    ]
    live_cats = []
    dry_run_cats = []
    disabled_cats = []

    for cat_name in all_cat_names:
        rule = getattr(config.categories, cat_name)
        if not rule.enabled:
            disabled_cats.append(cat_name)
            continue
        is_dry = config.categories.is_dry_run(cat_name)
        if is_dry:
            dry_run_cats.append(cat_name)
        else:
            live_cats.append(cat_name)

    live_str = ", ".join(live_cats) if live_cats else "none"
    dry_str = ", ".join(dry_run_cats) if dry_run_cats else "none"
    logger.info(f"Live categories (Auto-Import): {live_str}")
    logger.info(f"Dry run categories (Simulation only): {dry_str}")
    if disabled_cats:
        logger.info(f"Disabled categories: {', '.join(disabled_cats)}")

    logger.info(f"Archive Extractor: {'Enabled' if config.extraction.enabled else 'Disabled'} (Delete after import: {config.extraction.delete_after_import})")

    ai_provider = get_ai_provider(config.ai)

    # Initialize Sonarr
    sonarr_client = None
    if config.sonarr.enabled:
        if not config.sonarr.api_key:
            logger.warning("Sonarr is enabled but no api_key is configured.")
        else:
            sonarr_client = SonarrClient(
                base_url=config.sonarr.url,
                api_key=config.sonarr.api_key,
                min_confidence=config.general.min_confidence,
                categories=config.categories,
                extraction_config=config.extraction,
                path_mappings=config.path_mappings
            )
            logger.info(f"Sonarr client configured for: {config.sonarr.url}")

    # Initialize Radarr
    radarr_client = None
    if config.radarr.enabled:
        if not config.radarr.api_key:
            logger.warning("Radarr is enabled but no api_key is configured.")
        else:
            radarr_client = RadarrClient(
                base_url=config.radarr.url,
                api_key=config.radarr.api_key,
                min_confidence=config.general.min_confidence,
                categories=config.categories,
                extraction_config=config.extraction,
                path_mappings=config.path_mappings
            )
            logger.info(f"Radarr client configured for: {config.radarr.url}")

    # Start Lightweight Web UI & Metrics server
    web_server = None
    if config.web.enabled:
        try:
            web_server = WebServerRunner(
                host=config.web.host,
                port=config.web.port,
                metrics_instance=metrics
            )
            web_server.start()
        except Exception as we:
            logger.error(f"Failed to start Web UI server on {config.web.host}:{config.web.port}: {we}")

    while running:
        metrics.record_scan()
        # 1. Scan Sonarr queue
        if sonarr_client and running:
            try:
                queue_items = await sonarr_client.get_queue()
                if queue_items:
                    seen_downloads = set()
                    for item in queue_items:
                        if not running:
                            break
                        dl_id = item.get("downloadId")
                        if dl_id:
                            if dl_id in seen_downloads:
                                continue
                            seen_downloads.add(dl_id)
                        await sonarr_client.process_queue_item(item, ai_provider)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                logger.warning(f"Could not connect to Sonarr at '{config.sonarr.url}': connection refused or timed out. Please verify host and port.")
            except Exception as e:
                logger.error(f"Error during Sonarr scan: {e}")

        # 2. Scan Radarr queue
        if radarr_client and running:
            try:
                queue_items = await radarr_client.get_queue()
                if queue_items:
                    seen_downloads = set()
                    for item in queue_items:
                        if not running:
                            break
                        dl_id = item.get("downloadId")
                        if dl_id:
                            if dl_id in seen_downloads:
                                continue
                            seen_downloads.add(dl_id)
                        await radarr_client.process_queue_item(item, ai_provider)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                logger.warning(f"Could not connect to Radarr at '{config.radarr.url}': connection refused or timed out. Please verify host and port.")
            except Exception as e:
                logger.error(f"Error during Radarr scan: {e}")

        # Sleep in small increments to allow responsive shutdown
        for _ in range(config.general.check_interval_seconds):
            if not running:
                break
            await asyncio.sleep(1)

    if web_server:
        web_server.stop()

    logger.info("AImportarr has stopped.")

if __name__ == "__main__":
    asyncio.run(main())
