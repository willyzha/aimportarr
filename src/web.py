import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
from typing import Optional, Any

from .metrics import MetricsTracker, metrics as default_metrics

logger = logging.getLogger("aimportarr.web")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AImportarr - Metrics & History</title>
  <link rel="icon" type="image/png" href="/assets/logo.png">
  <style>
    :root {
      --bg-base: #0b0e14;
      --bg-card: #151921;
      --bg-card-hover: #1b212c;
      --border: #262e3d;
      --border-subtle: #1e2430;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --accent: #6366f1;
      --accent-glow: rgba(99, 102, 241, 0.25);
      --success: #10b981;
      --success-bg: rgba(16, 185, 129, 0.15);
      --warning: #f59e0b;
      --warning-bg: rgba(245, 158, 11, 0.15);
      --danger: #ef4444;
      --danger-bg: rgba(239, 68, 68, 0.15);
      --info: #06b6d4;
      --info-bg: rgba(6, 182, 212, 0.15);
      --purple: #8b5cf6;
      --purple-bg: rgba(139, 92, 246, 0.15);
      --sonarr: #38bdf8;
      --radarr: #fb923c;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: var(--bg-base);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      line-height: 1.5;
      font-size: 14px;
    }

    /* Top Navigation */
    header {
      background-color: #11151c;
      border-bottom: 1px solid var(--border);
      padding: 0.75rem 1.5rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 50;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 0.85rem;
      text-decoration: none;
      color: var(--text-main);
    }

    .brand img {
      width: 32px;
      height: 32px;
      border-radius: 6px;
      object-fit: contain;
    }

    .brand-fallback-logo {
      width: 32px;
      height: 32px;
      border-radius: 6px;
      background: linear-gradient(135deg, #4f46e5, #7c3aed);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 14px;
      color: #fff;
    }

    .brand-title {
      font-size: 1.15rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }

    .brand-subtitle {
      font-size: 0.75rem;
      color: var(--text-muted);
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 1rem;
    }

    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      background: rgba(16, 185, 129, 0.12);
      color: var(--success);
      border: 1px solid rgba(16, 185, 129, 0.3);
      padding: 0.25rem 0.65rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
    }

    .pulse-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background-color: var(--success);
      box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
      animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
      70% { box-shadow: 0 0 0 7px rgba(16, 185, 129, 0); }
      100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }

    .btn {
      background-color: var(--bg-card);
      color: var(--text-main);
      border: 1px solid var(--border);
      padding: 0.45rem 0.85rem;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.8rem;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      transition: all 0.15s ease;
    }

    .btn:hover {
      background-color: var(--bg-card-hover);
      border-color: #3b4556;
    }

    .btn-primary {
      background-color: var(--accent);
      border-color: var(--accent);
      color: white;
    }

    .btn-primary:hover {
      background-color: #4f46e5;
      border-color: #4f46e5;
    }

    .btn-sm {
      padding: 0.22rem 0.55rem;
      font-size: 0.72rem;
      border-radius: 4px;
    }

    .btn-retry {
      background-color: rgba(245, 158, 11, 0.12);
      border: 1px solid rgba(245, 158, 11, 0.35);
      color: #fbbf24;
      font-weight: 600;
    }

    .btn-retry:hover {
      background-color: rgba(245, 158, 11, 0.25);
      border-color: #f59e0b;
      color: #fff;
    }

    .btn-warning {
      background-color: rgba(245, 158, 11, 0.15);
      border: 1px solid rgba(245, 158, 11, 0.4);
      color: #fbbf24;
    }

    .btn-warning:hover {
      background-color: rgba(245, 158, 11, 0.3);
      color: #fff;
    }

    .toast-container {
      position: fixed;
      bottom: 1.5rem;
      right: 1.5rem;
      z-index: 999;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      pointer-events: none;
    }

    .toast {
      background: #1e2430;
      border: 1px solid var(--border);
      border-left: 4px solid var(--accent);
      color: var(--text-main);
      padding: 0.65rem 1rem;
      border-radius: 6px;
      font-size: 0.8rem;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
      animation: toastIn 0.2s ease forwards;
      pointer-events: auto;
      max-width: 380px;
    }

    .toast-success { border-left-color: var(--success); }
    .toast-warning { border-left-color: var(--warning); }
    .toast-danger { border-left-color: var(--danger); }

    @keyframes toastIn {
      from { transform: translateY(12px); opacity: 0; }
      to { transform: translateY(0); opacity: 1; }
    }

    select.input-select {
      background-color: var(--bg-card);
      color: var(--text-main);
      border: 1px solid var(--border);
      padding: 0.4rem 0.65rem;
      border-radius: 6px;
      font-size: 0.8rem;
      cursor: pointer;
      outline: none;
    }

    /* Main Container */
    main {
      flex: 1;
      padding: 1.5rem;
      max-width: 1400px;
      margin: 0 auto;
      width: 100%;
    }

    /* Metric Cards Grid */
    .metrics-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 1rem;
      margin-bottom: 1.5rem;
    }

    .metric-card {
      background-color: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.15rem;
      display: flex;
      flex-direction: column;
      position: relative;
      overflow: hidden;
      transition: transform 0.15s ease, border-color 0.15s ease;
    }

    .metric-card:hover {
      border-color: #3b4556;
      transform: translateY(-2px);
    }

    .metric-card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 0.5rem;
    }

    .metric-title {
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .metric-icon {
      width: 28px;
      height: 28px;
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 14px;
    }

    .metric-value {
      font-size: 1.75rem;
      font-weight: 700;
      color: var(--text-main);
      line-height: 1.1;
      margin-bottom: 0.35rem;
      font-feature-settings: "tnum";
    }

    .metric-sub {
      font-size: 0.75rem;
      color: var(--text-dim);
    }

    /* Tabs Bar */
    .tabs-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid var(--border);
      margin-bottom: 1.25rem;
      gap: 1rem;
      flex-wrap: wrap;
    }

    .tab-buttons {
      display: flex;
      gap: 0.5rem;
    }

    .tab-btn {
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 0.65rem 1rem;
      font-size: 0.875rem;
      font-weight: 600;
      cursor: pointer;
      position: relative;
      border-bottom: 2px solid transparent;
      margin-bottom: -1px;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      transition: color 0.15s ease;
    }

    .tab-btn:hover {
      color: var(--text-main);
    }

    .tab-btn.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
    }

    .tab-badge {
      background-color: #232b38;
      color: var(--text-muted);
      padding: 0.15rem 0.45rem;
      border-radius: 9999px;
      font-size: 0.7rem;
      font-weight: 600;
    }

    .tab-btn.active .tab-badge {
      background-color: var(--accent-glow);
      color: #c7d2fe;
    }

    /* Filters / Search Bar */
    .table-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      margin-bottom: 1rem;
      flex-wrap: wrap;
    }

    .search-box {
      position: relative;
      flex: 1;
      max-width: 360px;
    }

    .search-box input {
      width: 100%;
      background-color: var(--bg-card);
      border: 1px solid var(--border);
      color: var(--text-main);
      padding: 0.45rem 0.85rem 0.45rem 2rem;
      border-radius: 6px;
      font-size: 0.825rem;
      outline: none;
    }

    .search-box input:focus {
      border-color: var(--accent);
    }

    .search-icon {
      position: absolute;
      left: 0.65rem;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-dim);
      font-size: 13px;
    }

    .filter-pills {
      display: flex;
      gap: 0.4rem;
      flex-wrap: wrap;
    }

    .filter-pill {
      background-color: var(--bg-card);
      border: 1px solid var(--border);
      color: var(--text-muted);
      padding: 0.3rem 0.65rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      cursor: pointer;
      user-select: none;
      transition: all 0.15s ease;
    }

    .filter-pill:hover {
      background-color: var(--bg-card-hover);
      color: var(--text-main);
    }

    .filter-pill.active {
      background-color: var(--accent);
      border-color: var(--accent);
      color: white;
    }

    /* Tables */
    .card-table-wrapper {
      background-color: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow-x: auto;
      overflow-y: hidden;
      -webkit-overflow-scrolling: touch;
    }

    .card-table-wrapper::-webkit-scrollbar {
      height: 6px;
    }

    .card-table-wrapper::-webkit-scrollbar-track {
      background: #11151d;
      border-radius: 4px;
    }

    .card-table-wrapper::-webkit-scrollbar-thumb {
      background: #2b3545;
      border-radius: 4px;
    }

    .card-table-wrapper::-webkit-scrollbar-thumb:hover {
      background: #3e4c63;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
    }

    th {
      background-color: #12161f;
      color: var(--text-muted);
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--border);
      user-select: none;
    }

    td {
      padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--border-subtle);
      font-size: 0.825rem;
      vertical-align: middle;
    }

    tr:last-child td {
      border-bottom: none;
    }

    tbody tr:hover {
      background-color: var(--bg-card-hover);
    }

    /* Badges */
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
      padding: 0.2rem 0.5rem;
      border-radius: 4px;
      font-size: 0.7rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      white-space: nowrap;
    }

    .badge-imported { background: var(--success-bg); color: var(--success); }
    .badge-dry_run { background: var(--warning-bg); color: var(--warning); }
    .badge-blocked { background: var(--danger-bg); color: var(--danger); }
    .badge-rejected { background: rgba(239, 68, 68, 0.12); color: #f87171; }
    .badge-dismissed { background: var(--info-bg); color: var(--info); }
    .badge-skipped { background: rgba(148, 163, 184, 0.12); color: var(--text-muted); }

    .badge-sonarr { background: rgba(56, 189, 248, 0.15); color: var(--sonarr); }
    .badge-radarr { background: rgba(251, 146, 60, 0.15); color: var(--radarr); }

    .badge-category {
      background: rgba(139, 92, 246, 0.12);
      color: #c084fc;
      border: 1px solid rgba(139, 92, 246, 0.25);
      font-size: 0.68rem;
    }

    .title-cell {
      max-width: 380px;
      word-break: break-all;
    }

    .title-main {
      font-weight: 600;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }

    .title-sub {
      font-size: 0.725rem;
      color: var(--text-dim);
      margin-top: 0.15rem;
    }

    .reasoning-row {
      display: none;
      background-color: #0d1017;
    }

    .reasoning-row.open {
      display: table-row;
    }

    .reasoning-box {
      padding: 0.85rem 1.25rem;
      font-size: 0.8rem;
      color: #cbd5e1;
      border-left: 3px solid var(--accent);
      margin: 0.35rem 0;
      background-color: #121620;
      border-radius: 0 6px 6px 0;
    }

    .clickable-row {
      cursor: pointer;
    }

    .empty-state {
      padding: 3rem 1rem;
      text-align: center;
      color: var(--text-dim);
    }

    .empty-icon {
      font-size: 2rem;
      margin-bottom: 0.5rem;
      opacity: 0.6;
    }

    /* Model Analytics Cards */
    .analytics-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 1.25rem;
      margin-bottom: 1.5rem;
    }

    .analytics-card {
      background-color: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.25rem;
    }

    .analytics-card-title {
      font-size: 0.9rem;
      font-weight: 700;
      color: var(--text-main);
      margin-bottom: 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .bar-wrapper {
      margin-bottom: 0.75rem;
    }

    .bar-label {
      display: flex;
      justify-content: space-between;
      font-size: 0.75rem;
      color: var(--text-muted);
      margin-bottom: 0.25rem;
    }

    .bar-track {
      height: 8px;
      background-color: #1f2735;
      border-radius: 9999px;
      overflow: hidden;
      display: flex;
    }

    .bar-fill {
      height: 100%;
      background-color: var(--accent);
      border-radius: 9999px;
    }

    .bar-fill-prompt { background-color: #3b82f6; }
    .bar-fill-completion { background-color: #10b981; }

    /* Footer */
    footer {
      border-top: 1px solid var(--border);
      padding: 1rem 1.5rem;
      text-align: center;
      font-size: 0.75rem;
      color: var(--text-dim);
      margin-top: auto;
    }

    .spin {
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin {
      100% { transform: rotate(360deg); }
    }

    /* Tablet & iPad Vertical Responsive Optimizations */
    @media (max-width: 1024px) {
      main {
        padding: 1.25rem 1rem;
      }
      th, td {
        padding: 0.6rem 0.65rem;
        font-size: 0.8rem;
      }
      .title-cell {
        max-width: 250px;
      }
    }

    @media (max-width: 768px) {
      main {
        padding: 1rem 0.65rem;
      }
      th, td {
        padding: 0.5rem 0.45rem;
        font-size: 0.75rem;
      }
      .table-toolbar {
        flex-direction: column;
        align-items: stretch;
      }
      .search-box {
        max-width: 100%;
      }
    }
  </style>
</head>
<body>

  <!-- Header -->
  <header>
    <a href="/" class="brand">
      <img src="/assets/logo.png" alt="AImportarr" onerror="this.style.display='none'; document.getElementById('fallback-logo').style.display='flex';">
      <div id="fallback-logo" class="brand-fallback-logo" style="display: none;">AI</div>
      <div>
        <div class="brand-title">AImportarr</div>
        <div class="brand-subtitle">AI-Powered *arr Media Importer</div>
      </div>
    </a>

    <div class="header-actions">
      <div class="status-badge">
        <span class="pulse-dot"></span>
        <span id="header-status">Online</span>
      </div>

      <div style="display: flex; align-items: center; gap: 0.4rem;">
        <label for="refresh-interval" style="font-size: 0.75rem; color: var(--text-muted);">Auto-refresh:</label>
        <select id="refresh-interval" class="input-select" onchange="updateAutoRefresh()">
          <option value="5000" selected>5s</option>
          <option value="10000">10s</option>
          <option value="30000">30s</option>
          <option value="0">Off</option>
        </select>
      </div>

      <button id="refresh-btn" class="btn" onclick="fetchData(true)">
        <span id="refresh-icon">🔄</span> Refresh
      </button>
    </div>
  </header>

  <!-- Main Content -->
  <main>
    <!-- Top Stat Cards -->
    <div class="metrics-grid">
      <!-- Uptime & Scans -->
      <div class="metric-card">
        <div class="metric-card-header">
          <span class="metric-title">Uptime</span>
          <div class="metric-icon" style="background: rgba(99, 102, 241, 0.15); color: #818cf8;">⏱</div>
        </div>
        <div class="metric-value" id="stat-uptime">--</div>
        <div class="metric-sub" id="stat-scans">Queue Scans: 0</div>
      </div>

      <!-- Processed Items -->
      <div class="metric-card">
        <div class="metric-card-header">
          <span class="metric-title">Processed</span>
          <div class="metric-icon" style="background: rgba(14, 165, 233, 0.15); color: #38bdf8;">📋</div>
        </div>
        <div class="metric-value" id="stat-processed">0</div>
        <div class="metric-sub" id="stat-processed-sub">Scanned items</div>
      </div>

      <!-- Auto-Imported -->
      <div class="metric-card">
        <div class="metric-card-header">
          <span class="metric-title">Imported</span>
          <div class="metric-icon" style="background: var(--success-bg); color: var(--success);">✔</div>
        </div>
        <div class="metric-value" style="color: var(--success);" id="stat-imported">0</div>
        <div class="metric-sub" id="stat-dry-run">0 in Dry Run</div>
      </div>

      <!-- Blocked / Rejected -->
      <div class="metric-card">
        <div class="metric-card-header">
          <span class="metric-title">Blocked & Rejected</span>
          <div class="metric-icon" style="background: var(--danger-bg); color: var(--danger);">🛡</div>
        </div>
        <div class="metric-value" style="color: #f87171;" id="stat-blocked">0</div>
        <div class="metric-sub" id="stat-blocked-sub">Malware & Rejected</div>
      </div>

      <!-- Total AI Tokens -->
      <div class="metric-card">
        <div class="metric-card-header">
          <span class="metric-title">AI Token Usage</span>
          <div class="metric-icon" style="background: rgba(168, 85, 247, 0.15); color: #c084fc;">✨</div>
        </div>
        <div class="metric-value" style="color: #c084fc;" id="stat-tokens">0</div>
        <div class="metric-sub" id="stat-token-sub">Prompt: 0 | Thinking: 0 | Output: 0</div>
      </div>
    </div>

    <!-- Navigation Tabs -->
    <div class="tabs-bar">
      <div class="tab-buttons">
        <button class="tab-btn active" onclick="switchTab('history')">
          Processed History
          <span class="tab-badge" id="badge-history-count">0</span>
        </button>
        <button class="tab-btn" onclick="switchTab('tokens')">
          Token Usage History
          <span class="tab-badge" id="badge-token-count">0</span>
        </button>
        <button class="tab-btn" onclick="switchTab('analytics')">
          Model Analytics
        </button>
      </div>
    </div>

    <!-- TAB 1: Processed History -->
    <div id="tab-history" class="tab-pane">
      <div class="table-toolbar">
        <div style="display: flex; gap: 0.75rem; align-items: center; flex: 1; flex-wrap: wrap;">
          <div class="search-box">
            <span class="search-icon">🔍</span>
            <input type="text" id="history-search" placeholder="Filter by title, category, or reasoning..." oninput="filterHistory()">
          </div>

          <div class="filter-pills">
            <div class="filter-pill active" data-filter="all" onclick="setFilter('all')">All</div>
            <div class="filter-pill" data-filter="imported" onclick="setFilter('imported')">Imported</div>
            <div class="filter-pill" data-filter="dry_run" onclick="setFilter('dry_run')">Dry Run</div>
            <div class="filter-pill" data-filter="blocked" onclick="setFilter('blocked')">Blocked</div>
            <div class="filter-pill" data-filter="rejected" onclick="setFilter('rejected')">Rejected</div>
            <div class="filter-pill" data-filter="dismissed" onclick="setFilter('dismissed')">Dismissed</div>
          </div>
        </div>

        <button class="btn btn-warning" id="btn-retry-all" onclick="retryAllRejected()" title="Allow all rejected, blocked, and failed items to be re-evaluated on next queue scan">
          <span>🔄</span> Retry All Rejected
        </button>
      </div>

      <div class="card-table-wrapper">
        <table>
          <thead>
            <tr>
              <th style="width: 130px;">Time</th>
              <th style="width: 80px;">App</th>
              <th>Release / Item Title</th>
              <th style="width: 140px;">Category</th>
              <th style="width: 100px;">Status</th>
              <th style="width: 170px;">AI Model & Conf.</th>
              <th style="width: 100px; text-align: center;">Action</th>
              <th style="width: 35px;"></th>
            </tr>
          </thead>
          <tbody id="history-table-body">
            <tr>
              <td colspan="8" class="empty-state">
                <div class="empty-icon">⏳</div>
                <div>Waiting for data...</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- TAB 2: Token Usage History -->
    <div id="tab-tokens" class="tab-pane" style="display: none;">
      <div class="card-table-wrapper">
        <table>
          <thead>
            <tr>
              <th style="width: 105px;">Time</th>
              <th style="min-width: 130px;">Operation</th>
              <th style="width: 125px;">Model</th>
              <th style="width: 70px; text-align: right;">Prompt</th>
              <th style="width: 70px; text-align: right;">Thinking</th>
              <th style="width: 70px; text-align: right;">Output</th>
              <th style="width: 75px; text-align: right;">Total</th>
              <th style="width: 70px; text-align: right;">Latency</th>
              <th style="width: 75px; text-align: center;">Status</th>
            </tr>
          </thead>
          <tbody id="token-table-body">
            <tr>
              <td colspan="9" class="empty-state">
                <div class="empty-icon">✨</div>
                <div>No AI calls recorded yet.</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- TAB 3: Model Analytics -->
    <div id="tab-analytics" class="tab-pane" style="display: none;">
      <div class="analytics-grid">
        <!-- Models Breakdown -->
        <div class="analytics-card">
          <div class="analytics-card-title">
            <span>🤖</span> Usage by Model
          </div>
          <div id="analytics-models-list">
            <div class="empty-state">No model usage data recorded yet.</div>
          </div>
        </div>

        <!-- Operations Breakdown -->
        <div class="analytics-card">
          <div class="analytics-card-title">
            <span>⚡</span> Usage by Operation
          </div>
          <div id="analytics-operations-list">
            <div class="empty-state">No operation data recorded yet.</div>
          </div>
        </div>
      </div>
    </div>
  </main>

  <!-- Footer -->
  <footer>
    AImportarr &bull; In-Memory Metrics &bull; Lightweight Monitoring Server
  </footer>

  <script>
    let rawHistory = [];
    let rawTokens = [];
    let currentFilter = 'all';
    let refreshTimer = null;

    function formatNumber(num) {
      if (num === null || num === undefined) return '0';
      return Number(num).toLocaleString();
    }

    function formatTime(isoStr) {
      if (!isoStr) return '--';
      try {
        const d = new Date(isoStr);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      } catch (e) {
        return isoStr;
      }
    }

    function switchTab(tabName) {
      document.querySelectorAll('.tab-pane').forEach(el => el.style.display = 'none');
      document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));

      const targetPane = document.getElementById(`tab-${tabName}`);
      if (targetPane) targetPane.style.display = 'block';

      const targetBtn = Array.from(document.querySelectorAll('.tab-btn')).find(b =>
        b.getAttribute('onclick').includes(tabName)
      );
      if (targetBtn) targetBtn.classList.add('active');
    }

    function setFilter(status) {
      currentFilter = status;
      document.querySelectorAll('.filter-pill').forEach(el => {
        el.classList.toggle('active', el.dataset.filter === status);
      });
      filterHistory();
    }

    function filterHistory() {
      const q = (document.getElementById('history-search').value || '').toLowerCase();
      const filtered = rawHistory.filter(item => {
        const matchesFilter = (currentFilter === 'all') || (item.status === currentFilter);
        const matchesSearch = !q ||
          (item.title || '').toLowerCase().includes(q) ||
          (item.category || '').toLowerCase().includes(q) ||
          (item.reasoning || '').toLowerCase().includes(q) ||
          (item.model || '').toLowerCase().includes(q);
        return matchesFilter && matchesSearch;
      });
      renderHistoryTable(filtered);
    }

    function toggleReasoning(rowId) {
      const el = document.getElementById(`reasoning-${rowId}`);
      if (el) el.classList.toggle('open');
    }

    function renderHistoryTable(items) {
      const tbody = document.getElementById('history-table-body');
      if (!items || items.length === 0) {
        tbody.innerHTML = `
          <tr>
            <td colspan="8" class="empty-state">
              <div class="empty-icon">📭</div>
              <div>No processed items found.</div>
            </td>
          </tr>`;
        return;
      }

      tbody.innerHTML = items.map(item => {
        const confText = item.confidence !== null ? `${Math.round(item.confidence * 100)}%` : '--';
        const modelText = item.model || 'Direct / Rule';
        const appClass = (item.app || '').toLowerCase() === 'sonarr' ? 'badge-sonarr' : 'badge-radarr';
        const statusClass = `badge-${item.status || 'skipped'}`;

        let actionCell = '<span style="color: var(--text-dim); font-size: 0.75rem;">--</span>';
        if (['rejected', 'failed', 'blocked'].includes(item.status)) {
          if (item.retry_requested) {
            actionCell = '<span class="badge" style="background: rgba(245, 158, 11, 0.15); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.3); font-size: 0.65rem;">Pending Retry</span>';
          } else {
            actionCell = `<button class="btn btn-sm btn-retry" onclick="event.stopPropagation(); retryItem(${item.id})" title="Allow this release to be re-evaluated on next scan">🔄 Retry</button>`;
          }
        }

        return `
          <tr class="clickable-row" onclick="toggleReasoning(${item.id})">
            <td>${formatTime(item.timestamp)}</td>
            <td><span class="badge ${appClass}">${item.app || 'App'}</span></td>
            <td class="title-cell">
              <div class="title-main">${escapeHtml(item.title)}</div>
              ${item.details ? `<div class="title-sub">${escapeHtml(item.details)}</div>` : ''}
            </td>
            <td><span class="badge badge-category">${item.category}</span></td>
            <td><span class="badge ${statusClass}">${item.status}</span></td>
            <td>
              <div style="font-weight: 600;">${confText}</div>
              <div style="font-size: 0.7rem; color: var(--text-dim);">${escapeHtml(modelText)}</div>
            </td>
            <td style="text-align: center;" onclick="event.stopPropagation()">${actionCell}</td>
            <td style="color: var(--text-dim); font-size: 0.8rem;">▾</td>
          </tr>
          <tr id="reasoning-${item.id}" class="reasoning-row">
            <td colspan="8">
              <div class="reasoning-box">
                <div style="font-weight: 600; color: #a5b4fc; margin-bottom: 0.35rem;">
                  🤖 AI Reasoning & Decision Details
                </div>
                <div>${escapeHtml(item.reasoning || item.details || 'No reasoning details recorded.')}</div>
                <div style="font-size: 0.7rem; color: var(--text-dim); margin-top: 0.35rem;">
                  ID: #${item.id} &bull; Timestamp: ${item.timestamp} &bull; Model: ${escapeHtml(modelText)} &bull; Confidence: ${confText}
                </div>
              </div>
            </td>
          </tr>
        `;
      }).join('');
    }

    function renderTokenTable(tokens) {
      const tbody = document.getElementById('token-table-body');
      if (!tokens || tokens.length === 0) {
        tbody.innerHTML = `
          <tr>
            <td colspan="9" class="empty-state">
              <div class="empty-icon">✨</div>
              <div>No AI token calls recorded yet.</div>
            </td>
          </tr>`;
        return;
      }

      tbody.innerHTML = tokens.map(t => {
        const statusBadge = t.success
          ? '<span class="badge badge-imported">Success</span>'
          : '<span class="badge badge-blocked">Failed</span>';
        const latency = t.duration_ms ? `${t.duration_ms}ms` : '--';
        const thinkingHtml = (t.thinking_tokens && t.thinking_tokens > 0)
          ? `<span style="color: #a78bfa; font-feature-settings: 'tnum'; font-weight: 500;">${formatNumber(t.thinking_tokens)}</span>`
          : `<span style="color: var(--text-dim);">&mdash;</span>`;

        return `
          <tr>
            <td>${formatTime(t.timestamp)}</td>
            <td>
              <div style="font-weight: 600; font-family: monospace; font-size: 0.8rem;">${escapeHtml(t.operation)}</div>
              ${t.item_title ? `<div class="title-sub">${escapeHtml(t.item_title)}</div>` : ''}
            </td>
            <td><span style="color: #c084fc; font-weight: 500;">${escapeHtml(t.model)}</span></td>
            <td style="color: #60a5fa; font-feature-settings: 'tnum'; text-align: right;">${formatNumber(t.prompt_tokens)}</td>
            <td style="text-align: right;">${thinkingHtml}</td>
            <td style="color: #34d399; font-feature-settings: 'tnum'; text-align: right;">${formatNumber(t.completion_tokens)}</td>
            <td style="font-weight: 700; color: var(--text-main); font-feature-settings: 'tnum'; text-align: right;">${formatNumber(t.total_tokens)}</td>
            <td style="color: var(--text-dim); text-align: right;">${latency}</td>
            <td style="text-align: center;">${statusBadge}</td>
          </tr>
        `;
      }).join('');
    }

    function renderAnalytics(summary) {
      // Models List
      const modelsList = document.getElementById('analytics-models-list');
      const models = summary.tokens_by_model || {};
      const modelKeys = Object.keys(models);

      if (modelKeys.length === 0) {
        modelsList.innerHTML = '<div class="empty-state">No model usage recorded yet.</div>';
      } else {
        const totalAll = summary.total_tokens || 1;
        modelsList.innerHTML = modelKeys.map(k => {
          const m = models[k];
          const pct = Math.round((m.total_tokens / totalAll) * 100);
          return `
            <div class="bar-wrapper">
              <div class="bar-label">
                <span style="font-weight: 600; color: var(--text-main);">${escapeHtml(k)}</span>
                <span>${formatNumber(m.total_tokens)} tokens (${m.calls} calls)</span>
              </div>
              <div class="bar-track">
                <div class="bar-fill" style="width: ${pct}%;"></div>
              </div>
              <div style="font-size: 0.7rem; color: var(--text-dim); margin-top: 0.2rem;">
                Prompt: ${formatNumber(m.prompt_tokens)} &bull; Thinking: ${formatNumber(m.thinking_tokens || 0)} &bull; Output: ${formatNumber(m.completion_tokens)}
              </div>
            </div>
          `;
        }).join('');
      }

      // Operations List
      const opsList = document.getElementById('analytics-operations-list');
      const ops = summary.tokens_by_operation || {};
      const opKeys = Object.keys(ops);

      if (opKeys.length === 0) {
        opsList.innerHTML = '<div class="empty-state">No operation data recorded yet.</div>';
      } else {
        const totalAll = summary.total_tokens || 1;
        opsList.innerHTML = opKeys.map(k => {
          const op = ops[k];
          const pct = Math.round((op.total_tokens / totalAll) * 100);
          return `
            <div class="bar-wrapper">
              <div class="bar-label">
                <span style="font-weight: 600; font-family: monospace; font-size: 0.75rem;">${escapeHtml(k)}</span>
                <span>${formatNumber(op.total_tokens)} tokens (${op.calls} calls)</span>
              </div>
              <div class="bar-track">
                <div class="bar-fill" style="width: ${pct}%; background-color: #8b5cf6;"></div>
              </div>
            </div>
          `;
        }).join('');
      }
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    async function fetchData(isManual = false) {
      const refreshIcon = document.getElementById('refresh-icon');
      if (isManual && refreshIcon) refreshIcon.classList.add('spin');

      try {
        const [sumRes, histRes, tokRes] = await Promise.all([
          fetch('/api/metrics'),
          fetch('/api/history?limit=100'),
          fetch('/api/tokens?limit=100')
        ]);

        if (sumRes.ok) {
          const summary = await sumRes.json();
          document.getElementById('stat-uptime').innerText = summary.uptime_formatted || '--';
          document.getElementById('stat-scans').innerText = `Queue Scans: ${formatNumber(summary.total_scans)}`;
          document.getElementById('stat-processed').innerText = formatNumber(summary.total_items_processed);
          document.getElementById('stat-imported').innerText = formatNumber(summary.total_imported);
          document.getElementById('stat-dry-run').innerText = `${formatNumber(summary.total_dry_run)} in Dry Run`;
          document.getElementById('stat-blocked').innerText = formatNumber(summary.total_blocked + summary.total_rejected);
          document.getElementById('stat-blocked-sub').innerText = `${formatNumber(summary.total_blocked)} Blocked | ${formatNumber(summary.total_rejected)} Rejected`;
          document.getElementById('stat-tokens').innerText = formatNumber(summary.total_tokens);
          const pTok = formatNumber(summary.total_prompt_tokens || 0);
          const thTok = formatNumber(summary.total_thinking_tokens || 0);
          const outTok = formatNumber(summary.total_completion_tokens || 0);
          document.getElementById('stat-token-sub').innerText = `Prompt: ${pTok} | Thinking: ${thTok} | Output: ${outTok}`;
          renderAnalytics(summary);
        }

        if (histRes.ok) {
          rawHistory = await histRes.json();
          document.getElementById('badge-history-count').innerText = rawHistory.length;
          filterHistory();
        }

        if (tokRes.ok) {
          rawTokens = await tokRes.json();
          document.getElementById('badge-token-count').innerText = rawTokens.length;
          renderTokenTable(rawTokens);
        }

        document.getElementById('header-status').innerText = 'Online';
      } catch (err) {
        console.error('Failed to fetch metrics:', err);
        document.getElementById('header-status').innerText = 'Reconnecting...';
      } finally {
        if (refreshIcon) refreshIcon.classList.remove('spin');
      }
    }

    function updateAutoRefresh() {
      if (refreshTimer) clearInterval(refreshTimer);
      const val = parseInt(document.getElementById('refresh-interval').value, 10);
      if (val > 0) {
        refreshTimer = setInterval(() => fetchData(false), val);
      }
    }

    function showToast(msg, type = 'info') {
      let container = document.getElementById('toast-container');
      if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container';
        document.body.appendChild(container);
      }
      const el = document.createElement('div');
      el.className = `toast toast-${type}`;
      el.innerText = msg;
      container.appendChild(el);
      setTimeout(() => {
        el.style.opacity = '0';
        el.style.transition = 'opacity 0.3s ease';
        setTimeout(() => el.remove(), 300);
      }, 3500);
    }

    async function retryItem(id) {
      try {
        const res = await fetch('/api/retry', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: id })
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showToast(`Release #${id} marked for retry on next scan!`, 'success');
          const it = rawHistory.find(x => x.id === id);
          if (it) it.retry_requested = 1;
          filterHistory();
        } else {
          showToast(`Failed to retry item #${id}`, 'danger');
        }
      } catch (e) {
        console.error(e);
        showToast('Network error retrying item', 'danger');
      }
    }

    async function retryAllRejected() {
      if (!confirm('Mark all previously rejected, blocked, and failed items for retry on the next queue scan cycle?')) {
        return;
      }
      try {
        const res = await fetch('/api/retry', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ all: true })
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showToast(data.message || `Marked ${data.count || 0} items for retry!`, 'success');
          fetchData(true);
        } else {
          showToast('Failed to retry all rejected items', 'danger');
        }
      } catch (e) {
        console.error(e);
        showToast('Network error retrying items', 'danger');
      }
    }

    // Initialize
    fetchData();
    updateAutoRefresh();
  </script>
