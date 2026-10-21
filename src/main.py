import asyncio
import logging
import signal
import sys
import httpx
from .config import load_config
from .ai import get_ai_provider
from .sonarr import SonarrClient
from .radarr import RadarrClient

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

    logger.info(f"Configuration loaded (Dry Run: {config.general.dry_run}, Check Interval: {config.general.check_interval_seconds}s)")
    active_cats = [c for c in ["id_match", "unknown_media", "unknown_episode", "sample_or_extra", "quality_or_language", "missing_episodes", "other"] if getattr(config.categories, c).enabled]
    logger.info(f"Active categories: {', '.join(active_cats)}")
    for cat_name in ["id_match", "unknown_media", "unknown_episode", "sample_or_extra", "quality_or_language", "missing_episodes", "other"]:
        rule = getattr(config.categories, cat_name)
        cat_dry = rule.dry_run if rule.dry_run is not None else f"{config.general.dry_run} (inherited)"
        logger.debug(f"  Category '{cat_name}': enabled={rule.enabled}, dry_run={cat_dry}")

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
                dry_run=config.general.dry_run,
                categories=config.categories
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
                dry_run=config.general.dry_run,
                categories=config.categories
            )
            logger.info(f"Radarr client configured for: {config.radarr.url}")

    while running:
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

    logger.info("AImportarr has stopped.")

if __name__ == "__main__":
    asyncio.run(main())
