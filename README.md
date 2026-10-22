<p align="center">
  <img src="assets/logo.png" width="180" alt="AImportarr Logo" />
</p>

<h1 align="center">AImportarr</h1>

<p align="center">
  <strong>AI-powered media matching and automated import companion for Sonarr & Radarr</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License" />
  <img src="https://img.shields.io/badge/Python-3.12-3776AB.svg?logo=python" alt="Python" />
  <img src="https://img.shields.io/badge/Docker-Multi--Arch-2496ED.svg?logo=docker" alt="Docker" />
</p>

---

## 🚀 Quick Setup

### 1. Download or Create Config
Create a configuration folder and copy the example configuration:

```bash
mkdir -p config
curl -sSL https://raw.githubusercontent.com/willyzha/aimportarr/main/config.example.yml -o config/config.yml
```

### 2. Configure `config/config.yml`
Edit `config/config.yml` with your connection details:

```yaml
general:
  check_interval_seconds: 120   # How often to check queues (in seconds)
  min_confidence: 0.85          # Confidence threshold (0.0 to 1.0)
  log_level: "INFO"

ai:
  provider: "gemini"            # Primary: "gemini" or "ollama"
  fallback_enabled: true        # If Gemini throttles/fails, auto-fallback to Ollama

  # Google Gemini (Free API key at https://aistudio.google.com/)
  gemini:
    api_key: "YOUR_GEMINI_API_KEY"
    model: "auto"                 # Default: "auto" picks smallest/fastest active model

  # Local Ollama (Self-hosted fallback or primary)
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
```

---

## 🐳 Docker Deployment

### Option A: Docker Compose (Recommended)

Create a `docker-compose.yml`:

```yaml
version: "3.8"

services:
  aimportarr:
    image: ghcr.io/willyzha/aimportarr:beta
    container_name: aimportarr
    restart: unless-stopped
    ports:
      - "9890:9890" # Web UI Metrics & History Monitor
    volumes:
      - ./config:/config
    environment:
      - TZ=UTC
```

Start the container:
```bash
docker compose up -d
```

---

### Option B: Docker CLI (`docker run`)

```bash
docker run -d \
  --name aimportarr \
  --restart unless-stopped \
  -p 9890:9890 \
  -v /path/to/config:/config \
  -e TZ=UTC \
  ghcr.io/willyzha/aimportarr:beta
```

---

## 📊 Lightweight Web UI & Metrics Monitor

AImportarr includes an in-memory, ultra-lightweight web dashboard accessible at **`http://<your-ip>:9890`**:

- **Processed History**: Live feed of all scanned queue items, categorized issues, actions taken (Imported, Blocked, Dismissed, Dry Run), and expandable row drawers displaying full AI decision reasoning.
- **Token Usage History**: Real-time log of every AI API interaction, tracking prompt tokens, completion tokens, latency (ms), and success/failure status.
- **Model Analytics**: Aggregate breakdown of token consumption by model (`gemini-2.5-flash`, `ollama (qwen2.5)`, etc.) and operation type.
- **Real-Time Monitoring**: Selectable auto-refresh (5s, 10s, 30s) and zero external CDN dependencies (works 100% offline in isolated homelabs).
- **REST API Endpoints**:
  - `GET /api/metrics`: JSON summary of uptime, counters, and token totals.
  - `GET /api/history?limit=100`: JSON list of recently processed items.
  - `GET /api/tokens?limit=100`: JSON list of recent token transactions.
  - `GET /api/health`: Container healthcheck endpoint.

---

### Option C: Unraid

