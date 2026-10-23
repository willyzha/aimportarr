<p align="center">
  <img src="assets/logo.png" width="180" alt="AImportarr Logo" />
</p>

<h1 align="center">AImportarr</h1>

<p align="center">
  <strong>Hands-off AI queue companion & secure archive unpacker for Sonarr & Radarr</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License" />
  <img src="https://img.shields.io/badge/Python-3.12-3776AB.svg?logo=python" alt="Python" />
  <img src="https://img.shields.io/badge/Docker-Multi--Arch-2496ED.svg?logo=docker" alt="Docker" />
</p>

---

## ✨ Overview

**AImportarr** monitors your **Sonarr** and **Radarr** download queues and automatically resolves stuck, warning, or blocked releases so your media server stays completely hands-off:

- **Two-Stage Hybrid Pipeline**: Lets Sonarr/Radarr handle normal downloads first, uses zero-token fast paths whenever Sonarr/Radarr can map unpacked files or enforce quality rules directly, and invokes AI (**Google Gemini** or local **Ollama**) only when ambiguity remains.
- **Secure Sandbox Extraction (Replaces Unpackerr)**: Inspects `.rar`, `.zip`, `.7z`, and `.tar` headers in memory before extracting. Clean video archives are unpacked into a temporary `<stem>_extracted` staging folder, imported, and automatically cleaned up so disk space is never duplicated.
- **Seeding-Safe Rejection Policy**:
  - **Imported**: Valid upgrades and unmapped episodes/movies are matched and imported automatically.
  - **Dismissed (Seeding Preserved)**: Quality/Custom Format downgrades, wrong media matches, and satisfied season packs are removed from the *arr queue (and blocklisted + re-searched when appropriate) **without deleting torrents from your client**.
  - **Rejected (Deleted from Client)**: Only confirmed **virus/malware, sample-only, or corrupt** releases are deleted from the torrent client and blocklisted.
- **Built-in Web UI & Live Settings (`:9890`)**: View processed history, inspect AI reasoning, retry items still in the queue, track AI token usage, and edit/hot-reload your configuration directly from the browser.

> 📖 **Deep Dive**: See [**Architecture & Two-Stage AI Design (`docs/ARCHITECTURE.md`)**](docs/ARCHITECTURE.md) for the complete 4-step pipeline flowchart, decision rules, and component breakdown.

---

## 🚀 Quick Setup

### 1. Deploy with Docker Compose (Recommended)

Create a `docker-compose.yml`:

```yaml
services:
  aimportarr:
    image: ghcr.io/willyzha/aimportarr:beta
    container_name: aimportarr
    restart: unless-stopped
    ports:
      - "9890:9890" # Web UI, Metrics & Live Settings
    volumes:
      - ./config:/config
      # Mount your downloads folder matching Sonarr/Radarr paths for archive extraction:
      - /path/to/downloads:/data/downloads
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=UTC
```

Start the container:

```bash
docker compose up -d
```

### 2. Configure via Web UI or `config.yml`

Open **`http://<your-ip>:9890`** and click the **⚙️ Settings** tab to configure your API keys, connections, and policies in the browser (changes save to `/config/config.yml` and hot-reload immediately).

Alternatively, create `config/config.yml` from [`config.example.yml`](config.example.yml):

```bash
mkdir -p config
curl -sSL https://raw.githubusercontent.com/willyzha/aimportarr/main/config.example.yml -o config/config.yml
```

```yaml
general:
  dry_run: false                    # true = simulation mode, false = live execution
  check_interval_seconds: 120       # Queue poll interval in seconds
  min_confidence: 0.85              # Minimum AI confidence (0.0 to 1.0)
  reject_on_low_confidence: false   # false = leave low-confidence items in queue; true = blocklist & keep seeding
  log_level: "INFO"

ai:
  provider: "gemini"                # Primary: "gemini" or "ollama"
  fallback_enabled: true            # Auto-fallback if primary provider hits rate limits or errors
  gemini:
    api_key: "YOUR_GEMINI_API_KEY"  # Free API key at https://aistudio.google.com/
    model: "auto"                   # "auto" selects the fastest active Flash model
  ollama:
    base_url: "http://localhost:11434"
    model: "qwen2.5:1.5b"

sonarr:
  enabled: true
  url: "http://localhost:8989"
  api_key: "YOUR_SONARR_API_KEY"

radarr:
  enabled: false
  url: "http://localhost:7878"
  api_key: "YOUR_RADARR_API_KEY"

extraction:
  enabled: true
  delete_after_import: true
  staging_suffix: "_extracted"
```

