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
  dry_run: false                 # Set to true to test matches without importing
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
  -v /path/to/config:/config \
  -e TZ=UTC \
  ghcr.io/willyzha/aimportarr:beta
```

---

### Option C: Unraid

1. Open the Unraid WebGUI and navigate to the **Docker** tab.
2. Click **Add Container** at the bottom.
3. Configure the container:
   - **Name**: `aimportarr`
   - **Repository**: `ghcr.io/willyzha/aimportarr:beta`
   - **Path /config**: `/mnt/user/appdata/aimportarr`
4. Place your configured `config.yml` in `/mnt/user/appdata/aimportarr/config.yml`.
5. Click **Apply**.

---

## ⚙️ Configuration Reference

| Setting | Default | Description |
| :--- | :--- | :--- |
| `general.dry_run` | `false` | When `true`, matches are analyzed and logged without executing imports. |
| `general.check_interval_seconds` | `120` | Interval between queue inspections. |
| `general.min_confidence` | `0.85` | AI confidence required (0.0 to 1.0) before executing an import. |
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
| `categories.<name>.dry_run` | `null` | Per-category dry-run override (`true`, `false`, or `null` to inherit `general.dry_run`). |

### 🎯 Error & Import Failure Categories

AImportarr intelligently categorizes download issues into granular categories, allowing individual toggles and independent `dry_run` control:

| Category | Description | AI Action |
| :--- | :--- | :--- |
| `id_match` | Release was matched to series/movie by ID (Sonarr/Radarr safety guardrail) | Verifies release name against candidate title/aliases. |
| `unknown_media` | Unknown series/movie (metadata missing from queue item) | Searches your tracked library items to identify the correct media. |
| `unknown_episode` | Series is known, but season/episode numbers could not be parsed | Extracts season and episode numbers from irregular file paths. |
| `sample_or_extra` | File flagged as a sample, preview, or short video | Evaluates whether the file is disposable or a genuine OVA/special. |
| `quality_or_language` | Rejected by quality cutoff or language profile | Overrides cutoff when enabled to import alternative releases. |
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
