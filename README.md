<p align="center">
  <img src="assets/logo.png" width="180" alt="AImportarr Logo" />
</p>

<h1 align="center">AImportarr</h1>

<p align="center">
  <strong>Hands-off AI queue companion & archive unpacker for Sonarr & Radarr</strong>
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
- **Season Pack Backfill & Ongoing Episode Retry**:
  - **Completed Seasons**: Automatically backfills finished seasons with missing episodes using full season packs, even if Sonarr rejects the pack because some episodes on disk have higher quality.
  - **Ongoing Seasons & Bulk Matching**: Retries missing aired episodes for incomplete seasons using **bulk season searches (1 tracker query per season)**, zero-token Tier 1 in-memory matching, multi-episode greedy satisfaction (e.g. `E01-E02`), and single bulk AI prompts for unresolved titles.
  - **Inline Zero-Tracker Fallback**: If a completed season finds 0 qualifying season packs, it immediately falls back inline to match individual missing episodes using the already-fetched release pool—**without hitting trackers again**.
- **Automatic Archive Extraction**: Inspects `.rar`, `.zip`, `.7z`, and `.tar` headers before extracting. Valid video archives are unpacked into a temporary `<stem>_extracted` staging folder, imported, and automatically cleaned up so disk space is never duplicated.
- **Safe Rejection Policy**:
  - **Imported**: Valid upgrades and unmapped episodes/movies are matched and imported automatically.
  - **Dismissed (Files Preserved)**: Quality/Custom Format downgrades, wrong media matches, and satisfied season packs are removed from the *arr queue (and blocklisted + re-searched when appropriate) **without deleting files from your download client**.
  - **Rejected (Deleted from Client)**: Only confirmed **virus/malware, sample-only, or corrupt** releases are deleted from the download client and blocklisted.
- **Built-in Web UI & Live Settings (`:9890`)**: View processed history, inspect AI reasoning, retry items still in the queue, track AI token usage, and edit/hot-reload your configuration directly from the browser.

---

## 🚀 Quick Setup (Docker Compose)

Create a `docker-compose.yml` and start the container (`docker compose up -d`):

```yaml
services:
  aimportarr:
    image: ghcr.io/willyzha/aimportarr:latest
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

Then open **`http://<your-ip>:9890`** and go to the **⚙️ Settings** tab to configure your Sonarr, Radarr, and AI settings in the browser (changes save to `/config/config.yml` and hot-reload immediately).

---

## 📖 Documentation & Configuration Reference

- [**Architecture & Two-Stage AI Design (`docs/ARCHITECTURE.md`)**](docs/ARCHITECTURE.md) — Detailed 4-step pipeline flowchart, FIFO queue & webhook design, and status lifecycle.
- [**Configuration & Environment Variables (`docs/ARCHITECTURE.md#configuration--environment-variables`)**](docs/ARCHITECTURE.md#configuration--environment-variables) — Full reference of all settings and environment variable overrides.
- [**Annotated Example Config (`config.example.yml`)**](config.example.yml) — Template YAML configuration file.
- Licensed under the [MIT License](LICENSE).
