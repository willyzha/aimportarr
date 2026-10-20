import asyncio
import logging
import signal
import sys
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

    logger.info(f"Configuration loaded (Dry Run: {config.general.dry_run}, Check Interval: {config.general.check_interval_seconds}s)")
    logger.info(f"AI Provider: {config.ai.provider} (Model: {config.ai.gemini.model if config.ai.provider == 'gemini' else config.ai.ollama.model})")

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
                dry_run=config.general.dry_run
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
                dry_run=config.general.dry_run
            )
            logger.info(f"Radarr client configured for: {config.radarr.url}")

    while running:
        # 1. Scan Sonarr queue
        if sonarr_client and running:
            try:
                queue_items = await sonarr_client.get_queue()
                if queue_items:
                    for item in queue_items:
                        if not running:
                            break
                        await sonarr_client.process_queue_item(item, ai_provider)
            except Exception as e:
                logger.error(f"Error during Sonarr scan: {e}", exc_info=True)

        # 2. Scan Radarr queue
        if radarr_client and running:
            try:
                queue_items = await radarr_client.get_queue()
                if queue_items:
                    for item in queue_items:
                        if not running:
                            break
                        await radarr_client.process_queue_item(item, ai_provider)
            except Exception as e:
                logger.error(f"Error during Radarr scan: {e}", exc_info=True)

        # Sleep in small increments to allow responsive shutdown
        for _ in range(config.general.check_interval_seconds):
            if not running:
                break
            await asyncio.sleep(1)

    logger.info("AImportarr has stopped.")

if __name__ == "__main__":
    asyncio.run(main())