1. Open the Unraid WebGUI and navigate to the **Docker** tab.
2. Click **Add Container** at the bottom (or edit your existing `aimportarr` container).
3. Configure the container:
   - **Name**: `aimportarr`
   - **Repository**: `ghcr.io/willyzha/aimportarr:beta`
   - **Path /config**: `/mnt/user/appdata/aimportarr`
   - **Path /data/downloads** *(Optional, to replace Unpackerr)*: `/mnt/user/Media/downloads` (must match Sonarr/Radarr's internal download path)
4. Place your configured `config.yml` in `/mnt/user/appdata/aimportarr/config.yml`.
5. Click **Apply**.

---

## 📦 Secure Archive Extraction (Replaces Unpackerr)

Unlike Unpackerr—which blindly extracts archives and often leaves malicious executables on your disk—AImportarr uses **Zero-Extract Inspection + AI Verification**:

1. **Pre-Extraction In-Memory Inspection**: Reads archive headers (`.zip`, `.rar`, `.7z`, `.tar`) in memory without extracting anything.
2. **AI Malware Detection**: If an archive contains executables (`.exe`, `.scr`, `.bat`) or no video streams, AI evaluates the release and automatically deletes and blocklists it in Sonarr/Radarr and your download client. Zero bytes of malware are ever written to your array.
3. **Sandbox Extraction & Auto-Cleanup**: If valid media is verified, extracts into an isolated staging folder with strict Zip-Slip protection, triggers the Sonarr/Radarr manual import, and automatically cleans up the extracted staging folder so your disk space is never duplicated while your seeding torrent remains 100% intact.

To enable extraction, mount your downloads folder to the container (matching Sonarr/Radarr's path).

---

## ⚙️ Configuration Reference

| Setting | Default | Description |
| :--- | :--- | :--- |
| `general.check_interval_seconds` | `120` | Interval between queue inspections. |
| `general.min_confidence` | `0.85` | AI confidence required (0.0 to 1.0) before executing an import. |
| `web.enabled` | `true` | Enable built-in web UI dashboard and REST metrics API. |
| `web.port` | `9890` | Web UI HTTP listening port. |
| `extraction.enabled` | `true` | Enable automated secure archive inspection and extraction. |
| `extraction.delete_after_import` | `true` | Delete temporary extracted video files after Sonarr/Radarr import (keeps original archive seeding). |
| `path_mappings` | `[]` | Optional: List of `{from: "/remote", to: "/local"}` path mappings if Sonarr/Radarr container paths differ from AImportarr. |
| `ai.provider` | `gemini` | Selected primary AI backend: `gemini` or `ollama`. |
| `ai.fallback_enabled` | `true` | When `true`, automatically delegates to the secondary provider if the primary encounters invalid keys, rate limits (HTTP 429), token exhaustion, or network errors. |
| `ai.gemini.api_key` | `""` | Google Gemini API key (Get a free key at [aistudio.google.com](https://aistudio.google.com/)). |
| `ai.gemini.model` | `auto` | Gemini model to use (`auto` dynamically selects the smallest/fastest active model, e.g. `flash-lite` / `flash`, and auto-heals on 404s). |
| `ai.ollama.base_url` | `http://localhost:11434` | Network address of your Ollama instance. |
| `ai.ollama.model` | `qwen2.5:3b` | Model installed in Ollama (`qwen2.5:3b`, `llama3.2`, etc.). |
| `sonarr.enabled` | `true` | Enable or disable Sonarr queue monitoring. |
| `sonarr.url` | `http://localhost:8989` | Base URL of your Sonarr instance. |
| `sonarr.api_key` | `""` | Sonarr API key (*Settings → General → Security*). |
| `radarr.enabled` | `false` | Enable or disable Radarr queue monitoring. |
| `radarr.url` | `http://localhost:7878` | Base URL of your Radarr instance. |
| `radarr.api_key` | `""` | Radarr API key (*Settings → General → Security*). |
| `categories.<name>.enabled` | `true` | Enable or disable processing for specific error categories. |
| `categories.<name>.dry_run` | `true` | Per-category dry-run toggle (`false` to execute real imports, `true` for simulation only). |

### 🎯 Error & Import Failure Categories

AImportarr intelligently categorizes download issues into granular categories, allowing individual toggles and independent `dry_run` control:

| Category | Description | AI Action |
| :--- | :--- | :--- |
| `fake_or_malware` | Fake releases, trojans, unsupported archives masquerading as media | Verifies with AI, then deletes from download client and permanently blocklists. |
| `id_match` | Release was matched to series/movie by ID (Sonarr/Radarr safety guardrail) | Verifies release name against candidate title/aliases. |
| `unknown_media` | Unknown series/movie (metadata missing from queue item) | Searches your tracked library items to identify the correct media. |
| `unknown_episode` | Series is known, but season/episode numbers could not be parsed | Extracts season and episode numbers from irregular file paths. |
| `sample_or_extra` | File flagged as a sample, preview, or short video | Evaluates whether the file is disposable or a genuine OVA/special. |
| `quality_or_language` | Rejected by quality cutoff or language profile | Overrides cutoff when enabled to import alternative releases. |
| `missing_episodes` | Expected episodes missing from release (e.g. single episode labeled as season pack) | Imports available files, then dismisses lingering season pack warnings from queue without deleting from download client. |
| `other` | **Fallback / Catch-All** for unclassified queue warnings or errors | Analyzes release and applies configured dry-run/import rules. |

### Environment Variable Overrides

All options can alternatively be passed as container environment variables:

```bash
DRY_RUN=false
AI_PROVIDER=gemini
AI_FALLBACK_ENABLED=true
GEMINI_API_KEY=your_gemini_api_key
OLLAMA_BASE_URL=http://localhost:11434
SONARR_URL=http://localhost:8989
SONARR_API_KEY=your_sonarr_api_key
RADARR_URL=http://localhost:7878
RADARR_API_KEY=your_radarr_api_key

# Category dry-run and enabled overrides
CATEGORY_ID_MATCH_DRY_RUN=false
CATEGORY_UNKNOWN_MEDIA_DRY_RUN=true
CATEGORY_OTHER_DRY_RUN=false
CATEGORY_SAMPLE_OR_EXTRA_ENABLED=true
```

---

## 📋 Logs & Verification

To verify that AImportarr is running and watching your queues:

```bash
docker logs -f aimportarr
```

Sample output:
```text
2026-10-19 23:00:00 [INFO] [aimportarr] Starting AImportarr (Sonarr & Radarr)
2026-10-19 23:00:00 [INFO] [aimportarr] Configuration loaded (Dry Run: False, Check Interval: 120s)
2026-10-19 23:00:00 [INFO] [aimportarr.ai] AI Provider initialized: Gemini (gemini-2.0-flash) with automatic fallback to Ollama (qwen2.5:3b)
2026-10-19 23:00:00 [INFO] [aimportarr.sonarr] Sonarr client configured for: http://localhost:8989
```

---

## 📄 License
MIT License. See [LICENSE](LICENSE) for details.
