import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
from typing import Optional
import httpx
from .config import load_config, AppConfig
from .ai import get_ai_provider
from .sonarr import SonarrClient
from .radarr import RadarrClient
from .metrics import metrics
from .web import WebServerRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("aimportarr")

running = True


def handle_stop_signal(sig, frame):
    global running
    logger.info("Received termination signal. Shutting down gracefully...")
    running = False


def build_runtime_clients(config: AppConfig):
    """Instantiate AI provider, SonarrClient, and RadarrClient from current AppConfig."""
    log_level = getattr(logging, config.general.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(log_level)
    http_log_level = logging.DEBUG if log_level == logging.DEBUG else logging.WARNING
    logging.getLogger("httpx").setLevel(http_log_level)
    logging.getLogger("httpcore").setLevel(http_log_level)

    ai_provider = None
    try:
        ai_provider = get_ai_provider(config.ai)
    except Exception as e:
        logger.warning(f"AI provider could not be initialized yet: {e}. Configure AI settings in the Web UI.")

    sonarr_client: Optional[SonarrClient] = None
    if config.sonarr.enabled:
        if not config.sonarr.api_key or config.sonarr.api_key.startswith("YOUR_"):
            logger.warning("Sonarr is enabled but no valid api_key is configured.")
        else:
            sonarr_client = SonarrClient(
                base_url=config.sonarr.url,
                api_key=config.sonarr.api_key,
                min_confidence=config.general.min_confidence,
                dry_run=config.general.dry_run,
                reject_on_low_confidence=config.general.reject_on_low_confidence,
                extraction_config=config.extraction,
                path_mappings=config.path_mappings,
            )
            logger.info(f"Sonarr client configured for: {config.sonarr.url}")

    radarr_client: Optional[RadarrClient] = None
    if config.radarr.enabled:
        if not config.radarr.api_key or config.radarr.api_key.startswith("YOUR_"):
            logger.warning("Radarr is enabled but no valid api_key is configured.")
        else:
            radarr_client = RadarrClient(
                base_url=config.radarr.url,
                api_key=config.radarr.api_key,
                min_confidence=config.general.min_confidence,
                dry_run=config.general.dry_run,
                reject_on_low_confidence=config.general.reject_on_low_confidence,
                extraction_config=config.extraction,
                path_mappings=config.path_mappings,
            )
            logger.info(f"Radarr client configured for: {config.radarr.url}")

    return ai_provider, sonarr_client, radarr_client


async def main():
    global running
    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)

    logger.info("Starting AImportarr - AI-Powered *arr Media Importer (Sonarr & Radarr)")
    config = load_config()

    log_level = getattr(logging, config.general.log_level.upper(), logging.INFO)
    log_dir = Path("/config") if Path("/config").is_dir() else Path(config.database.path).parent
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "aimportarr.log"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logging.getLogger().addHandler(file_handler)
        logger.info(f"File logging enabled: {log_file}")
    except Exception as e:
        logger.warning(f"Could not initialize file logger in '{log_dir}': {e}")

    mode_str = "DRY RUN (Simulation)" if config.general.dry_run else "LIVE (Auto-Import & Blocklist)"
    low_conf_str = (
        "Hands-Off (Blocklist & Re-Search, Keep Seeding)"
        if config.general.reject_on_low_confidence
        else "Conservative (Leave in Queue)"
    )
    logger.info(
        f"Configuration loaded | Mode: {mode_str} | Low-Confidence Policy: {low_conf_str} | Check Interval: {config.general.check_interval_seconds}s"
    )
    logger.info(
        f"Archive Extractor: {'Enabled' if config.extraction.enabled else 'Disabled'} (Delete after import: {config.extraction.delete_after_import})"
    )

    metrics.init_database(
        db_path=config.database.path,
        retention_days=config.database.retention_days,
    )

    state = {}
    ai_provider, sonarr_client, radarr_client = build_runtime_clients(config)
    state["ai_provider"] = ai_provider
    state["sonarr_client"] = sonarr_client
    state["radarr_client"] = radarr_client

    def on_config_updated(updated_config: AppConfig):
        logger.info("Hot-reloading runtime clients from updated Web UI configuration...")
        new_ai, new_sonarr, new_radarr = build_runtime_clients(updated_config)
        state["ai_provider"] = new_ai
        state["sonarr_client"] = new_sonarr
        state["radarr_client"] = new_radarr

    web_server = None
    if config.web.enabled:
        try:
            web_server = WebServerRunner(
                host=config.web.host,
                port=config.web.port,
                metrics_instance=metrics,
                config_instance=config,
                on_config_updated=on_config_updated,
            )
            web_server.start()
        except Exception as we:
            logger.error(f"Failed to start Web UI server on {config.web.host}:{config.web.port}: {we}")

    while running:
        metrics.record_scan()
        curr_ai = state.get("ai_provider")
        curr_sonarr = state.get("sonarr_client")
        curr_radarr = state.get("radarr_client")

        # 1. Scan Sonarr queue
        if curr_sonarr and curr_ai and running:
            try:
                queue_items = await curr_sonarr.get_queue()
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
                        await curr_sonarr.process_queue_item(item, curr_ai)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                logger.warning(
                    f"Could not connect to Sonarr at '{config.sonarr.url}': connection refused or timed out. Please verify host and port."
                )
            except Exception as e:
                logger.error(f"Error during Sonarr scan: {e}")

        # 2. Scan Radarr queue
        if curr_radarr and curr_ai and running:
            try:
                queue_items = await curr_radarr.get_queue()
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
                        await curr_radarr.process_queue_item(item, curr_ai)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                logger.warning(
                    f"Could not connect to Radarr at '{config.radarr.url}': connection refused or timed out. Please verify host and port."
                )
            except Exception as e:
                logger.error(f"Error during Radarr scan: {e}")

        for _ in range(config.general.check_interval_seconds):
            if not running:
                break
            await asyncio.sleep(1)

    if web_server:
        web_server.stop()

    logger.info("AImportarr has stopped.")


if __name__ == "__main__":
    asyncio.run(main())