---

## 📦 Alternative Deployments

### Docker CLI (`docker run`)

```bash
docker run -d \
  --name aimportarr \
  --restart unless-stopped \
  -p 9890:9890 \
  -v /path/to/config:/config \
  -v /path/to/downloads:/data/downloads \
  -e PUID=1000 \
  -e PGID=1000 \
  -e TZ=UTC \
  ghcr.io/willyzha/aimportarr:beta
```

### Unraid

1. Open the Unraid WebGUI and go to the **Docker** tab → **Add Container**.
2. Configure:
   - **Name**: `aimportarr`
   - **Repository**: `ghcr.io/willyzha/aimportarr:beta`
   - **Port**: `9890` → `9890`
   - **Path `/config`**: `/mnt/user/appdata/aimportarr`
   - **Path `/data/downloads`**: `/mnt/user/Media/downloads` *(match your Sonarr/Radarr download container path, or map differences under **Path Mappings** in Settings)*
3. Click **Apply**, then open `http://<unraid-ip>:9890` → **⚙️ Settings** to enter your Sonarr, Radarr, and AI credentials.

---

## ⚙️ Configuration & Environment Variables

All settings can be configured in `/config/config.yml`, via the **⚙️ Settings** Web UI tab, or overridden with environment variables:

| Setting | Env Variable | Default | Description |
| :--- | :--- | :--- | :--- |
| `general.dry_run` | `DRY_RUN` | `false` | Simulate all decisions without modifying *arr or deleting files. |
| `general.check_interval_seconds` | `CHECK_INTERVAL_SECONDS` | `120` | Seconds between Sonarr/Radarr queue scans. |
| `general.min_confidence` | `MIN_CONFIDENCE` | `0.85` | Minimum AI confidence required (`0.0`–`1.0`) to import or delete files. |
| `general.reject_on_low_confidence` | `REJECT_ON_LOW_CONFIDENCE` | `false` | `false` leaves low-confidence items in queue (`In Queue`); `true` dismisses & blocklists while keeping seeding. |
| `ai.provider` | `AI_PROVIDER` | `gemini` | Primary AI backend (`gemini` or `ollama`). |
| `ai.fallback_enabled` | `AI_FALLBACK_ENABLED` | `true` | Automatically fail over to secondary provider on rate limits or errors. |
| `ai.gemini.api_key` | `GEMINI_API_KEY` | `""` | Google Gemini API key. |
| `ai.gemini.model` | `GEMINI_MODEL` | `auto` | Gemini model name (`auto` resolves fastest active Flash model). |
| `ai.ollama.base_url` | `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL. |
| `ai.ollama.model` | `OLLAMA_MODEL` | `qwen2.5:1.5b` | Ollama model tag. |
| `sonarr.enabled` / `url` / `api_key` | `SONARR_ENABLED`, `SONARR_URL`, `SONARR_API_KEY` | — | Sonarr connection settings. |
| `radarr.enabled` / `url` / `api_key` | `RADARR_ENABLED`, `RADARR_URL`, `RADARR_API_KEY` | — | Radarr connection settings. |
| `extraction.enabled` | `EXTRACTION_ENABLED` | `true` | Enable in-memory archive inspection & sandbox extraction. |
| `database.path` / `retention_days` | `DATABASE_PATH`, `DATABASE_RETENTION_DAYS` | `/config/aimportarr.db` (`30`d) | SQLite database path and history retention period. |

---

## 📄 Documentation & License

- [**Architecture & Two-Stage AI Design (`docs/ARCHITECTURE.md`)**](docs/ARCHITECTURE.md)
- Licensed under the [MIT License](LICENSE).