</body>
</html>
"""


class MetricsHTTPHandler(BaseHTTPRequestHandler):
    """HTTP Request handler for lightweight dashboard and REST metrics API."""

    def __init__(self, *args, metrics_instance: Optional[MetricsTracker] = None, **kwargs):
        self.metrics = metrics_instance or default_metrics
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        # Silence default standard error logging for HTTP requests
        logger.debug("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_str: str, status: int = 200) -> None:
        body = html_str.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File Not Found")
            return
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

        params = parse_qs(parsed.query)

        # Dashboard UI
        if path in ("/", "/index.html", "/dashboard", "/metrics"):
            self._send_html(DASHBOARD_HTML)
            return

        # Assets
        if path in ("/assets/logo.png", "/logo.png"):
            for candidate in [
                Path(__file__).resolve().parent.parent / "assets" / "logo.png",
                Path("assets/logo.png"),
                Path("/app/assets/logo.png"),
            ]:
                if candidate.exists():
                    self._send_file(candidate, "image/png")
                    return
            self.send_error(HTTPStatus.NOT_FOUND, "Logo Not Found")
            return

        # REST API: Summary
        if path in ("/api/metrics", "/api/summary"):
            self._send_json(self.metrics.get_summary())
            return

        # REST API: Processed History
        if path == "/api/history":
            limit = 100
            if "limit" in params:
                try:
                    limit = int(params["limit"][0])
                except (ValueError, IndexError):
                    pass
            self._send_json(self.metrics.get_processed_history(limit))
            return

        # REST API: Token Usage History
        if path == "/api/tokens":
            limit = 100
            if "limit" in params:
                try:
                    limit = int(params["limit"][0])
                except (ValueError, IndexError):
                    pass
            self._send_json(self.metrics.get_token_history(limit))
            return

        # REST API: Health
        if path in ("/api/health", "/health"):
            summary = self.metrics.get_summary()
            self._send_json({
                "status": "ok",
                "uptime_seconds": summary["uptime_seconds"],
                "total_scans": summary["total_scans"],
            })
            return

        # 404
        self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/retry":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                data = json.loads(body) if body.strip() else {}
            except Exception as e:
                logger.warning(f"Error parsing POST /api/retry payload: {e}")
                data = {}

            if data.get("all"):
                count = self.metrics.mark_all_rejected_for_retry()
                self._send_json({
                    "status": "ok",
                    "message": f"Marked {count} rejected item(s) for retry on next scan.",
                    "count": count
                })
                return

            item_id = data.get("id")
            if item_id is not None:
                try:
                    item_id = int(item_id)
                except ValueError:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Invalid item ID")
                    return
                success = self.metrics.mark_item_for_retry(item_id)
                if success:
                    self._send_json({
                        "status": "ok",
                        "message": f"Item #{item_id} marked for retry on next scan.",
                        "id": item_id,
                        "success": True
                    })
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, f"Item #{item_id} not found")
                return

            self.send_error(HTTPStatus.BAD_REQUEST, "Must specify 'id' or 'all: true'")
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")

class WebServerRunner:
    """Manager for the background ThreadingHTTPServer."""

    def __init__(self, host: str, port: int, metrics_instance: Optional[MetricsTracker] = None):
        self.host = host
        self.port = port
        self.metrics = metrics_instance or default_metrics
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        handler_factory = lambda *args, **kwargs: MetricsHTTPHandler(
            *args, metrics_instance=self.metrics, **kwargs
        )
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler_factory)
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"Web UI and Metrics server started at http://{self.host}:{self.port}")

    def stop(self) -> None:
        if self.httpd:
            logger.info("Stopping Web UI server...")
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
