import json
import logging
import math
import time
import urllib.request
import urllib.error
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse
from typing import Optional, Any, Callable, Dict, Tuple, List

from .config import AppConfig, load_config, save_config, config_to_dict, update_config_from_dict
from .metrics import MetricsTracker, metrics as default_metrics
from . import __version__

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

    html, body {
      overflow-x: hidden;
      max-width: 100vw;
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

    .version-tag {
      font-size: 0.68rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-weight: 600;
      padding: 0.12rem 0.45rem;
      border-radius: 4px;
      background: rgba(99, 102, 241, 0.15);
      color: #818cf8;
      border: 1px solid rgba(99, 102, 241, 0.3);
      line-height: 1.2;
      display: inline-block;
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

    .btn:disabled,
    .btn[disabled] {
      opacity: 0.35;
      cursor: not-allowed;
      pointer-events: none;
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

    /* Fixed Action Column & Stable Button Sizing */
    .col-action {
      width: 108px !important;
      min-width: 108px !important;
      max-width: 108px !important;
      text-align: center !important;
      box-sizing: border-box !important;
      white-space: nowrap !important;
    }

    .action-btn-slot {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 92px;
      height: 26px;
      box-sizing: border-box;
      vertical-align: middle;
    }

    .btn-retry,
    .badge-pending-retry {
      width: 92px !important;
      height: 26px !important;
      box-sizing: border-box !important;
      display: inline-flex !important;
      align-items: center !important;
      justify-content: center !important;
      padding: 0 0.4rem !important;
      font-size: 0.72rem !important;
      font-weight: 600 !important;
      line-height: 1 !important;
      white-space: nowrap !important;
      border-radius: 4px !important;
      margin: 0 !important;
      user-select: none;
      transition: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease;
    }

    .btn-retry {
      background-color: rgba(245, 158, 11, 0.12);
      border: 1px solid rgba(245, 158, 11, 0.35);
      color: #fbbf24;
      cursor: pointer;
    }

    .btn-retry:hover {
      background-color: rgba(245, 158, 11, 0.25);
      border-color: #f59e0b;
      color: #fff;
    }

    .btn-retry:active {
      transform: scale(0.98);
    }

    .badge-pending-retry {
      background-color: rgba(245, 158, 11, 0.16);
      border: 1px solid rgba(245, 158, 11, 0.45);
      color: #fbbf24;
      cursor: pointer;
    }

    .badge-pending-retry:hover {
      background-color: rgba(239, 68, 68, 0.2);
      border-color: #ef4444;
      color: #fca5a5;
    }

    .badge-pending-retry .cancel-hint {
      display: none;
    }

    .badge-pending-retry .queued-text {
      display: inline;
    }

    .badge-pending-retry:hover .cancel-hint {
      display: inline;
    }

    .badge-pending-retry:hover .queued-text {
      display: none;
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

    .btn-secondary {
      background-color: rgba(148, 163, 184, 0.12);
      border: 1px solid rgba(148, 163, 184, 0.3);
      color: var(--text-main);
    }

    .btn-secondary:hover {
      background-color: rgba(148, 163, 184, 0.22);
      border-color: rgba(148, 163, 184, 0.5);
    }

    .btn-danger {
      background-color: rgba(239, 68, 68, 0.12);
      border: 1px solid rgba(239, 68, 68, 0.35);
      color: #f87171;
    }

    .btn-danger:hover {
      background-color: rgba(239, 68, 68, 0.25);
      border-color: #ef4444;
      color: #fff;
    }

    .ollama-grid {
      display: grid;
      grid-template-columns: 1fr 380px;
      gap: 1rem;
      align-items: start;
    }

    @media (max-width: 900px) {
      .ollama-grid {
        grid-template-columns: 1fr;
      }
    }

    .ollama-connection-form {
      display: flex;
      gap: 0.75rem;
      align-items: flex-end;
      flex-wrap: wrap;
      margin-top: 0.85rem;
      padding-top: 0.85rem;
      border-top: 1px solid var(--border);
    }

    .ollama-connection-url-col {
      flex: 1;
      min-width: 220px;
    }

    .ollama-connection-model-col {
      width: 200px;
      min-width: 150px;
    }

    .ollama-connection-timeout-col {
      width: 110px;
      min-width: 90px;
    }

    .ollama-models-desktop {
      display: block;
      overflow-x: auto;
    }

    .ollama-models-mobile {
      display: none;
    }

    .ollama-model-mobile-card {
      background: rgba(0, 0, 0, 0.25);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.85rem;
      display: flex;
      flex-direction: column;
      gap: 0.55rem;
    }

    .ollama-model-mobile-card.active-model {
      border-color: rgba(34, 197, 94, 0.45);
      background: rgba(34, 197, 94, 0.04);
    }

    .ollama-preset-chip {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border);
      border-radius: 9999px;
      padding: 0.22rem 0.6rem;
      font-size: 0.73rem;
      color: var(--sonarr);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      word-break: break-all;
      transition: all 0.15s ease;
    }

    .ollama-preset-chip:hover {
      background: rgba(56, 189, 248, 0.15);
      border-color: var(--sonarr);
      color: #fff;
    }

    .ollama-unconfigured-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 0.85rem;
      margin-bottom: 1.25rem;
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

    /* Bottom Floating Save Bar for Settings */
    .settings-save-bar {
      position: fixed;
      bottom: 2rem;
      left: 50%;
      transform: translate(-50%, 150%);
      opacity: 0;
      pointer-events: none;
      z-index: 1000;
      background: rgba(21, 25, 33, 0.94);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid var(--accent);
      border-radius: 9999px;
      padding: 0.65rem 1.35rem;
      display: flex;
      align-items: center;
      gap: 1.5rem;
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.65), 0 0 24px var(--accent-glow);
      transition: transform 0.35s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.25s ease;
    }

    .settings-save-bar.visible {
      transform: translate(-50%, 0);
      opacity: 1;
      pointer-events: auto;
    }

    .settings-save-bar-info {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--text-main);
      white-space: nowrap;
    }

    .settings-save-bar-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--warning);
      box-shadow: 0 0 10px var(--warning);
      display: inline-block;
      animation: pulse-dot 2s infinite ease-in-out;
    }

    @keyframes pulse-dot {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.85); }
    }

    .settings-save-bar-actions {
      display: flex;
      align-items: center;
      gap: 0.6rem;
    }

    @media (max-width: 600px) {
      .settings-save-bar {
        bottom: 1rem;
        width: calc(100% - 2rem);
        border-radius: 12px;
        justify-content: space-between;
        gap: 0.75rem;
        padding: 0.65rem 0.85rem;
      }
      .settings-save-bar-info span:not(.settings-save-bar-dot) {
        font-size: 0.75rem;
      }
    }

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
      max-width: 1600px;
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

    /* Automation Tasks Panel */
    .automation-tasks-card {
      background: linear-gradient(180deg, rgba(30, 41, 59, 0.7) 0%, rgba(15, 23, 42, 0.7) 100%);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.95rem 1.15rem;
      margin-bottom: 1.5rem;
    }

    .automation-tasks-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 0.75rem;
      flex-wrap: wrap;
      gap: 0.5rem;
    }

    .automation-tasks-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 0.9rem;
    }

    .task-card {
      background-color: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.85rem 0.95rem;
      display: flex;
      flex-direction: column;
      gap: 0.6rem;
      transition: all 0.2s ease;
    }

    .task-card.running {
      border-color: rgba(99, 102, 241, 0.6);
      box-shadow: 0 0 16px rgba(99, 102, 241, 0.18);
    }

    .task-card-top {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 0.75rem;
    }

    .task-title-row {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-bottom: 0.2rem;
    }

    .task-name {
      font-size: 0.88rem;
      font-weight: 700;
      color: var(--text-main);
    }

    .task-desc {
      font-size: 0.73rem;
      color: var(--text-dim);
      line-height: 1.35;
    }

    .task-badge-idle {
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }

    .task-badge-running {
      background: rgba(99, 102, 241, 0.2);
      color: #a5b4fc;
      border: 1px solid rgba(99, 102, 241, 0.5);
    }

    .task-badge-disabled {
      background: rgba(148, 163, 184, 0.1);
      color: var(--text-dim);
      border: 1px solid rgba(148, 163, 184, 0.2);
    }

    .task-progress-box {
      background: rgba(99, 102, 241, 0.08);
      border: 1px dashed rgba(99, 102, 241, 0.35);
      border-radius: 6px;
      padding: 0.4rem 0.65rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.75rem;
      color: #c7d2fe;
    }

    .task-spinner {
      width: 13px;
      height: 13px;
      border: 2px solid rgba(99, 102, 241, 0.3);
      border-top-color: #818cf8;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      flex-shrink: 0;
    }

    .task-meta-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      padding-top: 0.4rem;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      font-size: 0.72rem;
      color: var(--text-muted);
      flex-wrap: wrap;
    }

    .task-meta-label {
      color: var(--text-dim);
    }

    .task-meta-value {
      font-weight: 600;
      color: var(--text-main);
    }

    /* Tabs Bar */
    .tabs-bar {
      display: flex;
      align-items: center;
      border-bottom: 1px solid var(--border);
      margin-bottom: 1.25rem;
      gap: 1rem;
      overflow-x: auto;
      overflow-y: hidden;
      white-space: nowrap;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
    }

    .tabs-bar::-webkit-scrollbar {
      display: none;
    }

    .tab-buttons {
      display: flex;
      gap: 0.5rem;
      flex-wrap: nowrap;
      min-width: max-content;
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
      white-space: nowrap;
      flex-shrink: 0;
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

    .new-items-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      background: var(--accent);
      color: #fff;
      font-size: 0.78rem;
      font-weight: 500;
      padding: 0.35rem 0.85rem;
      border-radius: 20px;
      margin-bottom: 0.75rem;
      cursor: pointer;
      box-shadow: 0 4px 14px var(--accent-glow);
      transition: all 0.2s ease;
      animation: slideDownFade 0.25s ease-out;
    }

    .new-items-pill:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 18px rgba(99, 102, 241, 0.4);
    }

    @keyframes slideDownFade {
      from { opacity: 0; transform: translateY(-6px); }
      to { opacity: 1; transform: translateY(0); }
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

    .history-table-wrapper,
    .token-table-wrapper {
      display: block;
    }

    .history-mobile-cards,
    .token-mobile-cards {
      display: none;
    }

    .history-mobile-card,
    .token-mobile-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.85rem;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      transition: background 0.15s ease, border-color 0.15s ease;
    }

    .history-mobile-card:hover,
    .token-mobile-card:hover {
      border-color: #3b4556;
      background: var(--bg-card-hover);
    }

    .reasoning-card-panel,
    .token-card-panel {
      display: none;
    }

    .reasoning-card-panel.open,
    .token-card-panel.open {
      display: block;
      animation: fadeIn 0.15s ease;
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

    /* Table Pagination */
    .table-pagination {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      margin-top: 0.85rem;
      padding: 0.4rem 0.25rem;
      flex-wrap: wrap;
    }

    .pagination-info {
      font-size: 0.78rem;
      color: var(--text-muted);
    }

    .pagination-info span {
      font-weight: 600;
      color: var(--text-main);
    }

    .pagination-controls {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }

    .pagination-select {
      background-color: var(--bg-card);
      border: 1px solid var(--border);
      color: var(--text-main);
      padding: 0.28rem 0.55rem;
      border-radius: 6px;
      font-size: 0.75rem;
      outline: none;
      cursor: pointer;
    }

    .pagination-select:focus {
      border-color: var(--accent);
    }

    .pagination-page-label {
      font-size: 0.78rem;
      color: var(--text-muted);
      padding: 0 0.35rem;
      user-select: none;
    }

    .pagination-page-label span {
      font-weight: 600;
      color: var(--text-main);
    }

    .pagination-btn {
      width: 28px;
      height: 28px;
      padding: 0;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      line-height: 1;
      flex-shrink: 0;
    }

    .pagination-btn svg {
      width: 13px;
      height: 13px;
      display: block;
      pointer-events: none;
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
    .badge-failed { background: rgba(245, 158, 11, 0.15); color: #f59e0b; }

    .badge-sonarr { background: rgba(56, 189, 248, 0.15); color: var(--sonarr); }
    .badge-radarr { background: rgba(251, 146, 60, 0.15); color: var(--radarr); }

    .badge-category {
      background: rgba(139, 92, 246, 0.12);
      color: #c084fc;
      border: 1px solid rgba(139, 92, 246, 0.25);
      font-size: 0.68rem;
    }

    .title-cell {
      min-width: 320px;
      word-break: break-word;
      overflow-wrap: anywhere;
    }

    .title-main {
      font-weight: 600;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 0.4rem;
      word-break: break-word;
      overflow-wrap: anywhere;
    }

    .title-sub {
      font-size: 0.725rem;
      color: var(--text-dim);
      margin-top: 0.15rem;
      word-break: break-word;
      overflow-wrap: anywhere;
    }

    .reasoning-row,
    .token-detail-row {
      display: none;
      background-color: #0d1017;
    }

    .reasoning-row.open,
    .token-detail-row.open {
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

    .debug-msg-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.85rem;
      margin-top: 0.5rem;
    }

    @media (max-width: 900px) {
      .debug-msg-grid {
        grid-template-columns: 1fr;
      }
    }

    .debug-msg-panel {
      background-color: #0b0e14;
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
    }

    .debug-msg-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0.4rem 0.75rem;
      background-color: #161b26;
      border-bottom: 1px solid var(--border);
      font-size: 0.72rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .debug-msg-pre {
      margin: 0;
      padding: 0.65rem 0.75rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.75rem;
      line-height: 1.45;
      color: #e2e8f0;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 340px;
      overflow-y: auto;
    }

    .debug-error-banner {
      background: rgba(239, 68, 68, 0.12);
      border: 1px solid rgba(239, 68, 68, 0.35);
      border-radius: 6px;
      padding: 0.65rem 0.85rem;
      margin-top: 0.5rem;
      margin-bottom: 0.65rem;
    }

    .debug-error-title {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.82rem;
      font-weight: 700;
      color: #f87171;
      margin-bottom: 0.3rem;
    }

    .debug-error-code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.75rem;
      background: rgba(239, 68, 68, 0.25);
      color: #fecaca;
      padding: 0.1rem 0.4rem;
      border-radius: 4px;
      border: 1px solid rgba(239, 68, 68, 0.4);
    }

    .debug-error-reason {
      font-size: 0.76rem;
      color: #fca5a5;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
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

    /* Settings Form Styles */
    .settings-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 1.25rem;
      margin-bottom: 1.5rem;
    }

    .form-group {
      margin-bottom: 1rem;
    }

    .form-group:last-child {
      margin-bottom: 0;
    }

    .form-label {
      display: block;
      font-size: 0.78rem;
      font-weight: 600;
      color: var(--text-main);
      margin-bottom: 0.3rem;
    }

    .form-hint {
      font-size: 0.72rem;
      color: var(--text-dim);
      margin-top: 0.25rem;
    }

    .form-input {
      width: 100%;
      background-color: #0e1218;
      border: 1px solid var(--border);
      color: var(--text-main);
      padding: 0.5rem 0.75rem;
      border-radius: 6px;
      font-size: 0.825rem;
      outline: none;
    }

    .form-input:focus {
      border-color: var(--accent);
    }

    .form-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding: 0.55rem 0;
      border-bottom: 1px solid var(--border-subtle);
    }

    .form-row:last-child {
      border-bottom: none;
    }

    .automation-params-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.65rem;
    }

    .path-mapping-row {
      display: flex;
      gap: 0.45rem;
      align-items: center;
    }

    .path-mapping-arrow {
      color: var(--text-dim);
      flex-shrink: 0;
    }

    .toggle-switch {
      position: relative;
      display: inline-block;
      width: 40px;
      height: 22px;
      flex-shrink: 0;
    }

    .toggle-switch input {
      opacity: 0;
      width: 0;
      height: 0;
    }

    .toggle-slider {
      position: absolute;
      cursor: pointer;
      top: 0; left: 0; right: 0; bottom: 0;
      background-color: #2b3545;
      transition: .2s;
      border-radius: 22px;
    }

    .toggle-slider:before {
      position: absolute;
      content: "";
      height: 16px;
      width: 16px;
      left: 3px;
      bottom: 3px;
      background-color: white;
      transition: .2s;
      border-radius: 50%;
    }

    .toggle-switch input:checked + .toggle-slider {
      background-color: var(--accent);
    }

    .toggle-switch input:checked + .toggle-slider:before {
      transform: translateX(18px);
    }

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
        min-width: 250px;
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
      .history-table-wrapper,
      .token-table-wrapper {
        display: none !important;
      }
      .history-mobile-cards,
      .token-mobile-cards {
        display: flex !important;
        flex-direction: column;
        gap: 0.65rem;
      }
      .filter-pills {
        overflow-x: auto;
        white-space: nowrap;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;
        padding-bottom: 0.25rem;
        max-width: 100%;
        flex-wrap: nowrap;
      }
      .filter-pills::-webkit-scrollbar {
        display: none;
      }
      .filter-pill {
        flex-shrink: 0;
        white-space: nowrap;
      }
      .ollama-models-desktop {
        display: none !important;
      }
      .ollama-models-mobile {
        display: flex !important;
        flex-direction: column;
        gap: 0.65rem;
      }
    }

    @media (max-width: 600px) {
      header {
        padding: 0.6rem 0.75rem;
      }
      .brand {
        gap: 0.5rem;
      }
      .brand-title {
        font-size: 1rem;
      }
      .brand-subtitle {
        display: none;
      }
      .header-actions {
        gap: 0.4rem;
      }
      .header-refresh-label {
        display: none;
      }
      .header-btn-text {
        display: none;
      }
      .status-badge {
        padding: 0.25rem 0.45rem;
      }
      .status-badge #header-status {
        display: none;
      }
      #refresh-btn {
        padding: 0.35rem 0.55rem;
      }
      main {
        padding: 0.75rem 0.65rem;
      }
      .metrics-grid {
        grid-template-columns: repeat(2, 1fr);
        gap: 0.6rem;
        margin-bottom: 1rem;
      }
      .metric-card {
        padding: 0.75rem 0.8rem;
      }
      .metric-card:last-child {
        grid-column: span 2;
      }
      .metric-title {
        font-size: 0.68rem;
      }
      .metric-value {
        font-size: 1.35rem;
        margin-bottom: 0.2rem;
      }
      .metric-sub {
        font-size: 0.68rem;
      }
      .automation-tasks-grid {
        grid-template-columns: 1fr;
        gap: 0.75rem;
      }
      .automation-tasks-card {
        padding: 0.85rem;
        margin-bottom: 1rem;
      }
      .task-card {
        padding: 0.75rem;
      }
      .task-card-top {
        flex-direction: column;
        align-items: stretch;
        gap: 0.6rem;
      }
      .task-card-top button {
        width: 100%;
        justify-content: center;
      }
      .task-meta-row {
        flex-direction: column;
        align-items: flex-start;
        gap: 0.35rem;
      }
      .tabs-bar {
        margin-bottom: 1rem;
      }
      .tab-buttons {
        gap: 0.35rem;
      }
      .tab-btn {
        padding: 0.5rem 0.75rem;
        font-size: 0.8rem;
      }
      .settings-grid {
        grid-template-columns: 1fr;
        gap: 1rem;
      }
      .analytics-grid {
        grid-template-columns: 1fr;
        gap: 1rem;
      }
      .analytics-card {
        padding: 0.95rem;
      }
      .automation-params-grid {
        grid-template-columns: 1fr !important;
        gap: 0.5rem;
      }
      .path-mapping-row {
        flex-direction: column;
        align-items: stretch;
        gap: 0.35rem;
        background: rgba(0, 0, 0, 0.2);
        padding: 0.6rem;
        border-radius: 6px;
      }
      .path-mapping-arrow {
        text-align: center;
        line-height: 1;
        font-size: 0.8rem;
      }
      .ollama-connection-form {
        flex-direction: column;
        align-items: stretch;
        gap: 0.65rem;
      }
      .ollama-connection-url-col,
      .ollama-connection-model-col,
      .ollama-connection-timeout-col {
        width: 100% !important;
        min-width: 0 !important;
      }
      .ollama-connection-form button {
        width: 100%;
        justify-content: center;
      }
      .ollama-unconfigured-grid {
        grid-template-columns: 1fr !important;
      }
      .table-pagination {
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 0.6rem;
        text-align: center;
      }
      .pagination-controls {
        justify-content: center;
      }
      #btn-retry-all {
        width: 100%;
        justify-content: center;
      }
    }

    /* Modal: Clear History */
    .modal-backdrop {
      position: fixed;
      top: 0;
      left: 0;
      width: 100vw;
      height: 100vh;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(4px);
      z-index: 9999;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1rem;
    }
    .modal-box {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      width: 100%;
      max-width: 480px;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .modal-header {
      padding: 1.15rem 1.4rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .modal-title {
      font-size: 1rem;
      font-weight: 600;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .modal-body {
      padding: 1.25rem 1.4rem;
      font-size: 0.85rem;
      color: var(--text-muted);
      line-height: 1.5;
    }
    .modal-footer {
      padding: 1rem 1.4rem;
      border-top: 1px solid var(--border);
      background: rgba(0, 0, 0, 0.15);
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 0.75rem;
    }
    .modal-checkbox-group {
      margin-top: 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
      background: var(--bg-base);
      padding: 0.85rem 1rem;
      border-radius: 8px;
      border: 1px solid var(--border-subtle);
    }
    .modal-checkbox-label {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      color: var(--text-main);
      font-size: 0.825rem;
      cursor: pointer;
    }
    .modal-checkbox-label input[type="checkbox"] {
      accent-color: var(--danger);
      width: 16px;
      height: 16px;
      cursor: pointer;
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
        <div style="display: flex; align-items: center; gap: 0.45rem;">
          <div class="brand-title">AImportarr</div>
          <span class="version-tag">v{{VERSION}}</span>
        </div>
        <div class="brand-subtitle">AI-Powered *arr Media Importer</div>
      </div>
    </a>

    <div class="header-actions">
      <div class="status-badge">
        <span class="pulse-dot"></span>
        <span id="header-status">Online</span>
      </div>

      <div style="display: flex; align-items: center; gap: 0.4rem;">
        <label for="refresh-interval" class="header-refresh-label" style="font-size: 0.75rem; color: var(--text-muted);">Stats update:</label>
        <select id="refresh-interval" class="input-select" onchange="updateAutoRefresh()" title="Background polling frequency for metric counters (tables only reload on demand)">
          <option value="15000" selected>15s</option>
          <option value="30000">30s</option>
          <option value="60000">1m</option>
          <option value="0">Off</option>
        </select>
      </div>

      <button id="refresh-btn" class="btn" onclick="fetchData(true)" title="Refresh data">
        <span id="refresh-icon">🔄</span><span class="header-btn-text"> Refresh</span>
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

      <!-- Rejected / Dismissed -->
      <div class="metric-card">
        <div class="metric-card-header">
          <span class="metric-title">Rejected &amp; Dismissed</span>
          <div class="metric-icon" style="background: var(--danger-bg); color: var(--danger);">🛡</div>
        </div>
        <div class="metric-value" style="color: #f87171;" id="stat-blocked">0</div>
        <div class="metric-sub" id="stat-blocked-sub">0 Rejected (Deleted) | 0 Dismissed (Kept in Client)</div>
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

    <!-- Background Automation Tasks Status & Triggers -->
    <div class="automation-tasks-card">
      <div class="automation-tasks-header">
        <div style="display: flex; align-items: center; gap: 0.5rem;">
          <span style="font-size: 1rem;">⚡</span>
          <span style="font-weight: 700; font-size: 0.88rem; color: var(--text-main);">Background Automation Tasks</span>
        </div>
        <div style="font-size: 0.74rem; color: var(--text-dim);">
          Scheduled Sonarr library backfill and missing episode search runs
        </div>
      </div>
      <div class="automation-tasks-grid">
        <!-- Season Pack Backfill Task Card -->
        <div class="task-card" id="task-backfill-card">
          <div class="task-card-top">
            <div class="task-info">
              <div class="task-title-row">
                <span class="task-name">📦 Season Pack Backfill</span>
                <span class="badge task-badge-idle" id="task-backfill-badge">Idle</span>
              </div>
              <div class="task-desc">Scans completed seasons with missing episodes to grab full season packs</div>
            </div>
            <button class="btn btn-sm" id="btn-trigger-backfill" onclick="triggerBackfillNow()" title="Run Season Pack Backfill scan now">
              <span>▶</span> Run Backfill
            </button>
          </div>
          <div class="task-progress-box" id="task-backfill-progress-box" style="display: none;">
            <div class="task-spinner"></div>
            <span class="task-progress-text" id="task-backfill-progress-text">Scanning library...</span>
          </div>
          <div class="task-meta-row">
            <div><span class="task-meta-label">Last Run:</span> <span class="task-meta-value" id="task-backfill-last-run">Never</span></div>
            <div><span class="task-meta-label">Last Grabbed:</span> <span class="task-meta-value" id="task-backfill-last-grabbed">0 packs</span></div>
            <div><span class="task-meta-label">Next Scheduled:</span> <span class="task-meta-value" id="task-backfill-next-run">--</span></div>
          </div>
        </div>

        <!-- Missing Episode Retry Task Card -->
        <div class="task-card" id="task-episode-retry-card">
          <div class="task-card-top">
            <div class="task-info">
              <div class="task-title-row">
                <span class="task-name">🔎 Missing Episode Retry</span>
                <span class="badge task-badge-idle" id="task-episode-retry-badge">Idle</span>
              </div>
              <div class="task-desc">Bulk season tracker search + AI matching for ongoing seasons with missing aired episodes</div>
            </div>
            <button class="btn btn-sm" id="btn-trigger-episode-retry" onclick="triggerEpisodeRetryNow()" title="Run Missing Episode Retry scan now">
              <span>▶</span> Retry Episodes
            </button>
          </div>
          <div class="task-progress-box" id="task-episode-retry-progress-box" style="display: none;">
            <div class="task-spinner"></div>
            <span class="task-progress-text" id="task-episode-retry-progress-text">Scanning library...</span>
          </div>
          <div class="task-meta-row">
            <div><span class="task-meta-label">Last Run:</span> <span class="task-meta-value" id="task-episode-retry-last-run">Never</span></div>
            <div><span class="task-meta-label">Last Grabbed:</span> <span class="task-meta-value" id="task-episode-retry-last-grabbed">0 episodes</span></div>
            <div><span class="task-meta-label">Next Scheduled:</span> <span class="task-meta-value" id="task-episode-retry-next-run">--</span></div>
          </div>
        </div>

        <!-- Missing Movie Search Task Card (Radarr) -->
        <div class="task-card" id="task-movie-search-card">
          <div class="task-card-top">
            <div class="task-info">
              <div class="task-title-row">
                <span class="task-name">🎬 Missing Movie Search</span>
                <span class="badge task-badge-idle" id="task-movie-search-badge">Idle</span>
              </div>
              <div class="task-desc">Interactive search + AI release matching for monitored missing movies in Radarr</div>
            </div>
            <button class="btn btn-sm" id="btn-trigger-movie-search" onclick="triggerMovieSearchNow()" title="Run Missing Movie Search scan now">
              <span>▶</span> Search Movies
            </button>
          </div>
          <div class="task-progress-box" id="task-movie-search-progress-box" style="display: none;">
            <div class="task-spinner"></div>
            <span class="task-progress-text" id="task-movie-search-progress-text">Scanning library...</span>
          </div>
          <div class="task-meta-row">
            <div><span class="task-meta-label">Last Run:</span> <span class="task-meta-value" id="task-movie-search-last-run">Never</span></div>
            <div><span class="task-meta-label">Last Grabbed:</span> <span class="task-meta-value" id="task-movie-search-last-grabbed">0 movies</span></div>
            <div><span class="task-meta-label">Next Scheduled:</span> <span class="task-meta-value" id="task-movie-search-next-run">--</span></div>
          </div>
        </div>
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
        <button class="tab-btn" onclick="switchTab('ollama')">
          Ollama Models
        </button>
        <button class="tab-btn" onclick="switchTab('settings')">
          Settings
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
            <div class="filter-pill" data-filter="imported" onclick="setFilter('imported')" title="Imported into Sonarr/Radarr media library">Imported</div>
            <div class="filter-pill" data-filter="skipped" onclick="setFilter('skipped')" title="Skipped (no release found, cooldown applied, or review needed)">Skipped</div>
            <div class="filter-pill" data-filter="rejected" onclick="setFilter('rejected')" title="Deleted from download client &amp; blocklisted (malware/corrupt)">Rejected</div>
            <div class="filter-pill" data-filter="dismissed" onclick="setFilter('dismissed')" title="Ignored/removed from *arr queue, files kept in download client">Dismissed</div>
            <div class="filter-pill" data-filter="failed" onclick="setFilter('failed')" title="Import or grab errors (eligible for retry)">Failed</div>
          </div>
        </div>

        <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
          <button class="btn btn-warning" id="btn-retry-all" onclick="retryAllRejected()" title="Re-evaluate all items still sitting in the queue on the next scan">
            <span>🔄</span> Retry All In Queue
          </button>
          <button class="btn btn-danger" id="btn-clear-history" onclick="openClearHistoryModal()" title="Clear processed history &amp; database">
            <span>🗑️</span> Clear History
          </button>
        </div>
      </div>

      <!-- Floating New Items Indicator -->
      <div id="history-new-items-pill" class="new-items-pill" style="display: none;" onclick="loadNewProcessedItems()" title="Click to view new processed items">
        <span>↑</span> <span id="history-new-items-text">New processed items available</span> &bull; <strong>Click to view</strong>
      </div>

      <!-- Desktop Table View (>= 768px) -->
      <div class="card-table-wrapper history-table-wrapper">
        <table>
          <thead>
            <tr>
              <th style="width: 115px;">Date &amp; Time</th>
              <th style="width: 70px;">App</th>
              <th style="min-width: 320px;">Release / Item Title</th>
              <th style="width: 120px;">Category</th>
              <th style="width: 90px;">Status</th>
              <th style="width: 140px;">AI Model & Conf.</th>
              <th class="col-action" style="width: 108px; min-width: 108px; max-width: 108px; text-align: center;">Action</th>
              <th style="width: 30px;"></th>
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

      <!-- Mobile Cards View (< 768px) -->
      <div id="history-cards-container" class="history-mobile-cards">
        <div class="empty-state">
          <div class="empty-icon">⏳</div>
          <div>Waiting for data...</div>
        </div>
      </div>

      <!-- Pagination Toolbar for Processed History -->
      <div class="table-pagination">
        <div class="pagination-info">
          Showing <span id="history-page-start">1</span>–<span id="history-page-end">10</span> of <span id="history-total-items">0</span> items
        </div>
        <div class="pagination-controls">
          <label style="font-size: 0.75rem; color: var(--text-dim); display: flex; align-items: center; gap: 0.35rem;">
            Per page:
            <select id="history-page-size" class="pagination-select" onchange="changeHistoryPageSize(this.value)">
              <option value="10" selected>10</option>
              <option value="25">25</option>
              <option value="50">50</option>
              <option value="100">100</option>
            </select>
          </label>
          <button class="btn btn-sm btn-secondary pagination-btn" id="history-btn-first" onclick="goToHistoryPage(1)" title="First Page"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="11 19 4 12 11 5"></polyline><polyline points="19 19 12 12 19 5"></polyline></svg></button>
          <button class="btn btn-sm btn-secondary pagination-btn" id="history-btn-prev" onclick="goToHistoryPage(historyCurrentPage - 1)" title="Previous Page"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg></button>
          <span class="pagination-page-label">Page <span id="history-current-page">1</span> of <span id="history-total-pages">1</span></span>
          <button class="btn btn-sm btn-secondary pagination-btn" id="history-btn-next" onclick="goToHistoryPage(historyCurrentPage + 1)" title="Next Page"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg></button>
          <button class="btn btn-sm btn-secondary pagination-btn" id="history-btn-last" onclick="goToHistoryPage(historyTotalPages)" title="Last Page"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="5 5 12 12 5 19"></polyline><polyline points="13 5 20 12 13 19"></polyline></svg></button>
        </div>
      </div>
    </div>

    <!-- TAB 2: Token Usage History -->
    <div id="tab-tokens" class="tab-pane" style="display: none;">
      <div class="table-toolbar">
        <div style="display: flex; gap: 0.75rem; align-items: center; flex: 1; flex-wrap: wrap;">
          <div class="search-box">
            <span class="search-icon">🔍</span>
            <input type="text" id="tokens-search" placeholder="Filter by operation, model, or release..." oninput="filterTokens()">
          </div>
        </div>

        <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
          <button class="btn btn-danger" id="btn-clear-tokens" onclick="openClearHistoryModal()" title="Clear database history &amp; tokens">
            <span>🗑️</span> Clear History
          </button>
        </div>
      </div>

      <!-- Floating New Items Indicator -->
      <div id="tokens-new-items-pill" class="new-items-pill" style="display: none;" onclick="loadNewTokenItems()" title="Click to view latest AI calls">
        <span>↑</span> <span id="tokens-new-items-text">New AI calls recorded</span> &bull; <strong>Click to view</strong>
      </div>

      <!-- Desktop Table View (>= 768px) -->
      <div class="card-table-wrapper token-table-wrapper">
        <table>
          <thead>
            <tr>
              <th style="width: 115px;">Date &amp; Time</th>
              <th style="min-width: 260px;">Operation / Release Title</th>
              <th style="width: 120px;">Model</th>
              <th style="width: 65px; text-align: right;">Prompt</th>
              <th style="width: 65px; text-align: right;">Thinking</th>
              <th style="width: 65px; text-align: right;">Output</th>
              <th style="width: 70px; text-align: right;">Total</th>
              <th style="width: 65px; text-align: right;">Latency</th>
              <th style="width: 75px; text-align: center;">Status</th>
              <th style="width: 30px;"></th>
            </tr>
          </thead>
          <tbody id="token-table-body">
            <tr>
              <td colspan="10" class="empty-state">
                <div class="empty-icon">✨</div>
                <div>No AI calls recorded yet.</div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Mobile Cards View (< 768px) -->
      <div id="token-cards-container" class="token-mobile-cards">
        <div class="empty-state">
          <div class="empty-icon">✨</div>
          <div>No AI calls recorded yet.</div>
        </div>
      </div>

      <!-- Pagination Toolbar for Token Usage History -->
      <div class="table-pagination">
        <div class="pagination-info">
          Showing <span id="tokens-page-start">1</span>–<span id="tokens-page-end">10</span> of <span id="tokens-total-items">0</span> transactions
        </div>
        <div class="pagination-controls">
          <label style="font-size: 0.75rem; color: var(--text-dim); display: flex; align-items: center; gap: 0.35rem;">
            Per page:
            <select id="tokens-page-size" class="pagination-select" onchange="changeTokensPageSize(this.value)">
              <option value="10" selected>10</option>
              <option value="25">25</option>
              <option value="50">50</option>
              <option value="100">100</option>
            </select>
          </label>
          <button class="btn btn-sm btn-secondary pagination-btn" id="tokens-btn-first" onclick="goToTokensPage(1)" title="First Page"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="11 19 4 12 11 5"></polyline><polyline points="19 19 12 12 19 5"></polyline></svg></button>
          <button class="btn btn-sm btn-secondary pagination-btn" id="tokens-btn-prev" onclick="goToTokensPage(tokensCurrentPage - 1)" title="Previous Page"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg></button>
          <span class="pagination-page-label">Page <span id="tokens-current-page">1</span> of <span id="tokens-total-pages">1</span></span>
          <button class="btn btn-sm btn-secondary pagination-btn" id="tokens-btn-next" onclick="goToTokensPage(tokensCurrentPage + 1)" title="Next Page"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg></button>
          <button class="btn btn-sm btn-secondary pagination-btn" id="tokens-btn-last" onclick="goToTokensPage(tokensTotalPages)" title="Last Page"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="5 5 12 12 5 19"></polyline><polyline points="13 5 20 12 13 19"></polyline></svg></button>
        </div>
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

    <!-- TAB: Ollama Model Manager -->
    <div id="tab-ollama" class="tab-pane" style="display: none;">
      <!-- Connection & Status Bar -->
      <div class="analytics-card" style="margin-bottom: 1rem;">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem;">
          <div style="display: flex; align-items: center; gap: 0.75rem;">
            <span style="font-size: 1.6rem;">🦙</span>
            <div>
              <div style="font-weight: 600; font-size: 1.05rem;">Ollama Host &amp; Model Engine</div>
              <div class="form-hint" id="ollama-status-url">Configured Host: http://localhost:11434</div>
            </div>
          </div>
          <div style="display: flex; align-items: center; gap: 0.6rem;">
            <span id="ollama-conn-status" class="status-badge" style="background: rgba(148, 163, 184, 0.12); color: var(--text-muted); font-size: 0.8rem; padding: 0.35rem 0.75rem;">○ Checking...</span>
            <button class="btn btn-secondary btn-sm" onclick="loadOllamaModels(true)">🔄 Refresh</button>
          </div>
        </div>

        <!-- Host URL, Active Model & Timeout Configuration Bar -->
        <div class="ollama-connection-form">
          <div class="ollama-connection-url-col">
            <label class="form-label" for="cfg-ollama-url" style="margin-bottom: 0.25rem; font-size: 0.75rem;">Ollama Base URL</label>
            <input type="text" id="cfg-ollama-url" class="form-input" placeholder="http://localhost:11434" value="http://localhost:11434" onchange="updateOllamaSummaryCards()">
          </div>
          <div class="ollama-connection-model-col">
            <label class="form-label" for="cfg-ollama-model" style="margin-bottom: 0.25rem; font-size: 0.75rem;">Active Model Tag</label>
            <input type="text" id="cfg-ollama-model" class="form-input" placeholder="qwen2.5:7b" value="qwen2.5:7b" oninput="updateOllamaSummaryCards()">
          </div>
          <div class="ollama-connection-timeout-col">
            <label class="form-label" for="cfg-ollama-timeout" style="margin-bottom: 0.25rem; font-size: 0.75rem;">Timeout (s)</label>
            <input type="number" id="cfg-ollama-timeout" class="form-input" min="5" max="900" step="5" placeholder="60" value="60" oninput="updateOllamaSummaryCards()" title="HTTP timeout in seconds for Ollama inference calls (increase for slower hardware)">
          </div>
          <div>
            <button type="button" class="btn btn-primary btn-sm" onclick="saveOllamaSettings()">Save &amp; Connect</button>
          </div>
        </div>
        <div class="form-hint" style="margin-top: 0.5rem; font-size: 0.75rem; display: flex; align-items: center; gap: 0.45rem; flex-wrap: wrap;">
          <span>Host Presets:</span>
          <a href="javascript:void(0)" onclick="setOllamaHostPreset('http://localhost:11434')" class="ollama-preset-chip">localhost:11434</a>
          <a href="javascript:void(0)" onclick="setOllamaHostPreset('http://host.docker.internal:11434')" class="ollama-preset-chip">host.docker.internal:11434 (Docker bridge)</a>
        </div>
      </div>

      <div class="ollama-grid">
        <!-- Left: Installed Models List -->
        <div class="analytics-card">
          <div class="analytics-card-title">
            <span>📦</span> Installed Models on Ollama
          </div>
          <div id="ollama-models-container">
            <div style="text-align: center; padding: 2rem; color: var(--text-dim);">Loading models from Ollama...</div>
          </div>
        </div>

        <!-- Right: Pull New Model -->
        <div class="analytics-card">
          <div class="analytics-card-title">
            <span>⬇️</span> Pull &amp; Install Model
          </div>
          <p class="form-hint" style="margin-bottom: 0.75rem;">
            Download models directly onto your Ollama server from the official Ollama library.
          </p>

          <div class="form-group">
            <label class="form-label" for="ollama-pull-tag">Model Tag</label>
            <input type="text" id="ollama-pull-tag" class="form-input" placeholder="e.g. qwen2.5:7b">
          </div>

          <div style="margin-bottom: 1rem;">
            <div class="form-hint" style="margin-bottom: 0.4rem;">Quick Select:</div>
            <div style="display: flex; flex-wrap: wrap; gap: 0.35rem;">
              <button type="button" class="btn btn-secondary btn-sm" onclick="setPullTag('qwen2.5:7b')">qwen2.5:7b ⭐</button>
              <button type="button" class="btn btn-secondary btn-sm" onclick="setPullTag('qwen2.5:3b')">qwen2.5:3b (Lite)</button>
              <button type="button" class="btn btn-secondary btn-sm" onclick="setPullTag('deepseek-r1:7b')">deepseek-r1:7b (CoT)</button>
              <button type="button" class="btn btn-secondary btn-sm" onclick="setPullTag('deepseek-r1:8b')">deepseek-r1:8b</button>
              <button type="button" class="btn btn-secondary btn-sm" onclick="setPullTag('llama3.1:8b')">llama3.1:8b</button>
            </div>
          </div>

          <button id="btn-pull-model" class="btn btn-primary" style="width: 100%; justify-content: center;" onclick="startOllamaPull()">
            <span>⬇️</span> Pull Model from Ollama
          </button>

          <!-- Pull Progress Bar -->
          <div id="ollama-pull-progress-box" style="display: none; margin-top: 1rem; padding: 0.85rem; background: rgba(0, 0, 0, 0.25); border: 1px solid var(--border); border-radius: 8px;">
            <div style="display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 0.4rem;">
              <span id="ollama-pull-status-text" style="font-weight: 500;">Downloading...</span>
              <span id="ollama-pull-percent-text" style="color: var(--sonarr); font-weight: 600;">0%</span>
            </div>
            <div style="width: 100%; height: 8px; background: rgba(255, 255, 255, 0.1); border-radius: 4px; overflow: hidden; margin-bottom: 0.4rem;">
              <div id="ollama-pull-progress-bar" style="width: 0%; height: 100%; background: var(--sonarr); transition: width 0.3s ease;"></div>
            </div>
            <div id="ollama-pull-detail-text" style="font-size: 0.75rem; color: var(--text-dim);">Connecting to Ollama...</div>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 5: Settings / Configuration -->
    <div id="tab-settings" class="tab-pane" style="display: none;">
      <div class="table-toolbar">
        <div>
          <div style="display: flex; align-items: center; gap: 0.6rem;">
            <div style="font-size: 1rem; font-weight: 700;">System Configuration</div>
            <span class="version-tag">v{{VERSION}}</span>
          </div>
          <div style="font-size: 0.78rem; color: var(--text-muted);">Changes are saved to your config file and hot-reloaded immediately without restarting the container.</div>
        </div>
        <div style="display: flex; gap: 0.6rem;">
          <button class="btn btn-secondary btn-sm" onclick="loadConfigFromApi()" title="Reload settings from disk">↺ Reload</button>
        </div>
      </div>

      <div class="settings-grid">
        <!-- General & Pipeline Policy -->
        <div class="analytics-card">
          <div class="analytics-card-title"><span>🎛</span> General & Two-Stage Policy</div>

          <div class="form-row">
            <div>
              <div class="form-label" style="margin-bottom: 0;">Dry Run (Simulation Mode)</div>
              <div class="form-hint">Log decisions without modifying Sonarr/Radarr or deleting files</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="cfg-dry-run">
              <span class="toggle-slider"></span>
            </label>
          </div>

          <div class="form-row" style="margin-bottom: 0.85rem;">
            <div>
              <div class="form-label" style="margin-bottom: 0;">Hands-Off Low Confidence Rejection</div>
              <div class="form-hint">Off (Conservative): leave low-confidence items in queue. On (Hands-Off): blocklist &amp; re-search while keeping files in client.</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="cfg-reject-low-conf">
              <span class="toggle-slider"></span>
            </label>
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-min-confidence">Minimum AI Confidence (0.00 - 1.00)</label>
            <input type="number" step="0.05" min="0" max="1" id="cfg-min-confidence" class="form-input" value="0.85">
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-check-interval">Background Queue Sync Interval (seconds)</label>
            <input type="number" min="5" id="cfg-check-interval" class="form-input" value="30">
            <div class="form-hint">Lightweight local check (0 AI tokens). Webhooks &amp; Retry trigger immediately.</div>
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-ai-cooldown">FIFO AI Rate-Limit Cooldown (seconds)</label>
            <input type="number" step="0.5" min="0" id="cfg-ai-cooldown" class="form-input" value="4.0">
            <div class="form-hint">Minimum pause between AI model calls in the FIFO worker to prevent rate-limit bursts</div>
          </div>

          <div class="form-group">
            <label class="form-label">On-Demand Webhook URL (Sonarr &amp; Radarr)</label>
            <input type="text" id="cfg-webhook-url" class="form-input" readonly onclick="this.select()" value="http://localhost:9890/api/webhook">
            <div class="form-hint">
              Add in Sonarr/Radarr (<strong>Settings &rarr; Connect &rarr; + &rarr; Webhook</strong>, Method: <code>POST</code>) for instant 0s event-driven processing.<br>
              <strong>Triggers to enable:</strong> In both Sonarr &amp; Radarr, check <strong>On Manual Interaction Required</strong>. In Sonarr only, also check <strong>On Import Complete</strong> (for partial season packs). Leave all other triggers unchecked.
            </div>
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-log-level">Log Level</label>
            <select id="cfg-log-level" class="form-input">
              <option value="INFO">INFO</option>
              <option value="DEBUG">DEBUG</option>
              <option value="WARNING">WARNING</option>
              <option value="ERROR">ERROR</option>
            </select>
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-timezone">System Timezone</label>
            <input type="text" id="cfg-timezone" class="form-input" placeholder="e.g. America/Los_Angeles, Europe/London (default: TZ env or UTC)">
            <div class="form-hint">Used for cron schedules and logs. Leave blank to inherit from container <code>TZ</code> environment variable or UTC.</div>
          </div>
        </div>

        <!-- AI Provider Configuration -->
        <div class="analytics-card">
          <div class="analytics-card-title"><span>✨</span> AI Provider (Gemini &amp; Ollama)</div>

          <div class="form-group">
            <label class="form-label" for="cfg-ai-provider">Primary AI Provider</label>
            <select id="cfg-ai-provider" class="form-input">
              <option value="gemini">Google Gemini</option>
              <option value="ollama">Ollama (Local)</option>
            </select>
          </div>

          <div class="form-row" style="margin-bottom: 0.85rem;">
            <div>
              <div class="form-label" style="margin-bottom: 0;">Automatic Fallback Provider</div>
              <div class="form-hint">Fall back to secondary provider if primary provider fails</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="cfg-ai-fallback">
              <span class="toggle-slider"></span>
            </label>
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-gemini-key">Gemini API Key</label>
            <input type="password" id="cfg-gemini-key" class="form-input" placeholder="AIza...">
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-gemini-model">Gemini Model</label>
            <input type="text" id="cfg-gemini-model" class="form-input" placeholder="auto">
            <div class="form-hint">Use "auto" to dynamically select the fastest active Flash model</div>
          </div>

          <!-- Consolidated Ollama Local Engine Summary in Settings -->
          <div style="background: rgba(0, 0, 0, 0.25); border: 1px solid var(--border); border-radius: 8px; padding: 0.85rem; margin-top: 0.75rem;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.6rem;">
              <div style="display: flex; align-items: center; gap: 0.65rem;">
                <span style="font-size: 1.35rem;">🦙</span>
                <div>
                  <div style="font-weight: 600; font-size: 0.88rem;">Ollama Local Engine</div>
                  <div class="form-hint" style="font-size: 0.75rem; margin-top: 0.1rem;">
                    Host: <span id="summary-ollama-host">http://localhost:11434</span> &bull; Active: <code id="summary-ollama-model">qwen2.5:7b</code> &bull; Timeout: <span id="summary-ollama-timeout">60</span>s
                  </div>
                </div>
              </div>
              <div style="display: flex; align-items: center; gap: 0.5rem;">
                <span id="settings-ollama-badge" class="status-badge" style="background: rgba(148, 163, 184, 0.12); color: var(--text-muted); font-size: 0.75rem; padding: 0.25rem 0.55rem;">○ Offline</span>
                <button type="button" class="btn btn-secondary btn-sm" onclick="switchTab('ollama')">
                  🦙 Configure Host &amp; Manage Models &rarr;
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- Sonarr & Radarr Connections -->
        <div class="analytics-card">
          <div class="analytics-card-title"><span>📺</span> Sonarr &amp; Radarr Connections</div>

          <div class="form-row">
            <div>
              <div class="form-label" style="margin-bottom: 0; color: var(--sonarr);">Enable Sonarr</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="cfg-sonarr-enabled">
              <span class="toggle-slider"></span>
            </label>
          </div>

          <div class="form-group" style="margin-top: 0.6rem;">
            <label class="form-label" for="cfg-sonarr-url">Sonarr URL</label>
            <input type="text" id="cfg-sonarr-url" class="form-input" placeholder="http://localhost:8989">
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-sonarr-key">Sonarr API Key</label>
            <input type="password" id="cfg-sonarr-key" class="form-input" placeholder="Sonarr API Key">
          </div>

          <hr style="border: none; border-top: 1px solid var(--border); margin: 1rem 0;">

          <div class="form-row">
            <div>
              <div class="form-label" style="margin-bottom: 0; color: var(--radarr);">Enable Radarr</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="cfg-radarr-enabled">
              <span class="toggle-slider"></span>
            </label>
          </div>

          <div class="form-group" style="margin-top: 0.6rem;">
            <label class="form-label" for="cfg-radarr-url">Radarr URL</label>
            <input type="text" id="cfg-radarr-url" class="form-input" placeholder="http://localhost:7878">
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-radarr-key">Radarr API Key</label>
            <input type="password" id="cfg-radarr-key" class="form-input" placeholder="Radarr API Key">
          </div>
        </div>

        <!-- Sonarr Automated Search & Backfill -->
        <div class="analytics-card">
          <div class="analytics-card-title"><span>⚡</span> Sonarr Automated Backfill &amp; Episode Retry</div>
          <div class="form-hint" style="margin-bottom: 1rem;">Automated background routines to recover missing episodes and completed seasons from Sonarr indexers.</div>

          <div style="background: rgba(56, 189, 248, 0.05); border: 1px solid rgba(56, 189, 248, 0.2); border-radius: 8px; padding: 0.85rem; margin-bottom: 0.85rem;">
            <div class="form-row" style="margin-bottom: 0.65rem;">
              <div>
                <div class="form-label" style="margin-bottom: 0; color: var(--sonarr);">📦 Season Pack Backfill</div>
                <div class="form-hint">Force-grab qualifying Season Packs for completed seasons with missing episodes when Sonarr rejects the pack solely because other episodes on disk have higher quality</div>
              </div>
              <label class="toggle-switch">
                <input type="checkbox" id="cfg-sonarr-backfill-enabled">
                <span class="toggle-slider"></span>
              </label>
            </div>

            <div class="automation-params-grid">
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-sonarr-backfill-cron">Cron Schedule</label>
                <input type="text" id="cfg-sonarr-backfill-cron" class="form-input" value="0 2 * * *" placeholder="0 2 * * *">
                <div class="form-hint" style="font-size:0.75rem;">Standard 5-part cron (e.g. 0 2 * * * = daily at 2 AM)</div>
              </div>
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-sonarr-backfill-missing-days">Post-Finale Grace (days)</label>
                <input type="number" min="0" id="cfg-sonarr-backfill-missing-days" class="form-input" value="3">
              </div>
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-sonarr-backfill-max-seasons">Max Seasons / Run</label>
                <input type="number" min="1" id="cfg-sonarr-backfill-max-seasons" class="form-input" value="3">
              </div>
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-sonarr-backfill-cooldown">Season Cooldown (days)</label>
                <input type="number" min="1" id="cfg-sonarr-backfill-cooldown" class="form-input" value="7">
              </div>
            </div>

            <div style="margin-top: 0.35rem; display: flex; justify-content: flex-end;">
              <button type="button" class="btn btn-sm" onclick="triggerBackfillNow()" title="Run an immediate Season Pack Backfill check in the background">📦 Run Backfill Now</button>
            </div>
          </div>

          <div style="background: rgba(99, 102, 241, 0.05); border: 1px solid rgba(99, 102, 241, 0.2); border-radius: 8px; padding: 0.85rem;">
            <div class="form-row" style="margin-bottom: 0.65rem;">
              <div>
                <div class="form-label" style="margin-bottom: 0; color: var(--sonarr);">🔎 Missing Episode Retry (Ongoing Seasons)</div>
                <div class="form-hint">Periodically search and grab missing aired episodes for seasons that are still airing (includes AI fallback for 'Unknown Series' episode releases)</div>
              </div>
              <label class="toggle-switch">
                <input type="checkbox" id="cfg-sonarr-ep-retry-enabled">
                <span class="toggle-slider"></span>
              </label>
            </div>

            <div class="automation-params-grid">
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-sonarr-ep-retry-cron">Cron Schedule</label>
                <input type="text" id="cfg-sonarr-ep-retry-cron" class="form-input" value="0 */6 * * *" placeholder="0 */6 * * *">
                <div class="form-hint" style="font-size:0.75rem;">Standard 5-part cron (e.g. 0 */6 * * * = every 6 hours)</div>
              </div>
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-sonarr-ep-retry-delay">Min Post-Air Delay (hours)</label>
                <input type="number" min="0" id="cfg-sonarr-ep-retry-delay" class="form-input" value="6">
              </div>
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-sonarr-ep-retry-max">Max Episodes / Run</label>
                <input type="number" min="1" id="cfg-sonarr-ep-retry-max" class="form-input" value="5">
              </div>
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-sonarr-ep-retry-cooldown">Episode Cooldown (hours)</label>
                <input type="number" min="1" id="cfg-sonarr-ep-retry-cooldown" class="form-input" value="24">
              </div>
            </div>

            <div style="margin-top: 0.35rem; display: flex; justify-content: flex-end;">
              <button type="button" class="btn btn-sm" onclick="triggerEpisodeRetryNow()" title="Run an immediate Missing Episode Retry check for ongoing seasons">🔎 Retry Missing Episodes Now</button>
            </div>
          </div>
        </div>

        <!-- Radarr Automated Movie Search -->
        <div class="analytics-card">
          <div class="analytics-card-title"><span>🎬</span> Radarr Automated Movie Search</div>
          <div class="form-hint" style="margin-bottom: 1rem;">Automated background routine to search and grab released missing movies from Radarr indexers with AI fallback.</div>

          <div style="background: rgba(251, 146, 60, 0.05); border: 1px solid rgba(251, 146, 60, 0.2); border-radius: 8px; padding: 0.85rem;">
            <div class="form-row" style="margin-bottom: 0.65rem;">
              <div>
                <div class="form-label" style="margin-bottom: 0; color: var(--radarr);">🎬 Missing Movie Search</div>
                <div class="form-hint">Search indexers for monitored missing movies, evaluating accepted releases and using AI for 'Unknown Movie' or foreign/alias title rejections</div>
              </div>
              <label class="toggle-switch">
                <input type="checkbox" id="cfg-radarr-movie-search-enabled">
                <span class="toggle-slider"></span>
              </label>
            </div>

            <div class="automation-params-grid">
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-radarr-movie-search-cron">Cron Schedule</label>
                <input type="text" id="cfg-radarr-movie-search-cron" class="form-input" value="0 3 * * *" placeholder="0 3 * * *">
                <div class="form-hint" style="font-size:0.75rem;">Standard 5-part cron (e.g. 0 3 * * * = daily at 3 AM)</div>
              </div>
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-radarr-movie-search-delay">Post-Release Grace (days)</label>
                <input type="number" min="0" id="cfg-radarr-movie-search-delay" class="form-input" value="1">
              </div>
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-radarr-movie-search-max">Max Movies / Run</label>
                <input type="number" min="1" id="cfg-radarr-movie-search-max" class="form-input" value="5">
              </div>
              <div class="form-group" style="margin-bottom: 0.5rem;">
                <label class="form-label" for="cfg-radarr-movie-search-cooldown">Movie Cooldown (days)</label>
                <input type="number" min="1" id="cfg-radarr-movie-search-cooldown" class="form-input" value="7">
              </div>
            </div>

            <div style="margin-top: 0.35rem; display: flex; justify-content: flex-end;">
              <button type="button" class="btn btn-sm" onclick="triggerMovieSearchNow()" title="Run an immediate Missing Movie Search check for Radarr in the background">🎬 Search Missing Movies Now</button>
            </div>
          </div>
        </div>

        <!-- Archive Extraction, Database & Path Mappings -->
        <div class="analytics-card">
          <div class="analytics-card-title"><span>📦</span> Archive Extraction &amp; Storage</div>

          <div class="form-row">
            <div>
              <div class="form-label" style="margin-bottom: 0;">Enable Archive Extraction</div>
              <div class="form-hint">Inspects and unpacks .rar/.zip/.7z archives into a temporary staging folder while keeping original archives intact</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="cfg-extract-enabled">
              <span class="toggle-slider"></span>
            </label>
          </div>

          <div class="form-row" style="margin-bottom: 0.85rem;">
            <div>
              <div class="form-label" style="margin-bottom: 0;">Delete Extracted Staging Folder After Import</div>
              <div class="form-hint">Clean up temporary unpacked video files once imported</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="cfg-extract-delete">
              <span class="toggle-slider"></span>
            </label>
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-extract-suffix">Staging Folder Suffix</label>
            <input type="text" id="cfg-extract-suffix" class="form-input" value="_extracted">
          </div>

          <div class="form-group">
            <label class="form-label" for="cfg-db-retention">Database History Retention (days)</label>
            <input type="number" min="0" id="cfg-db-retention" class="form-input" value="30">
          </div>

          <div class="form-group" style="display: flex; align-items: center; justify-content: space-between; padding-top: 0.5rem; border-top: 1px solid var(--border-subtle); margin-bottom: 1rem;">
            <div>
              <div class="form-label" style="margin-bottom: 0;">Database Maintenance</div>
              <div class="form-hint">Purge historical records and reset queue deduplication state</div>
            </div>
            <button type="button" class="btn btn-danger btn-sm" onclick="openClearHistoryModal()">🗑️ Clear Database History</button>
          </div>

          <div class="form-group">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.4rem;">
              <label class="form-label" style="margin-bottom: 0;">Path Mappings (Remote *arr &rarr; Local Container)</label>
              <button type="button" class="btn btn-sm" onclick="addPathMappingRow('', '')">+ Add Mapping</button>
            </div>
            <div id="cfg-path-mappings-list" style="display: flex; flex-direction: column; gap: 0.45rem;"></div>
          </div>
        </div>
      </div>
    </div>
  </main>

  <!-- Footer -->
  <footer>
    AImportarr <span class="version-tag" style="margin: 0 0.25rem;">v{{VERSION}}</span> &bull; AI-Powered *arr Queue Companion
  </footer>

  <!-- Modal: Clear Database History -->
  <div id="modal-clear-history" class="modal-backdrop" style="display: none;" onclick="if(event.target===this)closeClearHistoryModal()">
    <div class="modal-box">
      <div class="modal-header">
        <div class="modal-title">
          <span>🗑️</span> Clear Database &amp; History
        </div>
        <button type="button" class="btn btn-sm btn-secondary" onclick="closeClearHistoryModal()" style="padding: 0.2rem 0.5rem; border-radius: 4px;">✕</button>
      </div>
      <div class="modal-body">
        <p>This action will purge historical records from the SQLite database and in-memory caches. Clearing processed items allows any stuck or previously processed items in Sonarr / Radarr queues to be re-evaluated as brand new unattempted items on the next scan.</p>
        <div class="modal-checkbox-group">
          <label class="modal-checkbox-label">
            <input type="checkbox" id="clear-opt-items" checked>
            <span><strong>Processed Item History</strong> (releases, decisions, deduplication keys)</span>
          </label>
          <label class="modal-checkbox-label">
            <input type="checkbox" id="clear-opt-tokens" checked>
            <span><strong>Token Usage History</strong> (AI prompt &amp; response debug logs)</span>
          </label>
          <label class="modal-checkbox-label">
            <input type="checkbox" id="clear-opt-counters">
            <span><strong>Reset Lifetime Counters</strong> (reset dashboard stat cards to 0)</span>
          </label>
        </div>
        <p style="margin-top: 0.75rem; font-size: 0.775rem; color: #ef4444;">
          ⚠️ <em>Media files in your library and download client will <strong>not</strong> be touched.</em>
        </p>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary btn-sm" onclick="closeClearHistoryModal()">Cancel</button>
        <button type="button" class="btn btn-danger btn-sm" id="btn-confirm-clear" onclick="submitClearHistory()">Clear Selected History</button>
      </div>
    </div>
  </div>

  <!-- Floating Save Bar (pops up from bottom when any settings are modified) -->
  <div id="settings-save-bar" class="settings-save-bar">
    <div class="settings-save-bar-info">
      <span class="settings-save-bar-dot"></span>
      <span>Unsaved configuration changes</span>
    </div>
    <div class="settings-save-bar-actions">
      <button type="button" class="btn btn-secondary btn-sm" onclick="discardSettingsChanges()">Discard</button>
      <button type="button" class="btn btn-primary btn-sm" onclick="saveConfigToApi()">💾 Save Changes</button>
    </div>
  </div>

  <script>
    let rawHistory = [];
    let rawTokens = [];
    let historyTotal = 0;
    let historyCurrentPage = 1;
    let historyPageSize = 10;
    let historyTotalPages = 1;

    let tokensTotal = 0;
    let tokensCurrentPage = 1;
    let tokensPageSize = 10;
    let tokensTotalPages = 1;

    let lastKnownHistoryTotal = null;
    let lastKnownTokensTotal = null;
    let lastKnownGlobalHistoryTotal = null;
    let lastKnownGlobalTokensTotal = null;

    let currentFilter = 'all';
    let historySearchDebounce = null;
    let tokensSearchDebounce = null;
    let refreshTimer = null;
    const openReasoningIds = new Set();
    const openTokenIds = new Set();

    function hasHistoryChanged(oldItems, newItems) {
      if (!oldItems || !newItems) return true;
      if (oldItems.length !== newItems.length) return true;
      for (let i = 0; i < oldItems.length; i++) {
        if (oldItems[i].id !== newItems[i].id ||
            oldItems[i].status !== newItems[i].status ||
            oldItems[i].retry_requested !== newItems[i].retry_requested) {
          return true;
        }
      }
      return false;
    }

    function hasTokensChanged(oldTokens, newTokens) {
      if (!oldTokens || !newTokens) return true;
      if (oldTokens.length !== newTokens.length) return true;
      for (let i = 0; i < oldTokens.length; i++) {
        if (oldTokens[i].id !== newTokens[i].id ||
            oldTokens[i].success !== newTokens[i].success ||
            oldTokens[i].duration_ms !== newTokens[i].duration_ms) {
          return true;
        }
      }
      return false;
    }

    function formatNumber(num) {
      if (num === null || num === undefined) return '0';
      return Number(num).toLocaleString();
    }

    function parseUtcDate(isoStr) {
      if (!isoStr) return null;
      let s = String(isoStr).trim();
      if (!s.endsWith('Z') && !/[+-]\\d{2}(:\\d{2})?$/.test(s)) {
        s += 'Z';
      }
      const d = new Date(s);
      return isNaN(d.getTime()) ? null : d;
    }

    function formatDate(isoStr) {
      const d = parseUtcDate(isoStr);
      if (!d) return '--';
      try {
        return d.toLocaleDateString([], { year: 'numeric', month: '2-digit', day: '2-digit' });
      } catch (_) { return isoStr; }
    }

    function formatTime(isoStr) {
      const d = parseUtcDate(isoStr);
      if (!d) return '--';
      try {
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      } catch (_) { return isoStr; }
    }

    function formatDateTime(isoStr) {
      const d = parseUtcDate(isoStr);
      if (!d) return '--';
      try {
        return `${d.toLocaleDateString([], { year: 'numeric', month: '2-digit', day: '2-digit' })} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
      } catch (_) { return isoStr; }
    }

    function formatDateTimeCell(isoStr) {
      const d = parseUtcDate(isoStr);
      if (!d) return '--';
      try {
        const dStr = d.toLocaleDateString([], { year: 'numeric', month: '2-digit', day: '2-digit' });
        const tStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        return `<div style="font-weight: 500; font-feature-settings: 'tnum'; white-space: nowrap;">${dStr}</div><div style="font-size: 0.72rem; color: var(--text-dim); font-feature-settings: 'tnum'; white-space: nowrap;">${tStr}</div>`;
      } catch (_) { return escapeHtml(isoStr); }
    }

    function switchTab(tabName) {
      document.querySelectorAll('.tab-pane').forEach(el => el.style.display = 'none');
      document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));

      const targetPane = document.getElementById(`tab-${tabName}`);
      if (targetPane) targetPane.style.display = 'block';

      const targetBtn = Array.from(document.querySelectorAll('.tab-btn')).find(b =>
        b.getAttribute('onclick').includes(tabName)
      );
      if (targetBtn) {
        targetBtn.classList.add('active');
        try {
          targetBtn.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
        } catch (_) {}
      }

      if (tabName === 'settings') {
        if (!checkSettingsDirty()) {
          loadConfigFromApi();
        }
      }
      if (tabName === 'ollama') {
        loadOllamaModels(false);
      }
      updateSettingsDirtyUI();
    }

    function goToHistoryPage(page) {
      if (page < 1 || (historyTotalPages > 0 && page > historyTotalPages)) return;
      historyCurrentPage = page;
      fetchHistoryData(true);
    }

    function changeHistoryPageSize(newSize) {
      historyPageSize = parseInt(newSize, 10) || 10;
      historyCurrentPage = 1;
      fetchHistoryData(true);
    }

    function goToTokensPage(page) {
      if (page < 1 || (tokensTotalPages > 0 && page > tokensTotalPages)) return;
      tokensCurrentPage = page;
      fetchTokensData(true);
    }

    function changeTokensPageSize(newSize) {
      tokensPageSize = parseInt(newSize, 10) || 10;
      tokensCurrentPage = 1;
      fetchTokensData(true);
    }

    function setFilter(status) {
      currentFilter = status;
      historyCurrentPage = 1;
      document.querySelectorAll('.filter-pill').forEach(el => {
        el.classList.toggle('active', el.dataset.filter === status);
      });
      fetchHistoryData(true);
    }

    function filterHistory() {
      if (historySearchDebounce) clearTimeout(historySearchDebounce);
      historySearchDebounce = setTimeout(() => {
        historyCurrentPage = 1;
        fetchHistoryData(true);
      }, 250);
    }

    function filterTokens() {
      if (tokensSearchDebounce) clearTimeout(tokensSearchDebounce);
      tokensSearchDebounce = setTimeout(() => {
        tokensCurrentPage = 1;
        fetchTokensData(true);
      }, 250);
    }

    function loadNewProcessedItems() {
      const pill = document.getElementById('history-new-items-pill');
      if (pill) pill.style.display = 'none';

      // Reset to page 1
      historyCurrentPage = 1;

      // Reset filter to 'all' so new items (regardless of status/category) are visible
      currentFilter = 'all';
      document.querySelectorAll('.filter-pill').forEach(el => {
        el.classList.toggle('active', el.dataset.filter === 'all');
      });

      // Clear search box so newly arrived items are not hidden
      const sBox = document.getElementById('history-search');
      if (sBox) sBox.value = '';

      fetchHistoryData(true);

      const tableWrapper = document.querySelector('.history-table-wrapper');
      if (tableWrapper) {
        tableWrapper.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    }

    function loadNewTokenItems() {
      const pill = document.getElementById('tokens-new-items-pill');
      if (pill) pill.style.display = 'none';

      // Reset to page 1
      tokensCurrentPage = 1;

      // Clear search box so newly arrived calls are not hidden
      const sBox = document.getElementById('tokens-search');
      if (sBox) sBox.value = '';

      fetchTokensData(true);

      const tableWrapper = document.querySelector('.token-table-wrapper');
      if (tableWrapper) {
        tableWrapper.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    }

    function updateHistoryPaginationUI() {
      const start = historyTotal === 0 ? 0 : (historyCurrentPage - 1) * historyPageSize + 1;
      const end = Math.min(historyCurrentPage * historyPageSize, historyTotal);

      const startEl = document.getElementById('history-page-start');
      const endEl = document.getElementById('history-page-end');
      const totEl = document.getElementById('history-total-items');
      const curEl = document.getElementById('history-current-page');
      const totpEl = document.getElementById('history-total-pages');

      if (startEl) startEl.innerText = formatNumber(start);
      if (endEl) endEl.innerText = formatNumber(end);
      if (totEl) totEl.innerText = formatNumber(historyTotal);
      if (curEl) curEl.innerText = formatNumber(historyCurrentPage);
      if (totpEl) totpEl.innerText = formatNumber(historyTotalPages);

      const btnFirst = document.getElementById('history-btn-first');
      const btnPrev = document.getElementById('history-btn-prev');
      const btnNext = document.getElementById('history-btn-next');
      const btnLast = document.getElementById('history-btn-last');

      if (btnFirst) btnFirst.disabled = historyCurrentPage <= 1;
      if (btnPrev) btnPrev.disabled = historyCurrentPage <= 1;
      if (btnNext) btnNext.disabled = historyCurrentPage >= historyTotalPages;
      if (btnLast) btnLast.disabled = historyCurrentPage >= historyTotalPages;
    }

    function updateTokensPaginationUI() {
      const start = tokensTotal === 0 ? 0 : (tokensCurrentPage - 1) * tokensPageSize + 1;
      const end = Math.min(tokensCurrentPage * tokensPageSize, tokensTotal);

      const startEl = document.getElementById('tokens-page-start');
      const endEl = document.getElementById('tokens-page-end');
      const totEl = document.getElementById('tokens-total-items');
      const curEl = document.getElementById('tokens-current-page');
      const totpEl = document.getElementById('tokens-total-pages');

      if (startEl) startEl.innerText = formatNumber(start);
      if (endEl) endEl.innerText = formatNumber(end);
      if (totEl) totEl.innerText = formatNumber(tokensTotal);
      if (curEl) curEl.innerText = formatNumber(tokensCurrentPage);
      if (totpEl) totpEl.innerText = formatNumber(tokensTotalPages);

      const btnFirst = document.getElementById('tokens-btn-first');
      const btnPrev = document.getElementById('tokens-btn-prev');
      const btnNext = document.getElementById('tokens-btn-next');
      const btnLast = document.getElementById('tokens-btn-last');

      if (btnFirst) btnFirst.disabled = tokensCurrentPage <= 1;
      if (btnPrev) btnPrev.disabled = tokensCurrentPage <= 1;
      if (btnNext) btnNext.disabled = tokensCurrentPage >= tokensTotalPages;
      if (btnLast) btnLast.disabled = tokensCurrentPage >= tokensTotalPages;
    }

    async function fetchHistoryData(forceRender = false) {
      try {
        const q = (document.getElementById('history-search')?.value || '').trim();
        const params = new URLSearchParams({
          page: historyCurrentPage,
          limit: historyPageSize,
          filter: currentFilter,
        });
        if (q) params.set('search', q);

        const res = await fetch(`/api/history?${params.toString()}`);
        if (!res.ok) return;
        const data = await res.json();

        let newItems = [];
        if (Array.isArray(data)) {
          newItems = data;
          historyTotal = data.length;
          historyTotalPages = Math.ceil(historyTotal / historyPageSize) || 1;
        } else {
          newItems = data.items || [];
          historyTotal = data.total || 0;
          historyCurrentPage = data.page || 1;
          historyPageSize = data.limit || historyPageSize;
          historyTotalPages = data.total_pages || 1;
        }

        const changed = hasHistoryChanged(rawHistory, newItems);
        rawHistory = newItems;
        lastKnownHistoryTotal = historyTotal;

        const isViewingLatestHistory = (currentFilter === 'all' && historyCurrentPage === 1 && !q);
        if (typeof data.global_total === 'number') {
          if (isViewingLatestHistory || lastKnownGlobalHistoryTotal === null) {
            lastKnownGlobalHistoryTotal = data.global_total;
          }
        } else if (isViewingLatestHistory && (lastKnownGlobalHistoryTotal === null || historyTotal >= (lastKnownGlobalHistoryTotal || 0))) {
          lastKnownGlobalHistoryTotal = historyTotal;
        }

        if (isViewingLatestHistory) {
          const pill = document.getElementById('history-new-items-pill');
          if (pill) pill.style.display = 'none';
        }

        const badgeCount = document.getElementById('badge-history-count');
        if (badgeCount) badgeCount.innerText = formatNumber(historyTotal);
        updateHistoryPaginationUI();

        const hasSelection = Boolean(window.getSelection && window.getSelection().toString().trim().length > 0);
        if (forceRender || (changed && !hasSelection)) {
          renderHistoryTable(rawHistory);
        }
      } catch (err) {
        console.error('Failed to fetch history data:', err);
      }
    }

    async function fetchTokensData(forceRender = false) {
      try {
        const q = (document.getElementById('tokens-search')?.value || '').trim();
        const params = new URLSearchParams({
          page: tokensCurrentPage,
          limit: tokensPageSize,
        });
        if (q) params.set('search', q);

        const res = await fetch(`/api/tokens?${params.toString()}`);
        if (!res.ok) return;
        const data = await res.json();

        let newTokens = [];
        if (Array.isArray(data)) {
          newTokens = data;
          tokensTotal = data.length;
          tokensTotalPages = Math.ceil(tokensTotal / tokensPageSize) || 1;
        } else {
          newTokens = data.items || [];
          tokensTotal = data.total || 0;
          tokensCurrentPage = data.page || 1;
          tokensPageSize = data.limit || tokensPageSize;
          tokensTotalPages = data.total_pages || 1;
        }

        const changed = hasTokensChanged(rawTokens, newTokens);
        rawTokens = newTokens;
        lastKnownTokensTotal = tokensTotal;

        const isViewingLatestTokens = (tokensCurrentPage === 1 && !q);
        if (typeof data.global_total === 'number') {
          if (isViewingLatestTokens || lastKnownGlobalTokensTotal === null) {
            lastKnownGlobalTokensTotal = data.global_total;
          }
        } else if (isViewingLatestTokens && (lastKnownGlobalTokensTotal === null || tokensTotal >= (lastKnownGlobalTokensTotal || 0))) {
          lastKnownGlobalTokensTotal = tokensTotal;
        }

        if (isViewingLatestTokens) {
          const pill = document.getElementById('tokens-new-items-pill');
          if (pill) pill.style.display = 'none';
        }

        const badgeCount = document.getElementById('badge-token-count');
        if (badgeCount) badgeCount.innerText = formatNumber(tokensTotal);
        updateTokensPaginationUI();

        const hasSelection = Boolean(window.getSelection && window.getSelection().toString().trim().length > 0);
        if (forceRender || (changed && !hasSelection)) {
          renderTokenTable(rawTokens);
        }
      } catch (err) {
        console.error('Failed to fetch tokens data:', err);
      }
    }

    function normalizeStatus(status) {
      if (status === 'blocked') return 'rejected';
      return status || 'skipped';
    }

    function getStatusTooltip(status) {
      const s = normalizeStatus(status);
      if (s === 'imported') return 'Imported into Sonarr/Radarr media library';
      if (s === 'rejected') return 'Deleted from download client & blocklisted (malware, sample, or corrupt)';
      if (s === 'dismissed') return 'Ignored/removed from *arr queue, files kept in download client';
      if (s === 'skipped') return 'Skipped without action (no qualifying release found, cooldown applied, or review needed)';
      if (s === 'failed') return 'Manual import or grab failed (eligible for retry)';
      if (s === 'dry_run') return 'Simulated in Dry Run mode, still in queue (eligible for retry)';
      return s;
    }

    function toggleReasoning(rowId) {
      const id = Number(rowId);
      const willOpen = !openReasoningIds.has(id);
      if (willOpen) {
        openReasoningIds.add(id);
      } else {
        openReasoningIds.delete(id);
      }

      const row = document.getElementById(`reasoning-${id}`);
      const card = document.getElementById(`reasoning-card-${id}`);
      const chevron = document.getElementById(`chevron-${id}`);
      const cardChevron = document.getElementById(`chevron-card-${id}`);

      if (row) row.classList.toggle('open', willOpen);
      if (card) card.classList.toggle('open', willOpen);
      if (chevron) chevron.innerText = willOpen ? '▴' : '▾';
      if (cardChevron) cardChevron.innerText = willOpen ? '▴' : '▾';
    }

    function renderActionCell(item, isCard = false) {
      const normStatus = normalizeStatus(item.status);
      if (!['skipped', 'failed', 'dry_run'].includes(normStatus)) {
        return isCard ? '' : '<span style="color: var(--text-dim); font-size: 0.75rem;">--</span>';
      }

      if (item.retry_requested) {
        if (isCard) {
          return `
            <button class="badge-pending-retry" style="width: 100%; height: 28px; justify-content: center;" onclick="event.stopPropagation(); cancelRetry(${item.id})" title="Queued for retry. Click to cancel.">
              <span class="queued-text">⏳ Queued</span>
              <span class="cancel-hint">✖ Cancel</span>
            </button>
          `;
        }
        return `
          <div class="action-btn-slot">
            <span class="badge-pending-retry" onclick="event.stopPropagation(); cancelRetry(${item.id})" title="Queued for retry. Click to cancel.">
              <span class="queued-text">⏳ Queued</span>
              <span class="cancel-hint">✖ Cancel</span>
            </span>
          </div>
        `;
      }

      if (isCard) {
        return `
          <button class="btn btn-sm btn-retry" style="width: 100%; height: 28px; justify-content: center;" onclick="event.stopPropagation(); retryItem(${item.id})" title="Allow this release to be re-evaluated on next scan">
            🔄 Retry
          </button>
        `;
      }

      return `
        <div class="action-btn-slot">
          <button class="btn btn-sm btn-retry" onclick="event.stopPropagation(); retryItem(${item.id})" title="Allow this release to be re-evaluated on next scan">
            🔄 Retry
          </button>
        </div>
      `;
    }

    function renderHistoryTable(items) {
      const tbody = document.getElementById('history-table-body');
      const cardsContainer = document.getElementById('history-cards-container');
      if (!items || items.length === 0) {
        if (tbody) {
          tbody.innerHTML = `
            <tr>
              <td colspan="8" class="empty-state">
                <div class="empty-icon">📭</div>
                <div>No processed items found.</div>
              </td>
            </tr>`;
        }
        if (cardsContainer) {
          cardsContainer.innerHTML = `
            <div class="empty-state">
              <div class="empty-icon">📭</div>
              <div>No processed items found.</div>
            </div>`;
        }
        return;
      }

      if (tbody) {
        tbody.innerHTML = items.map(item => {
          const confText = item.confidence !== null ? `${Math.round(item.confidence * 100)}%` : '--';
          let modelText = item.model || 'Direct / Rule';
          if (modelText.includes(' with Fallback to ')) {
            modelText = modelText.split(' with Fallback to ')[0];
          }
          const appClass = (item.app || '').toLowerCase() === 'sonarr' ? 'badge-sonarr' : 'badge-radarr';
          const normStatus = normalizeStatus(item.status);
          const statusClass = `badge-${normStatus}`;
          const statusTooltip = getStatusTooltip(normStatus);
          const isOpen = openReasoningIds.has(Number(item.id));

          return `
            <tr class="clickable-row" onclick="toggleReasoning(${item.id})">
              <td>${formatDateTimeCell(item.timestamp)}</td>
              <td><span class="badge ${appClass}">${item.app || 'App'}</span></td>
              <td class="title-cell">
                <div class="title-main">${escapeHtml(item.title)}</div>
                ${item.details ? `<div class="title-sub">${escapeHtml(item.details)}</div>` : ''}
              </td>
              <td><span class="badge badge-category">${item.category}</span></td>
              <td><span class="badge ${statusClass}" title="${escapeHtml(statusTooltip)}">${normStatus}</span></td>
              <td>
                <div style="font-weight: 600;">${confText}</div>
                <div style="font-size: 0.7rem; color: var(--text-dim);">${escapeHtml(modelText)}</div>
              </td>
              <td id="action-cell-${item.id}" class="col-action" onclick="event.stopPropagation()">${renderActionCell(item, false)}</td>
              <td id="chevron-${item.id}" class="reasoning-chevron" style="color: var(--text-dim); font-size: 0.8rem;">${isOpen ? '▴' : '▾'}</td>
            </tr>
            <tr id="reasoning-${item.id}" class="reasoning-row ${isOpen ? 'open' : ''}" onclick="event.stopPropagation()">
              <td colspan="8">
                <div class="reasoning-box">
                  <div style="font-weight: 600; color: #a5b4fc; margin-bottom: 0.35rem;">
                    🤖 AI Reasoning & Decision Details
                  </div>
                  <div>${escapeHtml(item.reasoning || item.details || 'No reasoning details recorded.')}</div>
                  <div style="font-size: 0.7rem; color: var(--text-dim); margin-top: 0.35rem;">
                    ID: #${item.id} &bull; Time: ${formatDateTime(item.timestamp)} &bull; Model: ${escapeHtml(modelText)} &bull; Confidence: ${confText}
                  </div>
                </div>
              </td>
            </tr>
          `;
        }).join('');
      }

      if (cardsContainer) {
        cardsContainer.innerHTML = items.map(item => {
          const confText = item.confidence !== null ? `${Math.round(item.confidence * 100)}%` : '--';
          let modelText = item.model || 'Direct / Rule';
          if (modelText.includes(' with Fallback to ')) {
            modelText = modelText.split(' with Fallback to ')[0];
          }
          const appClass = (item.app || '').toLowerCase() === 'sonarr' ? 'badge-sonarr' : 'badge-radarr';
          const normStatus = normalizeStatus(item.status);
          const statusClass = `badge-${normStatus}`;
          const statusTooltip = getStatusTooltip(normStatus);
          const isOpen = openReasoningIds.has(Number(item.id));

          return `
            <div class="history-mobile-card clickable-row" onclick="toggleReasoning(${item.id})">
              <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem;">
                <div style="display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap;">
                  <span class="badge ${appClass}">${item.app || 'App'}</span>
                  <span class="badge ${statusClass}" title="${escapeHtml(statusTooltip)}">${normStatus}</span>
                </div>
                <div style="font-size: 0.72rem; color: var(--text-dim); text-align: right; line-height: 1.25; flex-shrink: 0;">
                  <div>${formatDate(item.timestamp)}</div>
                  <div>${formatTime(item.timestamp)}</div>
                </div>
              </div>

              <div style="margin: 0.15rem 0;">
                <div style="font-weight: 600; font-size: 0.88rem; color: var(--text-main); word-break: break-all; line-height: 1.35;">${escapeHtml(item.title)}</div>
                ${item.details ? `<div style="font-size: 0.74rem; color: var(--text-dim); margin-top: 0.15rem; word-break: break-all;">${escapeHtml(item.details)}</div>` : ''}
              </div>

              <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.4rem; background: rgba(0, 0, 0, 0.2); padding: 0.45rem 0.65rem; border-radius: 6px; font-size: 0.74rem;">
                <div style="display: flex; align-items: center; gap: 0.35rem;">
                  <span style="color: var(--text-dim);">Category:</span>
                  <span class="badge badge-category" style="font-size: 0.68rem; padding: 0.1rem 0.35rem;">${item.category}</span>
                </div>
                <div style="display: flex; align-items: center; gap: 0.35rem;">
                  <span style="color: var(--text-dim);">AI:</span>
                  <span style="font-weight: 600; color: var(--text-main);">${confText}</span>
                  <span style="color: var(--text-muted); font-size: 0.7rem;">(${escapeHtml(modelText)})</span>
                </div>
              </div>

              <div id="action-card-${item.id}" style="margin-top: 0.1rem;" onclick="event.stopPropagation()">
                ${renderActionCell(item, true)}
              </div>

              <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.72rem; color: var(--accent); padding-top: 0.3rem; border-top: 1px solid var(--border-subtle);">
                <span>AI Reasoning &amp; Details</span>
                <span id="chevron-card-${item.id}" class="reasoning-chevron" style="font-size: 0.85rem;">${isOpen ? '▴' : '▾'}</span>
              </div>

              <div id="reasoning-card-${item.id}" class="reasoning-card-panel ${isOpen ? 'open' : ''}" style="margin-top: 0.2rem;" onclick="event.stopPropagation()">
                <div class="reasoning-box">
                  <div style="font-weight: 600; color: #a5b4fc; margin-bottom: 0.35rem;">
                    🤖 AI Reasoning &amp; Decision Details
                  </div>
                  <div style="line-height: 1.45; word-break: break-word;">${escapeHtml(item.reasoning || item.details || 'No reasoning details recorded.')}</div>
                  <div style="font-size: 0.7rem; color: var(--text-dim); margin-top: 0.45rem;">
                    ID: #${item.id} &bull; Time: ${formatDateTime(item.timestamp)} &bull; Model: ${escapeHtml(modelText)} &bull; Confidence: ${confText}
                  </div>
                </div>
              </div>
            </div>
          `;
        }).join('');
      }
    }

    function toggleTokenDetails(rowId) {
      const id = Number(rowId);
      const willOpen = !openTokenIds.has(id);
      if (willOpen) {
        openTokenIds.add(id);
      } else {
        openTokenIds.delete(id);
      }

      const row = document.getElementById(`token-detail-${id}`);
      const card = document.getElementById(`token-card-${id}`);
      const chevron = document.getElementById(`token-chevron-${id}`);
      const cardChevron = document.getElementById(`token-chevron-card-${id}`);

      if (row) row.classList.toggle('open', willOpen);
      if (card) card.classList.toggle('open', willOpen);
      if (chevron) chevron.innerText = willOpen ? '▴' : '▾';
      if (cardChevron) cardChevron.innerText = willOpen ? '▴' : '▾';
    }

    function renderTokenTable(tokens) {
      const tbody = document.getElementById('token-table-body');
      const cardsContainer = document.getElementById('token-cards-container');
      if (!tokens || tokens.length === 0) {
        if (tbody) {
          tbody.innerHTML = `
            <tr>
              <td colspan="10" class="empty-state">
                <div class="empty-icon">✨</div>
                <div>No AI token calls recorded yet.</div>
              </td>
            </tr>`;
        }
        if (cardsContainer) {
          cardsContainer.innerHTML = `
            <div class="empty-state">
              <div class="empty-icon">✨</div>
              <div>No AI token calls recorded yet.</div>
            </div>`;
        }
        return;
      }

      if (tbody) {
        tbody.innerHTML = tokens.map(t => {
          let statusBadge;
          if (t.success) {
            statusBadge = '<span class="badge badge-imported">Success</span>';
          } else {
            const codeLabel = t.error_code ? escapeHtml(t.error_code) : 'Failed';
            statusBadge = `<span class="badge badge-blocked" title="${escapeHtml(t.response_text || 'AI Call Failed')}">✖ ${codeLabel}</span>`;
          }
          const latency = t.duration_ms ? `${t.duration_ms}ms` : '--';
          const thinkingHtml = (t.thinking_tokens && t.thinking_tokens > 0)
            ? `<span style="color: #a78bfa; font-feature-settings: 'tnum'; font-weight: 500;">${formatNumber(t.thinking_tokens)}</span>`
            : `<span style="color: var(--text-dim);">&mdash;</span>`;
          const isOpen = openTokenIds.has(Number(t.id));
          const inputText = t.prompt_text || 'No input message recorded for this entry.';
          const outputText = t.response_text || 'No output message recorded for this entry.';

          const errorBannerHtml = !t.success ? `
            <div class="debug-error-banner">
              <div class="debug-error-title">
                <span>🚨</span>
                <span>AI Call Failed ${t.error_code ? `&bull; Error Code: <code class="debug-error-code">${escapeHtml(t.error_code)}</code>` : ''}</span>
              </div>
              <div class="debug-error-reason">${escapeHtml(t.response_text || 'No error details recorded.')}</div>
            </div>
          ` : '';

          return `
            <tr class="clickable-row" onclick="toggleTokenDetails(${t.id})">
              <td>${formatDateTimeCell(t.timestamp)}</td>
              <td class="title-cell">
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
              <td id="token-chevron-${t.id}" class="token-chevron" style="color: var(--text-dim); font-size: 0.8rem;">${isOpen ? '▴' : '▾'}</td>
            </tr>
            <tr id="token-detail-${t.id}" class="token-detail-row ${isOpen ? 'open' : ''}" onclick="event.stopPropagation()">
              <td colspan="10">
                <div class="reasoning-box">
                  <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.35rem; flex-wrap: wrap; gap: 0.4rem;">
                    <div style="font-weight: 600; color: #a5b4fc;">
                      🔍 AI Debug Trace &bull; <span style="font-family: monospace; font-size: 0.78rem; color: #c084fc;">${escapeHtml(t.operation)}</span>
                    </div>
                    ${!t.success && t.error_code ? `<span class="debug-error-code">${escapeHtml(t.error_code)}</span>` : ''}
                  </div>
                  ${errorBannerHtml}
                  <div class="debug-msg-grid">
                    <div class="debug-msg-panel">
                      <div class="debug-msg-header" style="color: #60a5fa;">
                        <span>📥 Input Message (Prompt)</span>
                        <span>${formatNumber(t.prompt_tokens)} tok</span>
                      </div>
                      <pre class="debug-msg-pre">${escapeHtml(inputText)}</pre>
                    </div>
                    <div class="debug-msg-panel">
                      <div class="debug-msg-header" style="color: ${t.success ? '#34d399' : '#f87171'};">
                        <span>${t.success ? '📤 Output Message (Response)' : '⚠️ Error Details & Server Response'}</span>
                        <span>${t.success ? `${formatNumber(t.completion_tokens)} tok` : (t.error_code ? escapeHtml(t.error_code) : 'Failed')}</span>
                      </div>
                      <pre class="debug-msg-pre" style="${t.success ? '' : 'color: #fca5a5; background: rgba(239, 68, 68, 0.05);'}">${escapeHtml(outputText)}</pre>
                    </div>
                  </div>
                  <div style="font-size: 0.7rem; color: var(--text-dim); margin-top: 0.45rem;">
                    ID: #${t.id} &bull; Time: ${formatDateTime(t.timestamp)} &bull; Model: ${escapeHtml(t.model)} &bull; Latency: ${latency} ${t.error_code ? `&bull; Error: ${escapeHtml(t.error_code)}` : ''}
                  </div>
                </div>
              </td>
            </tr>
          `;
        }).join('');
      }

      if (cardsContainer) {
        cardsContainer.innerHTML = tokens.map(t => {
          let statusBadge;
          if (t.success) {
            statusBadge = '<span class="badge badge-imported" style="font-size: 0.7rem; padding: 0.15rem 0.45rem;">✔ Success</span>';
          } else {
            const codeLabel = t.error_code ? escapeHtml(t.error_code) : 'Failed';
            statusBadge = `<span class="badge badge-blocked" style="font-size: 0.7rem; padding: 0.15rem 0.45rem;" title="${escapeHtml(t.response_text || 'AI Call Failed')}">✖ ${codeLabel}</span>`;
          }
          const latency = t.duration_ms ? `${t.duration_ms}ms` : '--';
          const thinkingHtml = (t.thinking_tokens && t.thinking_tokens > 0)
            ? `<span style="color: #a78bfa; font-feature-settings: 'tnum'; font-weight: 500;">${formatNumber(t.thinking_tokens)}</span>`
            : `<span style="color: var(--text-dim);">&mdash;</span>`;
          const isOpen = openTokenIds.has(Number(t.id));
          const inputText = t.prompt_text || 'No input message recorded for this entry.';
          const outputText = t.response_text || 'No output message recorded for this entry.';

          const errorBannerHtml = !t.success ? `
            <div class="debug-error-banner">
              <div class="debug-error-title">
                <span>🚨</span>
                <span>AI Call Failed ${t.error_code ? `&bull; Error Code: <code class="debug-error-code">${escapeHtml(t.error_code)}</code>` : ''}</span>
              </div>
              <div class="debug-error-reason">${escapeHtml(t.response_text || 'No error details recorded.')}</div>
            </div>
          ` : '';

          return `
            <div class="token-mobile-card clickable-row" onclick="toggleTokenDetails(${t.id})">
              <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem;">
                <div style="min-width: 0; flex: 1;">
                  <div style="font-weight: 700; font-family: monospace; font-size: 0.82rem; color: var(--text-main); word-break: break-all;">${escapeHtml(t.operation)}</div>
                  ${t.item_title ? `<div style="font-size: 0.74rem; color: var(--text-dim); margin-top: 0.15rem; word-break: break-all;">${escapeHtml(t.item_title)}</div>` : ''}
                </div>
                <div style="text-align: right; flex-shrink: 0;">
                  <div>${statusBadge}</div>
                  <div style="font-size: 0.7rem; color: var(--text-dim); margin-top: 0.2rem; text-align: right; line-height: 1.25;">
                    <div>${formatDate(t.timestamp)} ${formatTime(t.timestamp)}</div>
                    <div>${latency}</div>
                  </div>
                </div>
              </div>

              <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.4rem; font-size: 0.75rem; background: rgba(0, 0, 0, 0.2); padding: 0.45rem 0.65rem; border-radius: 6px;">
                <div><span style="color: var(--text-dim);">Model:</span> <span style="color: #c084fc; font-weight: 500;">${escapeHtml(t.model)}</span></div>
                <div style="display: flex; gap: 0.4rem; align-items: center; font-feature-settings: 'tnum'; font-size: 0.72rem;">
                  <span style="color: #60a5fa;" title="Prompt tokens">${formatNumber(t.prompt_tokens)}</span>
                  <span style="color: var(--text-dim);">+</span>
                  <span style="color: #a78bfa;" title="Thinking tokens">${thinkingHtml}</span>
                  <span style="color: var(--text-dim);">+</span>
                  <span style="color: #34d399;" title="Output tokens">${formatNumber(t.completion_tokens)}</span>
                  <span style="color: var(--text-dim);">=</span>
                  <strong style="color: var(--text-main);">${formatNumber(t.total_tokens)} tok</strong>
                </div>
              </div>

              <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.72rem; color: var(--accent); padding-top: 0.3rem; border-top: 1px solid var(--border-subtle);">
                <span>Debug Trace (Prompt &amp; Output)</span>
                <span id="token-chevron-card-${t.id}" class="token-chevron" style="font-size: 0.85rem;">${isOpen ? '▴' : '▾'}</span>
              </div>

              <div id="token-card-${t.id}" class="token-card-panel ${isOpen ? 'open' : ''}" style="margin-top: 0.2rem;" onclick="event.stopPropagation()">
                <div class="reasoning-box">
                  <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.35rem; flex-wrap: wrap; gap: 0.4rem;">
                    <div style="font-weight: 600; color: #a5b4fc;">
                      🔍 AI Debug Trace &bull; <span style="font-family: monospace; font-size: 0.78rem; color: #c084fc;">${escapeHtml(t.operation)}</span>
                    </div>
                    ${!t.success && t.error_code ? `<span class="debug-error-code">${escapeHtml(t.error_code)}</span>` : ''}
                  </div>
                  ${errorBannerHtml}
                  <div class="debug-msg-grid">
                    <div class="debug-msg-panel">
                      <div class="debug-msg-header" style="color: #60a5fa;">
                        <span>📥 Input Message (Prompt)</span>
                        <span>${formatNumber(t.prompt_tokens)} tok</span>
                      </div>
                      <pre class="debug-msg-pre">${escapeHtml(inputText)}</pre>
                    </div>
                    <div class="debug-msg-panel">
                      <div class="debug-msg-header" style="color: ${t.success ? '#34d399' : '#f87171'};">
                        <span>${t.success ? '📤 Output Message (Response)' : '⚠️ Error Details & Server Response'}</span>
                        <span>${t.success ? `${formatNumber(t.completion_tokens)} tok` : (t.error_code ? escapeHtml(t.error_code) : 'Failed')}</span>
                      </div>
                      <pre class="debug-msg-pre" style="${t.success ? '' : 'color: #fca5a5; background: rgba(239, 68, 68, 0.05);'}">${escapeHtml(outputText)}</pre>
                    </div>
                  </div>
                  <div style="font-size: 0.7rem; color: var(--text-dim); margin-top: 0.45rem;">
                    ID: #${t.id} &bull; Time: ${formatDateTime(t.timestamp)} &bull; Model: ${escapeHtml(t.model)} &bull; Latency: ${latency} ${t.error_code ? `&bull; Error: ${escapeHtml(t.error_code)}` : ''}
                  </div>
                </div>
              </div>
            </div>
          `;
        }).join('');
      }
    }

    function renderAnalytics(summary) {
      // Models List (sorted by total_tokens descending, then calls descending)
      const modelsList = document.getElementById('analytics-models-list');
      const models = summary.tokens_by_model || {};
      const modelKeys = Object.keys(models).sort((a, b) => {
        const diff = (models[b].total_tokens || 0) - (models[a].total_tokens || 0);
        return diff !== 0 ? diff : (models[b].calls || 0) - (models[a].calls || 0);
      });

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

      // Operations List (sorted by total_tokens descending, then calls descending)
      const opsList = document.getElementById('analytics-operations-list');
      const ops = summary.tokens_by_operation || {};
      const opKeys = Object.keys(ops).sort((a, b) => {
        const diff = (ops[b].total_tokens || 0) - (ops[a].total_tokens || 0);
        return diff !== 0 ? diff : (ops[b].calls || 0) - (ops[a].calls || 0);
      });

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

    function formatRelativeTime(isoStr) {
      if (!isoStr) return 'Never';
      const d = parseUtcDate(isoStr);
      if (!d) return '--';
      const diffSec = Math.round((Date.now() - d.getTime()) / 1000);
      if (diffSec < 5) return 'Just now';
      if (diffSec < 60) return `${diffSec}s ago`;
      const diffMin = Math.round(diffSec / 60);
      if (diffMin < 60) return `${diffMin}m ago`;
      const diffHr = Math.round(diffMin / 60);
      if (diffHr < 24) return `${diffHr}h ago`;
      const diffDays = Math.round(diffHr / 24);
      return `${diffDays}d ago`;
    }

    function formatNextRun(isoStr, state) {
      if (state === 'disabled') return 'Disabled';
      if (state === 'running') return 'In Progress';
      if (!isoStr) return '--';
      const d = parseUtcDate(isoStr);
      if (!d) return '--';
      const diffSec = Math.round((d.getTime() - Date.now()) / 1000);
      if (diffSec <= 0) return 'Due now';
      if (diffSec < 60) return `in ${diffSec}s`;
      const diffMin = Math.round(diffSec / 60);
      if (diffMin < 60) return `in ${diffMin}m`;
      const diffHr = Math.floor(diffMin / 60);
      const remMin = diffMin % 60;
      return remMin > 0 ? `in ${diffHr}h ${remMin}m` : `in ${diffHr}h`;
    }

    function renderTasks(tasks) {
      if (!tasks) return;

      const configs = [
        {
          key: 'season_pack_backfill',
          cardId: 'task-backfill-card',
          badgeId: 'task-backfill-badge',
          btnId: 'btn-trigger-backfill',
          btnLabel: 'Run Backfill',
          progressBoxId: 'task-backfill-progress-box',
          progressTextId: 'task-backfill-progress-text',
          lastRunId: 'task-backfill-last-run',
          lastGrabbedId: 'task-backfill-last-grabbed',
          nextRunId: 'task-backfill-next-run',
          grabLabel: 'packs'
        },
        {
          key: 'missing_episode_retry',
          cardId: 'task-episode-retry-card',
          badgeId: 'task-episode-retry-badge',
          btnId: 'btn-trigger-episode-retry',
          btnLabel: 'Retry Episodes',
          progressBoxId: 'task-episode-retry-progress-box',
          progressTextId: 'task-episode-retry-progress-text',
          lastRunId: 'task-episode-retry-last-run',
          lastGrabbedId: 'task-episode-retry-last-grabbed',
          nextRunId: 'task-episode-retry-next-run',
          grabLabel: 'episodes'
        },
        {
          key: 'missing_movie_search',
          cardId: 'task-movie-search-card',
          badgeId: 'task-movie-search-badge',
          btnId: 'btn-trigger-movie-search',
          btnLabel: 'Search Movies',
          progressBoxId: 'task-movie-search-progress-box',
          progressTextId: 'task-movie-search-progress-text',
          lastRunId: 'task-movie-search-last-run',
          lastGrabbedId: 'task-movie-search-last-grabbed',
          nextRunId: 'task-movie-search-next-run',
          grabLabel: 'movies'
        }
      ];

      configs.forEach(tc => {
        const info = tasks[tc.key];
        if (!info) return;

        const card = document.getElementById(tc.cardId);
        const badge = document.getElementById(tc.badgeId);
        const btn = document.getElementById(tc.btnId);
        const progressBox = document.getElementById(tc.progressBoxId);
        const progressText = document.getElementById(tc.progressTextId);
        const lastRun = document.getElementById(tc.lastRunId);
        const lastGrabbed = document.getElementById(tc.lastGrabbedId);
        const nextRun = document.getElementById(tc.nextRunId);

        const state = info.state || 'idle';
        if (card) {
          card.classList.toggle('running', state === 'running');
        }

        if (badge) {
          badge.className = 'badge';
          if (state === 'running') {
            badge.classList.add('task-badge-running');
            badge.innerText = '⚡ Running...';
          } else if (state === 'disabled') {
            badge.classList.add('task-badge-disabled');
            badge.innerText = 'Disabled';
          } else {
            badge.classList.add('task-badge-idle');
            badge.innerText = 'Idle';
          }
        }

        if (btn) {
          btn.disabled = (state === 'running' || state === 'disabled');
          btn.innerHTML = state === 'running'
            ? `<div class="task-spinner" style="width:11px; height:11px; border-width:2px; display:inline-block;"></div> Running...`
            : `<span>▶</span> ${tc.btnLabel}`;
        }

        if (progressBox && progressText) {
          if (state === 'running' || info.current_progress) {
            progressBox.style.display = 'flex';
            progressText.innerText = info.current_progress || 'Scanning library and indexers...';
          } else {
            progressBox.style.display = 'none';
          }
        }

        if (lastRun) {
          let text = formatRelativeTime(info.last_run);
          if (info.last_duration_sec) {
            text += ` (${info.last_duration_sec}s)`;
          }
          lastRun.innerText = text;
          if (info.last_run) {
            lastRun.title = formatDateTime(info.last_run);
          } else {
            lastRun.removeAttribute('title');
          }
        }

        if (lastGrabbed) {
          const grabbed = info.items_grabbed_last_run || 0;
          lastGrabbed.innerText = `${formatNumber(grabbed)} ${tc.grabLabel}`;
        }

        if (nextRun) {
          nextRun.innerText = formatNextRun(info.next_run, state);
          if (info.next_run) {
            nextRun.title = formatDateTime(info.next_run);
          } else {
            nextRun.removeAttribute('title');
          }
        }
      });
    }

    function updateStatsUI(summary) {
      if (!summary) return;
      document.getElementById('stat-uptime').innerText = summary.uptime_formatted || '--';
      document.getElementById('stat-scans').innerText = `Queue Scans: ${formatNumber(summary.total_scans)}`;
      document.getElementById('stat-processed').innerText = formatNumber(summary.total_items_processed);
      const qSize = summary.fifo_queue_size || 0;
      document.getElementById('stat-processed-sub').innerText = qSize > 0
        ? `⚡ FIFO Queue: ${qSize} active`
        : 'Scanned items (FIFO idle)';
      document.getElementById('stat-imported').innerText = formatNumber(summary.total_imported);
      document.getElementById('stat-dry-run').innerText = `${formatNumber(summary.total_dry_run)} in Dry Run`;
      const rejCount = summary.total_rejected || 0;
      const disCount = summary.total_dismissed || 0;
      document.getElementById('stat-blocked').innerText = formatNumber(rejCount + disCount);
      document.getElementById('stat-blocked-sub').innerText = `${formatNumber(rejCount)} Rejected (Deleted) | ${formatNumber(disCount)} Dismissed (Kept in Client)`;
      document.getElementById('stat-tokens').innerText = formatNumber(summary.total_tokens);
      const pTok = formatNumber(summary.total_prompt_tokens || 0);
      const thTok = formatNumber(summary.total_thinking_tokens || 0);
      const outTok = formatNumber(summary.total_completion_tokens || 0);
      document.getElementById('stat-token-sub').innerText = `Prompt: ${pTok} | Thinking: ${thTok} | Output: ${outTok}`;
      renderAnalytics(summary);
      renderTasks(summary.tasks);
    }

    async function fetchStatsOnly() {
      try {
        const res = await fetch('/api/metrics');
        if (!res.ok) return;
        const summary = await res.json();
        updateStatsUI(summary);

        const globalHistory = typeof summary.total_items_processed === 'number' ? summary.total_items_processed : null;
        const globalTokens = typeof summary.total_ai_calls === 'number' ? summary.total_ai_calls : null;

        // Check if new processed history items are available
        if (lastKnownGlobalHistoryTotal === null && globalHistory !== null) {
          lastKnownGlobalHistoryTotal = globalHistory;
        } else if (globalHistory !== null && lastKnownGlobalHistoryTotal !== null) {
          if (globalHistory > lastKnownGlobalHistoryTotal) {
            const diff = globalHistory - lastKnownGlobalHistoryTotal;
            const pill = document.getElementById('history-new-items-pill');
            const txt = document.getElementById('history-new-items-text');
            if (pill && txt) {
              txt.innerText = `${diff} new processed item${diff > 1 ? 's' : ''} available`;
              pill.style.display = 'inline-flex';
            }
          } else if (globalHistory <= lastKnownGlobalHistoryTotal) {
            const pill = document.getElementById('history-new-items-pill');
            if (pill) pill.style.display = 'none';
            if (globalHistory < lastKnownGlobalHistoryTotal) {
              lastKnownGlobalHistoryTotal = globalHistory;
            }
          }
        }

        // Check if new AI calls are recorded
        if (lastKnownGlobalTokensTotal === null && globalTokens !== null) {
          lastKnownGlobalTokensTotal = globalTokens;
        } else if (globalTokens !== null && lastKnownGlobalTokensTotal !== null) {
          if (globalTokens > lastKnownGlobalTokensTotal) {
            const diff = globalTokens - lastKnownGlobalTokensTotal;
            const pill = document.getElementById('tokens-new-items-pill');
            const txt = document.getElementById('tokens-new-items-text');
            if (pill && txt) {
              txt.innerText = `${diff} new AI call${diff > 1 ? 's' : ''} recorded`;
              pill.style.display = 'inline-flex';
            }
          } else if (globalTokens <= lastKnownGlobalTokensTotal) {
            const pill = document.getElementById('tokens-new-items-pill');
            if (pill) pill.style.display = 'none';
            if (globalTokens < lastKnownGlobalTokensTotal) {
              lastKnownGlobalTokensTotal = globalTokens;
            }
          }
        }

        document.getElementById('header-status').innerText = 'Online';
      } catch (err) {
        console.error('Failed to fetch stats:', err);
        document.getElementById('header-status').innerText = 'Reconnecting...';
      }
    }

    async function fetchData(isManual = false) {
      const refreshIcon = document.getElementById('refresh-icon');
      if (isManual && refreshIcon) refreshIcon.classList.add('spin');

      try {
        const [sumRes] = await Promise.all([
          fetch('/api/metrics'),
          fetchHistoryData(true),
          fetchTokensData(true)
        ]);

        if (sumRes.ok) {
          const summary = await sumRes.json();
          updateStatsUI(summary);
          if (typeof summary.total_items_processed === 'number') {
            lastKnownGlobalHistoryTotal = summary.total_items_processed;
          }
          if (typeof summary.total_ai_calls === 'number') {
            lastKnownGlobalTokensTotal = summary.total_ai_calls;
          }
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
      const sel = document.getElementById('refresh-interval');
      if (!sel) return;
      const val = parseInt(sel.value, 10);
      try {
        localStorage.setItem('aimportarr_stats_interval', String(val));
      } catch (_) {}
      if (val > 0) {
        refreshTimer = setInterval(() => fetchStatsOnly(), val);
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

    async function triggerImmediateScan() {
      try {
        const res = await fetch('/api/scan', { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showToast('Immediate queue scan triggered!', 'success');
          setTimeout(() => fetchData(true), 500);
        } else {
          showToast('Failed to trigger queue scan', 'danger');
        }
      } catch (e) {
        console.error(e);
        showToast('Network error triggering scan', 'danger');
      }
    }

    async function triggerBackfillNow() {
      const btn = document.getElementById('btn-trigger-backfill');
      if (btn) btn.disabled = true;
      try {
        const res = await fetch('/api/backfill', { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showToast('Season Pack Backfill check started in background!', 'success');
          setTimeout(() => fetchData(true), 400);
        } else {
          showToast(data.message || 'Failed to trigger Season Pack Backfill', 'danger');
        }
      } catch (e) {
        console.error(e);
        showToast('Network error triggering Season Pack Backfill', 'danger');
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function triggerEpisodeRetryNow() {
      const btn = document.getElementById('btn-trigger-episode-retry');
      if (btn) btn.disabled = true;
      try {
        const res = await fetch('/api/episode-retry', { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showToast('Missing Episode Retry check started in background!', 'success');
          setTimeout(() => fetchData(true), 400);
        } else {
          showToast(data.message || 'Failed to trigger Missing Episode Retry', 'danger');
        }
      } catch (e) {
        console.error(e);
        showToast('Network error triggering Missing Episode Retry', 'danger');
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function triggerMovieSearchNow() {
      const btn = document.getElementById('btn-trigger-movie-search');
      if (btn) btn.disabled = true;
      try {
        const res = await fetch('/api/movie-search', { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showToast('Missing Movie Search check started in background!', 'success');
          setTimeout(() => fetchData(true), 400);
        } else {
          showToast(data.message || 'Failed to trigger Missing Movie Search', 'danger');
        }
      } catch (e) {
        console.error(e);
        showToast('Network error triggering Missing Movie Search', 'danger');
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    function updateActionCellState(id, retryRequested) {
      const it = rawHistory.find(x => x.id === id);
      if (it) {
        it.retry_requested = retryRequested ? 1 : 0;
      }
      const cell = document.getElementById(`action-cell-${id}`);
      if (cell && it) {
        cell.innerHTML = renderActionCell(it, false);
      }
      const cardSlot = document.getElementById(`action-card-${id}`);
      if (cardSlot && it) {
        cardSlot.innerHTML = renderActionCell(it, true);
      }
    }

    async function retryItem(id) {
      updateActionCellState(id, 1);
      try {
        const res = await fetch('/api/retry', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: id })
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showToast(`Release #${id} queued for immediate retry!`, 'success');
        } else {
          updateActionCellState(id, 0);
          showToast(data.message || `Failed to retry item #${id}`, 'danger');
        }
      } catch (e) {
        console.error(e);
        updateActionCellState(id, 0);
        showToast('Network error retrying item', 'danger');
      }
    }

    async function cancelRetry(id) {
      updateActionCellState(id, 0);
      try {
        const res = await fetch('/api/retry', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: id, cancel: true })
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showToast(`Cancelled retry for release #${id}`, 'info');
        } else {
          updateActionCellState(id, 1);
          showToast(data.message || `Failed to cancel retry for item #${id}`, 'danger');
        }
      } catch (e) {
        console.error(e);
        updateActionCellState(id, 1);
        showToast('Network error cancelling retry', 'danger');
      }
    }

    async function retryAllRejected() {
      if (!confirm('Mark all skipped, failed, and dry-run items still sitting in the queue for retry on the next scan?')) {
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
          showToast(data.message || `Marked ${data.count || 0} item(s) for retry!`, 'success');
          fetchData(true);
        } else {
          showToast('Failed to mark items for retry', 'danger');
        }
      } catch (e) {
        console.error(e);
        showToast('Network error retrying items', 'danger');
      }
    }

    function openClearHistoryModal() {
      const modal = document.getElementById('modal-clear-history');
      if (modal) modal.style.display = 'flex';
    }

    function closeClearHistoryModal() {
      const modal = document.getElementById('modal-clear-history');
      if (modal) modal.style.display = 'none';
    }

    async function submitClearHistory() {
      const clearItems = document.getElementById('clear-opt-items')?.checked ?? true;
      const clearTokens = document.getElementById('clear-opt-tokens')?.checked ?? true;
      const resetCounters = document.getElementById('clear-opt-counters')?.checked ?? false;

      if (!clearItems && !clearTokens && !resetCounters) {
        showToast('Please select at least one item to clear.', 'warning');
        return;
      }

      const btn = document.getElementById('btn-confirm-clear');
      if (btn) {
        btn.disabled = true;
        btn.innerText = 'Clearing...';
      }

      try {
        const res = await fetch('/api/history/clear', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            items: clearItems,
            tokens: clearTokens,
            counters: resetCounters
          })
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showToast(data.message || 'Database history cleared successfully!', 'success');
          closeClearHistoryModal();
          historyCurrentPage = 1;
          tokensCurrentPage = 1;
          rawHistory = [];
          rawTokens = [];
          lastKnownHistoryTotal = null;
          lastKnownTokensTotal = null;
          lastKnownGlobalHistoryTotal = null;
          lastKnownGlobalTokensTotal = null;
          if (typeof fetchHistoryData === 'function') fetchHistoryData(true);
          if (typeof fetchTokensData === 'function') fetchTokensData(true);
          if (typeof fetchData === 'function') fetchData(true);
        } else {
          showToast(data.message || 'Failed to clear database history', 'danger');
        }
      } catch (e) {
        console.error(e);
        showToast('Network error clearing history: ' + e.message, 'danger');
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerText = 'Clear Selected History';
        }
      }
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeClearHistoryModal();
    });

    let settingsBaselineSnapshot = null;

    function collectSettingsPayload() {
      const mappings = [];
      document.querySelectorAll('.path-mapping-row').forEach(row => {
        const f = row.querySelector('.pm-from')?.value.trim();
        const t = row.querySelector('.pm-to')?.value.trim();
        if (f && t) mappings.push({ from: f, to: t });
      });

      return {
        general: {
          dry_run: Boolean(document.getElementById('cfg-dry-run')?.checked),
          reject_on_low_confidence: Boolean(document.getElementById('cfg-reject-low-conf')?.checked),
          min_confidence: parseFloat(document.getElementById('cfg-min-confidence')?.value) || 0.85,
          check_interval_seconds: parseInt(document.getElementById('cfg-check-interval')?.value, 10) || 30,
          ai_cooldown_seconds: parseFloat(document.getElementById('cfg-ai-cooldown')?.value) ?? 4.0,
          log_level: document.getElementById('cfg-log-level')?.value || 'INFO',
          timezone: (document.getElementById('cfg-timezone')?.value || '').trim()
        },
        ai: {
          provider: document.getElementById('cfg-ai-provider')?.value || 'gemini',
          fallback_enabled: Boolean(document.getElementById('cfg-ai-fallback')?.checked),
          gemini: {
            api_key: (document.getElementById('cfg-gemini-key')?.value || '').trim(),
            model: (document.getElementById('cfg-gemini-model')?.value || '').trim() || 'auto'
          },
          ollama: {
            base_url: (document.getElementById('cfg-ollama-url')?.value || '').trim(),
            model: (document.getElementById('cfg-ollama-model')?.value || '').trim(),
            timeout_seconds: parseFloat(document.getElementById('cfg-ollama-timeout')?.value || '60') || 60
          }
        },
        sonarr: {
          enabled: Boolean(document.getElementById('cfg-sonarr-enabled')?.checked),
          url: (document.getElementById('cfg-sonarr-url')?.value || '').trim(),
          api_key: (document.getElementById('cfg-sonarr-key')?.value || '').trim(),
          season_pack_backfill: {
            enabled: Boolean(document.getElementById('cfg-sonarr-backfill-enabled')?.checked),
            cron: (document.getElementById('cfg-sonarr-backfill-cron')?.value || '').trim() || '0 2 * * *',
            min_missing_days: parseInt(document.getElementById('cfg-sonarr-backfill-missing-days')?.value, 10) ?? 3,
            max_seasons_per_run: parseInt(document.getElementById('cfg-sonarr-backfill-max-seasons')?.value, 10) || 3,
            season_cooldown_days: parseInt(document.getElementById('cfg-sonarr-backfill-cooldown')?.value, 10) || 7
          },
          missing_episode_retry: {
            enabled: Boolean(document.getElementById('cfg-sonarr-ep-retry-enabled')?.checked),
            cron: (document.getElementById('cfg-sonarr-ep-retry-cron')?.value || '').trim() || '0 */6 * * *',
            min_air_delay_hours: parseInt(document.getElementById('cfg-sonarr-ep-retry-delay')?.value, 10) ?? 6,
            max_episodes_per_run: parseInt(document.getElementById('cfg-sonarr-ep-retry-max')?.value, 10) || 5,
            episode_cooldown_hours: parseInt(document.getElementById('cfg-sonarr-ep-retry-cooldown')?.value, 10) || 24
          }
        },
        radarr: {
          enabled: Boolean(document.getElementById('cfg-radarr-enabled')?.checked),
          url: (document.getElementById('cfg-radarr-url')?.value || '').trim(),
          api_key: (document.getElementById('cfg-radarr-key')?.value || '').trim(),
          missing_movie_search: {
            enabled: Boolean(document.getElementById('cfg-radarr-movie-search-enabled')?.checked),
            cron: (document.getElementById('cfg-radarr-movie-search-cron')?.value || '').trim() || '0 3 * * *',
            min_release_delay_days: parseInt(document.getElementById('cfg-radarr-movie-search-delay')?.value, 10) ?? 1,
            max_movies_per_run: parseInt(document.getElementById('cfg-radarr-movie-search-max')?.value, 10) || 5,
            movie_cooldown_days: parseInt(document.getElementById('cfg-radarr-movie-search-cooldown')?.value, 10) || 7
          }
        },
        extraction: {
          enabled: Boolean(document.getElementById('cfg-extract-enabled')?.checked),
          delete_after_import: Boolean(document.getElementById('cfg-extract-delete')?.checked),
          staging_suffix: (document.getElementById('cfg-extract-suffix')?.value || '').trim() || '_extracted'
        },
        database: {
          retention_days: parseInt(document.getElementById('cfg-db-retention')?.value, 10) || 30
        },
        path_mappings: mappings
      };
    }

    function checkSettingsDirty() {
      if (!settingsBaselineSnapshot) return false;
      return JSON.stringify(collectSettingsPayload()) !== settingsBaselineSnapshot;
    }

    function updateSettingsDirtyUI() {
      const isDirty = checkSettingsDirty();
      const bar = document.getElementById('settings-save-bar');
      const isSettingsActive = document.getElementById('tab-settings')?.style.display !== 'none';
      if (bar) {
        bar.classList.toggle('visible', isDirty && isSettingsActive);
      }
    }

    async function discardSettingsChanges() {
      await loadConfigFromApi();
      showToast('Settings changes discarded', 'info');
    }

    function setupSettingsDirtyTracking() {
      const pane = document.getElementById('tab-settings');
      if (pane) {
        pane.addEventListener('input', updateSettingsDirtyUI);
        pane.addEventListener('change', updateSettingsDirtyUI);
      }
      if (!settingsBaselineSnapshot) {
        settingsBaselineSnapshot = JSON.stringify(collectSettingsPayload());
      }
      window.addEventListener('beforeunload', (e) => {
        if (checkSettingsDirty()) {
          e.preventDefault();
          e.returnValue = '';
        }
      });
    }

    function addPathMappingRow(fromVal = '', toVal = '') {
      const container = document.getElementById('cfg-path-mappings-list');
      if (!container) return;
      const row = document.createElement('div');
      row.className = 'path-mapping-row';
      row.innerHTML = `
        <input type="text" class="form-input pm-from" placeholder="/remote/downloads" value="${escapeHtml(fromVal)}" style="flex: 1;">
        <span class="path-mapping-arrow">&rarr;</span>
        <input type="text" class="form-input pm-to" placeholder="/data/downloads" value="${escapeHtml(toVal)}" style="flex: 1;">
        <button type="button" class="btn btn-sm" onclick="this.parentElement.remove(); updateSettingsDirtyUI();" title="Remove">✕</button>
      `;
      container.appendChild(row);
      updateSettingsDirtyUI();
    }

    async function loadConfigFromApi() {
      try {
        const res = await fetch('/api/config');
        if (!res.ok) return;
        const cfg = await res.json();
        const g = cfg.general || {};
        const ai = cfg.ai || {};
        const gm = ai.gemini || {};
        const ol = ai.ollama || {};
        const s = cfg.sonarr || {};
        const r = cfg.radarr || {};
        const ex = cfg.extraction || {};
        const db = cfg.database || {};

        document.getElementById('cfg-dry-run').checked = Boolean(g.dry_run);
        document.getElementById('cfg-reject-low-conf').checked = Boolean(g.reject_on_low_confidence);
        document.getElementById('cfg-min-confidence').value = g.min_confidence ?? 0.85;
        document.getElementById('cfg-check-interval').value = g.check_interval_seconds ?? 30;
        document.getElementById('cfg-ai-cooldown').value = g.ai_cooldown_seconds ?? 4.0;
        document.getElementById('cfg-log-level').value = g.log_level || 'INFO';
        const tzEl = document.getElementById('cfg-timezone');
        if (tzEl) tzEl.value = g.timezone || '';
        const whInput = document.getElementById('cfg-webhook-url');
        if (whInput && window.location.origin) {
          whInput.value = `${window.location.origin}/api/webhook`;
        }

        document.getElementById('cfg-ai-provider').value = ai.provider || 'gemini';
        document.getElementById('cfg-ai-fallback').checked = ai.fallback_enabled !== false;
        document.getElementById('cfg-gemini-key').value = gm.api_key || '';
        document.getElementById('cfg-gemini-model').value = gm.model || 'auto';
        document.getElementById('cfg-ollama-url').value = ol.base_url || 'http://localhost:11434';
        document.getElementById('cfg-ollama-model').value = ol.model || 'qwen2.5:7b';
        if (document.getElementById('cfg-ollama-timeout')) {
          document.getElementById('cfg-ollama-timeout').value = ol.timeout_seconds || 60;
        }
        updateOllamaSummaryCards();

        document.getElementById('cfg-sonarr-enabled').checked = Boolean(s.enabled);
        document.getElementById('cfg-sonarr-url').value = s.url || 'http://localhost:8989';
        document.getElementById('cfg-sonarr-key').value = s.api_key || '';
        const spb = s.season_pack_backfill || {};
        document.getElementById('cfg-sonarr-backfill-enabled').checked = Boolean(spb.enabled);
        document.getElementById('cfg-sonarr-backfill-cron').value = spb.cron || '0 2 * * *';
        document.getElementById('cfg-sonarr-backfill-missing-days').value = spb.min_missing_days ?? 3;
        document.getElementById('cfg-sonarr-backfill-max-seasons').value = spb.max_seasons_per_run ?? 3;
        document.getElementById('cfg-sonarr-backfill-cooldown').value = spb.season_cooldown_days ?? 7;

        const mer = s.missing_episode_retry || {};
        document.getElementById('cfg-sonarr-ep-retry-enabled').checked = Boolean(mer.enabled);
        document.getElementById('cfg-sonarr-ep-retry-cron').value = mer.cron || '0 */6 * * *';
        document.getElementById('cfg-sonarr-ep-retry-delay').value = mer.min_air_delay_hours ?? 6;
        document.getElementById('cfg-sonarr-ep-retry-max').value = mer.max_episodes_per_run ?? 5;
        document.getElementById('cfg-sonarr-ep-retry-cooldown').value = mer.episode_cooldown_hours ?? 24;

        document.getElementById('cfg-radarr-enabled').checked = Boolean(r.enabled);
        document.getElementById('cfg-radarr-url').value = r.url || 'http://localhost:7878';
        document.getElementById('cfg-radarr-key').value = r.api_key || '';
        const mms = r.missing_movie_search || {};
        document.getElementById('cfg-radarr-movie-search-enabled').checked = Boolean(mms.enabled);
        document.getElementById('cfg-radarr-movie-search-cron').value = mms.cron || '0 3 * * *';
        document.getElementById('cfg-radarr-movie-search-delay').value = mms.min_release_delay_days ?? 1;
        document.getElementById('cfg-radarr-movie-search-max').value = mms.max_movies_per_run ?? 5;
        document.getElementById('cfg-radarr-movie-search-cooldown').value = mms.movie_cooldown_days ?? 7;

        document.getElementById('cfg-extract-enabled').checked = ex.enabled !== false;
        document.getElementById('cfg-extract-delete').checked = ex.delete_after_import !== false;
        document.getElementById('cfg-extract-suffix').value = ex.staging_suffix || '_extracted';
        document.getElementById('cfg-db-retention').value = db.retention_days ?? 30;

        const pmContainer = document.getElementById('cfg-path-mappings-list');
        if (pmContainer) {
          pmContainer.innerHTML = '';
          (cfg.path_mappings || []).forEach(m => addPathMappingRow(m.from || '', m.to || ''));
        }
        settingsBaselineSnapshot = JSON.stringify(collectSettingsPayload());
        updateSettingsDirtyUI();
      } catch (e) {
        console.error('Failed to load config:', e);
        showToast('Failed to load configuration', 'danger');
      }
    }

    async function saveConfigToApi() {
      const payload = collectSettingsPayload();

      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showToast('Configuration saved and hot-reloaded!', 'success');
          settingsBaselineSnapshot = JSON.stringify(collectSettingsPayload());
          updateSettingsDirtyUI();
        } else {
          showToast(data.message || 'Failed to save configuration', 'danger');
        }
      } catch (e) {
        console.error('Error saving configuration:', e);
        showToast('Network error saving configuration', 'danger');
      }
    }

    // ==========================================
    // Ollama Model Management
    // ==========================================
    let ollamaModelsList = [];
    let ollamaPullPollInterval = null;

    function updateOllamaSummaryCards() {
      const hostVal = document.getElementById('cfg-ollama-url')?.value || 'http://localhost:11434';
      const modelVal = document.getElementById('cfg-ollama-model')?.value || 'qwen2.5:7b';
      const timeoutVal = document.getElementById('cfg-ollama-timeout')?.value || '60';
      const sumHost = document.getElementById('summary-ollama-host');
      const sumModel = document.getElementById('summary-ollama-model');
      const sumTimeout = document.getElementById('summary-ollama-timeout');
      const statusUrl = document.getElementById('ollama-status-url');
      if (sumHost) sumHost.textContent = hostVal;
      if (sumModel) sumModel.textContent = modelVal;
      if (sumTimeout) sumTimeout.textContent = timeoutVal;
      if (statusUrl) statusUrl.textContent = `Configured Host: ${hostVal} (${timeoutVal}s timeout)`;
    }

    async function saveOllamaSettings() {
      const urlInput = document.getElementById('cfg-ollama-url');
      const modelInput = document.getElementById('cfg-ollama-model');
      const timeoutInput = document.getElementById('cfg-ollama-timeout');
      const newUrl = urlInput ? urlInput.value.trim() : 'http://localhost:11434';
      const newModel = modelInput ? modelInput.value.trim() : 'qwen2.5:7b';
      const newTimeout = timeoutInput ? (parseFloat(timeoutInput.value) || 60) : 60;

      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ai: {
              ollama: {
                base_url: newUrl,
                model: newModel,
                timeout_seconds: newTimeout
              }
            }
          })
        });
        if (res.ok) {
          showToast(`Ollama settings saved: ${newModel} @ ${newUrl} (${newTimeout}s)`, 'success');
          updateOllamaSummaryCards();
          await loadOllamaModels(false);
        } else {
          showToast('Failed to save Ollama settings', 'danger');
        }
      } catch (e) {
        showToast('Network error saving Ollama settings: ' + e.message, 'danger');
      }
    }

    function setOllamaHostPreset(presetUrl) {
      const urlInput = document.getElementById('cfg-ollama-url');
      if (urlInput) {
        urlInput.value = presetUrl;
        saveOllamaSettings();
      }
    }

    async function loadOllamaModels(showToastNotification = false) {
      const connStatus = document.getElementById('ollama-conn-status');
      const settingsBadge = document.getElementById('settings-ollama-badge');
      const currentUrl = document.getElementById('cfg-ollama-url')?.value || 'http://localhost:11434';
      updateOllamaSummaryCards();

      if (connStatus) {
        connStatus.textContent = '○ Checking...';
        connStatus.style.background = 'rgba(148, 163, 184, 0.12)';
        connStatus.style.color = 'var(--text-muted)';
      }
      if (settingsBadge) {
        settingsBadge.textContent = 'Checking...';
        settingsBadge.style.background = 'rgba(148, 163, 184, 0.12)';
        settingsBadge.style.color = 'var(--text-muted)';
      }

      try {
        const res = await fetch('/api/ollama/models');
        const data = await res.json();
        if (data.status === 'ok') {
          ollamaModelsList = data.models || [];
          if (connStatus) {
            connStatus.textContent = '● Connected';
            connStatus.style.background = 'rgba(34, 197, 94, 0.15)';
            connStatus.style.color = '#22c55e';
          }
          if (settingsBadge) {
            settingsBadge.textContent = '● Connected';
            settingsBadge.style.background = 'rgba(34, 197, 94, 0.15)';
            settingsBadge.style.color = '#22c55e';
          }
          renderOllamaModelsManager();
          if (showToastNotification) {
            showToast('Connected to Ollama', 'success');
          }
        } else {
          if (connStatus) {
            connStatus.textContent = '○ Offline / Not Configured';
            connStatus.style.background = 'rgba(148, 163, 184, 0.12)';
            connStatus.style.color = 'var(--text-muted)';
          }
          if (settingsBadge) {
            settingsBadge.textContent = '○ Offline';
            settingsBadge.style.background = 'rgba(148, 163, 184, 0.12)';
            settingsBadge.style.color = 'var(--text-muted)';
          }
          renderOllamaUnconfiguredState(currentUrl);
          if (showToastNotification) {
            showToast('Ollama is offline or unreachable at ' + currentUrl, 'warning');
          }
        }
      } catch (err) {
        if (connStatus) {
          connStatus.textContent = '○ Offline / Not Configured';
          connStatus.style.background = 'rgba(148, 163, 184, 0.12)';
          connStatus.style.color = 'var(--text-muted)';
        }
        if (settingsBadge) {
          settingsBadge.textContent = '○ Offline';
          settingsBadge.style.background = 'rgba(148, 163, 184, 0.12)';
          settingsBadge.style.color = 'var(--text-muted)';
        }
        renderOllamaUnconfiguredState(currentUrl);
        if (showToastNotification) {
          showToast('Cannot reach Ollama at ' + currentUrl, 'warning');
        }
      }
    }

    function renderOllamaUnconfiguredState(currentUrl) {
      const container = document.getElementById('ollama-models-container');
      if (!container) return;

      container.innerHTML = `
        <div style="padding: 1.25rem; background: rgba(0, 0, 0, 0.2); border: 1px solid var(--border); border-radius: 8px;">
          <div style="display: flex; align-items: flex-start; gap: 0.75rem; flex-wrap: wrap;">
            <span style="font-size: 2rem; line-height: 1;">🦙</span>
            <div style="flex: 1; min-width: 220px;">
              <div style="font-weight: 600; font-size: 1.05rem; color: var(--text-main); margin-bottom: 0.35rem;">
                Ollama Is Offline or Not Configured
              </div>
              <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 1rem; line-height: 1.5; word-break: break-all;">
                No local Ollama server was detected at <code>${escapeHtml(currentUrl)}</code>. AImportarr works seamlessly with or without Ollama.
              </p>

              <div class="ollama-unconfigured-grid">
                <div style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border); border-radius: 6px; padding: 0.85rem;">
                  <div style="font-weight: 600; font-size: 0.85rem; color: var(--accent); margin-bottom: 0.35rem;">✨ Using Google Gemini?</div>
                  <div style="font-size: 0.78rem; color: var(--text-muted); line-height: 1.4; margin-bottom: 0.65rem;">
                    If you use Google Gemini for AI evaluations, Ollama is entirely optional. AImportarr runs completely through your Gemini API key.
                  </div>
                  <button type="button" class="btn btn-secondary btn-sm" onclick="switchTab('settings')">
                    Configure Gemini in Settings &rarr;
                  </button>
                </div>

                <div style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border); border-radius: 6px; padding: 0.85rem;">
                  <div style="font-weight: 600; font-size: 0.85rem; color: var(--sonarr); margin-bottom: 0.35rem;">🐳 Running Ollama in Docker or LAN?</div>
                  <div style="font-size: 0.78rem; color: var(--text-muted); line-height: 1.4; margin-bottom: 0.65rem;">
                    In Docker, <code>localhost</code> points inside this container. Use <code>host.docker.internal</code> or your host LAN IP above.
                  </div>
                  <button type="button" class="btn btn-secondary btn-sm" onclick="setOllamaHostPreset('http://host.docker.internal:11434')">
                    Try host.docker.internal &rarr;
                  </button>
                </div>
              </div>

              <div style="font-size: 0.78rem; color: var(--text-dim); background: rgba(0, 0, 0, 0.35); padding: 0.65rem 0.85rem; border-radius: 6px; border: 1px solid var(--border-subtle); word-break: break-all;">
                <strong>Want to run local AI?</strong> Install Ollama on your host: <code style="color: #a5f3fc; word-break: break-all;">curl -fsSL https://ollama.com/install.sh | sh</code>
              </div>
            </div>
          </div>
        </div>
      `;
    }

    function renderOllamaModelsManager() {
      const container = document.getElementById('ollama-models-container');
      const countBadge = document.getElementById('ollama-installed-count');
      if (countBadge) countBadge.textContent = ollamaModelsList.length;
      if (!container) return;

      const activeModel = document.getElementById('cfg-ollama-model')?.value || 'qwen2.5:7b';

      if (ollamaModelsList.length === 0) {
        container.innerHTML = `
          <div style="text-align: center; padding: 2.5rem 1rem; color: var(--text-dim);">
            <div style="font-size: 2.2rem; margin-bottom: 0.5rem;">🦙</div>
            <div style="font-size: 1rem; font-weight: 500; color: var(--text-muted); margin-bottom: 0.25rem;">Connected to Ollama, but no models installed yet</div>
            <div style="font-size: 0.85rem;">Use the panel on the right to pull your first model (e.g. <code>qwen2.5:7b</code>).</div>
          </div>
        `;
        return;
      }

      let html = `
        <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(0, 0, 0, 0.25); border: 1px solid var(--border); border-radius: 6px; padding: 0.65rem 0.85rem; margin-bottom: 0.85rem; flex-wrap: wrap; gap: 0.5rem;">
          <div>
            <div style="font-size: 0.72rem; color: var(--text-dim); text-transform: uppercase; font-weight: 600; letter-spacing: 0.03em;">Active Engine in AImportarr</div>
            <div style="display: flex; align-items: center; gap: 0.5rem; margin-top: 0.15rem;">
              <span style="font-weight: 700; font-size: 0.95rem; color: var(--text-main);">${escapeHtml(activeModel)}</span>
              <span class="status-badge" style="background: rgba(34, 197, 94, 0.15); color: #22c55e; font-size: 0.7rem; padding: 0.15rem 0.45rem;">● Active</span>
            </div>
          </div>
          <div style="font-size: 0.75rem; color: var(--text-dim);">
            Click <strong>Set as Active</strong> on any installed model below to switch engines.
          </div>
        </div>

        <!-- Desktop View (>= 768px): Full Table -->
        <div class="ollama-models-desktop">
          <table class="data-table" style="width: 100%;">
            <thead>
              <tr>
                <th>Model Tag</th>
                <th>Parameters</th>
                <th>Quant</th>
                <th>Size</th>
                <th>Status</th>
                <th style="text-align: right;">Actions</th>
              </tr>
            </thead>
            <tbody>
      `;

      ollamaModelsList.forEach(m => {
        const isActive = (m.name === activeModel || m.model === activeModel);
        html += `
          <tr>
            <td>
              <div style="font-weight: 600; color: var(--text-main);">${escapeHtml(m.name)}</div>
              <div style="font-size: 0.72rem; color: var(--text-dim);">${escapeHtml(m.family || '')}</div>
            </td>
            <td><span class="badge" style="background: rgba(99, 102, 241, 0.15); color: var(--accent);">${escapeHtml(m.parameter_size || '—')}</span></td>
            <td><span style="font-size: 0.8rem; color: var(--text-muted);">${escapeHtml(m.quantization_level || '—')}</span></td>
            <td><span style="font-weight: 500;">${escapeHtml(m.size_human)}</span></td>
            <td>
              ${isActive 
                ? '<span class="status-badge" style="background: rgba(34, 197, 94, 0.15); color: #22c55e;">● Active</span>' 
                : '<span class="status-badge" style="background: rgba(148, 163, 184, 0.1); color: var(--text-dim);">Ready</span>'}
            </td>
            <td style="text-align: right;">
              <div style="display: flex; gap: 0.4rem; justify-content: flex-end;">
                ${!isActive ? `
                  <button class="btn btn-secondary btn-sm" onclick="setActiveOllamaModel('${escapeHtml(m.name)}')">
                    Set as Active
                  </button>
                ` : ''}
                <button class="btn btn-danger btn-sm" onclick="deleteOllamaModel('${escapeHtml(m.name)}')">
                  🗑️ Delete
                </button>
              </div>
            </td>
          </tr>
        `;
      });

      html += `
            </tbody>
          </table>
        </div>

        <!-- Mobile View (< 768px): Responsive Touch Cards -->
        <div class="ollama-models-mobile">
      `;

      ollamaModelsList.forEach(m => {
        const isActive = (m.name === activeModel || m.model === activeModel);
        html += `
          <div class="ollama-model-mobile-card ${isActive ? 'active-model' : ''}">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem;">
              <div style="min-width: 0; flex: 1;">
                <div style="font-weight: 700; font-size: 0.95rem; color: var(--text-main); word-break: break-all;">${escapeHtml(m.name)}</div>
                <div style="font-size: 0.72rem; color: var(--text-dim); margin-top: 0.15rem;">Family: ${escapeHtml(m.family || 'unknown')}</div>
              </div>
              <div style="display: flex; align-items: center; gap: 0.35rem; flex-shrink: 0;">
                <span class="badge" style="background: rgba(99, 102, 241, 0.15); color: var(--accent);">${escapeHtml(m.parameter_size || '—')}</span>
                ${isActive 
                  ? '<span class="status-badge" style="background: rgba(34, 197, 94, 0.15); color: #22c55e; font-size: 0.72rem; padding: 0.15rem 0.45rem;">● Active</span>' 
                  : '<span class="status-badge" style="background: rgba(148, 163, 184, 0.1); color: var(--text-dim); font-size: 0.72rem; padding: 0.15rem 0.45rem;">Ready</span>'}
              </div>
            </div>

            <div style="display: flex; gap: 0.85rem; font-size: 0.75rem; color: var(--text-muted); background: rgba(0, 0, 0, 0.2); padding: 0.45rem 0.65rem; border-radius: 6px; flex-wrap: wrap;">
              <div><span style="color: var(--text-dim);">Format:</span> <strong>${escapeHtml(m.quantization_level || '—')}</strong></div>
              <div><span style="color: var(--text-dim);">Size:</span> <strong>${escapeHtml(m.size_human || '—')}</strong></div>
            </div>

            <div style="display: flex; gap: 0.5rem; margin-top: 0.15rem;">
              ${!isActive ? `
                <button class="btn btn-secondary btn-sm" style="flex: 1; justify-content: center;" onclick="setActiveOllamaModel('${escapeHtml(m.name)}')">
                  ✨ Set as Active
                </button>
              ` : ''}
              <button class="btn btn-danger btn-sm" style="${isActive ? 'width: 100%;' : ''} justify-content: center;" onclick="deleteOllamaModel('${escapeHtml(m.name)}')">
                🗑️ Delete
              </button>
            </div>
          </div>
        `;
      });

      html += `
        </div>
      `;
      container.innerHTML = html;
    }

    async function setActiveOllamaModel(modelName) {
      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ai: {
              ollama: {
                model: modelName
              }
            }
          })
        });
        if (res.ok) {
          const mInput = document.getElementById('cfg-ollama-model');
          if (mInput) mInput.value = modelName;
          updateOllamaSummaryCards();
          renderOllamaModelsManager();
          showToast(`Active Ollama model set to "${modelName}"`, 'success');
        } else {
          showToast('Failed to update active model in configuration', 'danger');
        }
      } catch (err) {
        showToast('Error updating configuration: ' + err.message, 'danger');
      }
    }

    async function deleteOllamaModel(modelName) {
      if (!confirm(`Are you sure you want to delete model "${modelName}" from Ollama?\n\nThis will free up disk space on the Ollama host.`)) {
        return;
      }

      showToast(`Deleting model "${modelName}"...`, 'info');
      try {
        const res = await fetch('/api/ollama/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: modelName })
        });
        const data = await res.json();
        if (data.status === 'ok') {
          showToast(data.message || `Deleted model "${modelName}"`, 'success');
          await loadOllamaModels(false);
        } else {
          showToast(data.message || 'Failed to delete model', 'danger');
        }
      } catch (err) {
        showToast('Error deleting model: ' + err.message, 'danger');
      }
    }

    function setPullTag(tag) {
      const input = document.getElementById('ollama-pull-tag');
      if (input) {
        input.value = tag;
        input.focus();
      }
    }

    async function startOllamaPull() {
      const tagInput = document.getElementById('ollama-pull-tag');
      const tag = tagInput ? tagInput.value.trim() : '';
      if (!tag) {
        showToast('Please enter a model tag to pull (e.g. qwen2.5:7b)', 'warning');
        return;
      }

      const btn = document.getElementById('btn-pull-model');
      if (btn) btn.disabled = true;

      try {
        const res = await fetch('/api/ollama/pull', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: tag })
        });
        const data = await res.json();
        if (data.status === 'ok') {
          showToast(`Started downloading "${tag}" from Ollama...`, 'info');
          showPullProgress();
          startPollingPullStatus();
        } else {
          showToast(data.message || 'Failed to start pull', 'danger');
          if (btn) btn.disabled = false;
        }
      } catch (err) {
        showToast('Error starting pull: ' + err.message, 'danger');
        if (btn) btn.disabled = false;
      }
    }

    function showPullProgress() {
      const box = document.getElementById('ollama-pull-progress-box');
      if (box) box.style.display = 'block';
    }

    function startPollingPullStatus() {
      if (ollamaPullPollInterval) clearInterval(ollamaPullPollInterval);

      ollamaPullPollInterval = setInterval(async () => {
        try {
          const res = await fetch('/api/ollama/pull-status');
          const data = await res.json();
          const statusText = document.getElementById('ollama-pull-status-text');
          const percentText = document.getElementById('ollama-pull-percent-text');
          const bar = document.getElementById('ollama-pull-progress-bar');
          const detailText = document.getElementById('ollama-pull-detail-text');
          const btn = document.getElementById('btn-pull-model');

          if (data.status === 'pulling') {
            if (statusText) statusText.textContent = `Downloading ${data.model || ''}...`;
            if (percentText) percentText.textContent = `${data.percent || 0}%`;
            if (bar) {
              bar.style.width = `${Math.min(100, data.percent || 0)}%`;
              bar.style.background = 'var(--sonarr)';
            }
            if (detailText) {
              const detail = data.message || 'Downloading...';
              const totalStr = data.total > 0 ? ` (${formatBytesJs(data.completed)} / ${formatBytesJs(data.total)})` : '';
              detailText.textContent = `${detail}${totalStr}`;
            }
          } else if (data.status === 'success') {
            clearInterval(ollamaPullPollInterval);
            ollamaPullPollInterval = null;
            if (btn) btn.disabled = false;
            if (statusText) statusText.textContent = 'Complete! 🎉';
            if (percentText) percentText.textContent = '100%';
            if (bar) {
              bar.style.width = '100%';
              bar.style.background = 'var(--success)';
            }
            if (detailText) detailText.textContent = data.message || 'Model downloaded and ready!';
            showToast(data.message || 'Model installed successfully!', 'success');
            await loadOllamaModels(false);
          } else if (data.status === 'error') {
            clearInterval(ollamaPullPollInterval);
            ollamaPullPollInterval = null;
            if (btn) btn.disabled = false;
            if (statusText) statusText.textContent = 'Error';
            if (bar) bar.style.background = 'var(--danger)';
            if (detailText) detailText.textContent = data.message || 'Download failed.';
            showToast(data.message || 'Download failed', 'danger');
          }
        } catch (e) {
          console.warn('Pull status polling error:', e);
        }
      }, 1000);
    }

    function formatBytesJs(b) {
      if (!b || b <= 0) return '0 B';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      const i = Math.floor(Math.log(b) / Math.log(1024));
      return (b / Math.pow(1024, i)).toFixed(2) + ' ' + units[i];
    }

    // Initialize
    try {
      const savedInterval = localStorage.getItem('aimportarr_stats_interval');
      if (savedInterval !== null) {
        const sel = document.getElementById('refresh-interval');
        if (sel) {
          const match = Array.from(sel.options).some(o => o.value === savedInterval);
          if (match) {
            sel.value = savedInterval;
          }
        }
      }
    } catch (_) {}
    fetchData(true);
    loadConfigFromApi();
    loadOllamaModels(false);
    updateAutoRefresh();
    setupSettingsDirtyTracking();
  </script>
</body>
</html>
"""

RENDERED_DASHBOARD_HTML = DASHBOARD_HTML.replace("{{VERSION}}", __version__)


def format_bytes(b: int) -> str:
    """Format bytes into human-readable string (e.g. 4.68 GB)."""
    if not b or b <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = min(len(units) - 1, int(math.floor(math.log(b, 1024))))
    p = math.pow(1024, i)
    s = round(b / p, 2)
    return f"{s} {units[i]}"


def get_ollama_models(base_url: str) -> Dict[str, Any]:
    """Fetch installed models from Ollama's /api/tags endpoint."""
    url = f"{base_url.rstrip('/')}/api/tags"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_models = data.get("models", [])
            formatted = []
            for m in raw_models:
                sz = m.get("size", 0)
                details = m.get("details", {}) or {}
                formatted.append({
                    "name": m.get("name", ""),
                    "model": m.get("model", ""),
                    "size": sz,
                    "size_human": format_bytes(sz),
                    "modified_at": m.get("modified_at", ""),
                    "parameter_size": details.get("parameter_size", ""),
                    "quantization_level": details.get("quantization_level", ""),
                    "family": details.get("family", ""),
                })
            return {"status": "ok", "base_url": base_url, "models": formatted}
    except urllib.error.HTTPError as e:
        return {"status": "error", "base_url": base_url, "message": f"Ollama returned HTTP {e.code}", "models": []}
    except Exception as e:
        return {"status": "error", "base_url": base_url, "message": f"Cannot connect to Ollama at {base_url}: {e}", "models": []}


def delete_ollama_model(base_url: str, model_name: str) -> Tuple[bool, str]:
    """Delete an installed model from Ollama using /api/delete."""
    url = f"{base_url.rstrip('/')}/api/delete"
    payload = json.dumps({"name": model_name}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="DELETE"
    )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            return True, f"Successfully deleted model '{model_name}'"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body)
            msg = err_json.get("error", body)
        except Exception:
            msg = body or str(e)
        return False, f"Ollama returned HTTP {e.code}: {msg}"
    except Exception as e:
        return False, f"Failed to delete model: {e}"


class OllamaPullManager:
    """Tracks background pulls of Ollama models with live progress reporting."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active_pull: Dict[str, Any] = {
            "status": "idle",
            "model": None,
            "completed": 0,
            "total": 0,
            "percent": 0.0,
            "message": "",
            "started_at": 0.0,
        }

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._active_pull)

    def start_pull(self, base_url: str, model_name: str) -> Tuple[bool, str]:
        with self._lock:
            if self._active_pull["status"] == "pulling":
                current = self._active_pull["model"]
                return False, f"A pull is already in progress for '{current}'."
            self._active_pull = {
                "status": "pulling",
                "model": model_name,
                "completed": 0,
                "total": 0,
                "percent": 0.0,
                "message": f"Initiating pull for '{model_name}'...",
                "started_at": time.time(),
            }

        thread = threading.Thread(
            target=self._run_pull,
            args=(base_url, model_name),
            daemon=True,
        )
        thread.start()
        return True, "Pull started."

    def _run_pull(self, base_url: str, model_name: str) -> None:
        url = f"{base_url.rstrip('/')}/api/pull"
        payload = json.dumps({"name": model_name, "stream": True}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                for raw_line in resp:
                    if not raw_line:
                        continue
                    try:
                        data = json.loads(raw_line.decode("utf-8"))
                    except Exception:
                        continue

                    status_msg = data.get("status", "")
                    if status_msg == "success":
                        with self._lock:
                            self._active_pull["status"] = "success"
                            self._active_pull["percent"] = 100.0
                            self._active_pull["message"] = f"Successfully installed '{model_name}'!"
                        return

                    completed = data.get("completed", 0)
                    total = data.get("total", 0)
                    pct = round((completed / total * 100), 1) if total > 0 else 0.0

                    with self._lock:
                        self._active_pull["completed"] = completed
                        self._active_pull["total"] = total
                        self._active_pull["percent"] = pct
                        self._active_pull["message"] = status_msg

            with self._lock:
                if self._active_pull["status"] == "pulling":
                    self._active_pull["status"] = "success"
                    self._active_pull["percent"] = 100.0
                    self._active_pull["message"] = f"Finished pulling '{model_name}'."
        except Exception as e:
            logger.error(f"Error pulling Ollama model '{model_name}': {e}")
            with self._lock:
                self._active_pull["status"] = "error"
                self._active_pull["message"] = f"Pull failed: {e}"


ollama_pull_manager = OllamaPullManager()


class MetricsHTTPHandler(BaseHTTPRequestHandler):
    """HTTP Request handler for lightweight dashboard and REST metrics API."""

    def __init__(
        self,
        *args,
        metrics_instance: Optional[MetricsTracker] = None,
        config_instance: Optional[AppConfig] = None,
        on_config_updated: Optional[Callable[[AppConfig], None]] = None,
        on_trigger_scan: Optional[Callable[[str], None]] = None,
        config_path: Optional[str] = None,
        **kwargs,
    ):
        self.metrics = metrics_instance or default_metrics
        self.config = config_instance or load_config(config_path)
        self.on_config_updated = on_config_updated
        self.on_trigger_scan = on_trigger_scan
        self.config_path = config_path
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
            self._send_html(RENDERED_DASHBOARD_HTML)
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
            page = None
            search = None
            filter_val = None
            if "limit" in params:
                try:
                    limit = max(1, min(1000, int(params["limit"][0])))
                except (ValueError, IndexError):
                    pass
            if "page" in params:
                try:
                    page = max(1, int(params["page"][0]))
                except (ValueError, IndexError):
                    page = 1
            if "search" in params:
                search = params["search"][0].strip() or None
            if "filter" in params:
                filter_val = params["filter"][0].strip() or None

            self._send_json(self.metrics.get_processed_history(
                limit=limit,
                page=page,
                search=search,
                filter_status=filter_val,
            ))
            return

        # REST API: Token Usage History
        if path == "/api/tokens":
            limit = 100
            page = None
            search = None
            if "limit" in params:
                try:
                    limit = max(1, min(1000, int(params["limit"][0])))
                except (ValueError, IndexError):
                    pass
            if "page" in params:
                try:
                    page = max(1, int(params["page"][0]))
                except (ValueError, IndexError):
                    page = 1
            if "search" in params:
                search = params["search"][0].strip() or None

            self._send_json(self.metrics.get_token_history(
                limit=limit,
                page=page,
                search=search,
            ))
            return

        # REST API: Configuration
        if path == "/api/config":
            self._send_json(config_to_dict(self.config))
            return

        # REST API: Ollama Models List
        if path == "/api/ollama/models":
            base_url = self.config.ai.ollama.base_url
            self._send_json(get_ollama_models(base_url))
            return

        # REST API: Ollama Pull Status
        if path == "/api/ollama/pull-status":
            self._send_json(ollama_pull_manager.get_status())
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

        if path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                data = json.loads(body) if body.strip() else {}
                update_config_from_dict(self.config, data)
                saved_path = save_config(self.config, self.config_path)
                if self.on_config_updated:
                    self.on_config_updated(self.config)
                self._send_json({
                    "status": "ok",
                    "message": f"Configuration saved to {saved_path}",
                    "config": config_to_dict(self.config),
                })
            except Exception as e:
                logger.error(f"Error saving configuration via POST /api/config: {e}")
                self._send_json({"status": "error", "message": str(e)}, status=400)
            return

        if path == "/api/scan":
            if self.on_trigger_scan:
                self.on_trigger_scan("manual")
            self._send_json({
                "status": "ok",
                "message": "Immediate queue scan triggered.",
            })
            return

        if path == "/api/backfill":
            if self.on_trigger_scan:
                self.on_trigger_scan("backfill")
            self._send_json({
                "status": "ok",
                "message": "Season Pack Backfill check triggered.",
            })
            return

        if path == "/api/episode-retry":
            if self.on_trigger_scan:
                self.on_trigger_scan("episode_retry")
            self._send_json({
                "status": "ok",
                "message": "Missing Episode Retry check triggered.",
            })
            return

        if path == "/api/movie-search":
            if self.on_trigger_scan:
                self.on_trigger_scan("movie_search")
            self._send_json({
                "status": "ok",
                "message": "Missing Movie Search check triggered.",
            })
            return

        if path in ("/api/webhook", "/api/webhook/sonarr", "/api/webhook/radarr"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                data = json.loads(body) if body.strip() else {}
            except Exception:
                data = {}
            event_type = str(data.get("eventType") or "Webhook")
            if event_type.lower() == "test":
                logger.info("Received Test webhook from Sonarr/Radarr.")
                self._send_json({"status": "ok", "message": "Test webhook received successfully."})
                return
            logger.info(f"Received webhook event '{event_type}'; triggering immediate queue scan.")
            if self.on_trigger_scan:
                self.on_trigger_scan(f"webhook:{event_type}")
            self._send_json({
                "status": "ok",
                "message": f"Webhook '{event_type}' received; queue scan triggered.",
            })
            return

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
                if count > 0 and self.on_trigger_scan:
                    self.on_trigger_scan("retry_all")
                self._send_json({
                    "status": "ok",
                    "message": f"Queued {count} item(s) for immediate retry.",
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

                if data.get("cancel"):
                    success = self.metrics.cancel_item_retry(item_id)
                    self._send_json({
                        "status": "ok",
                        "message": f"Cancelled retry for item #{item_id}.",
                        "id": item_id,
                        "success": success
                    })
                    return

                success = self.metrics.mark_item_for_retry(item_id, allow_all=False)
                if success:
                    if self.on_trigger_scan:
                        self.on_trigger_scan(f"retry:{item_id}")
                    self._send_json({
                        "status": "ok",
                        "message": f"Item #{item_id} queued for immediate retry.",
                        "id": item_id,
                        "success": True
                    })
                else:
                    self._send_json({
                        "status": "error",
                        "message": f"Item #{item_id} cannot be retried (only skipped, failed, or dry_run items can be retried).",
                    }, status=400)
                return

            self.send_error(HTTPStatus.BAD_REQUEST, "Must specify 'id' or 'all: true'")
            return

        # REST API: Clear History & Metrics
        if path == "/api/history/clear":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                data = json.loads(body) if body.strip() else {}
            except Exception as e:
                logger.warning(f"Error parsing POST /api/history/clear payload: {e}")
                data = {}

            clear_items = bool(data.get("items", True))
            clear_tokens = bool(data.get("tokens", True))
            reset_counters = bool(data.get("counters", False))

            result = self.metrics.clear_history(
                clear_items=clear_items,
                clear_tokens=clear_tokens,
                reset_counters=reset_counters,
            )

            msg_parts = []
            if clear_items:
                msg_parts.append(f"{result.get('deleted_items', 0)} item(s)")
            if clear_tokens:
                msg_parts.append(f"{result.get('deleted_tokens', 0)} token call(s)")
            if reset_counters:
                msg_parts.append("reset counters")
            summary_msg = f"Cleared {', '.join(msg_parts)}." if msg_parts else "History cleared."

            self._send_json({
                "status": "ok",
                "message": summary_msg,
                "result": result,
            })
            return

        # Ollama Model Management: Pull
        if path == "/api/ollama/pull":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                data = json.loads(body) if body.strip() else {}
                model_name = str(data.get("name") or "").strip()
                if not model_name:
                    self._send_json({"status": "error", "message": "Model name is required"}, status=400)
                    return
                base_url = self.config.ai.ollama.base_url
                ok, msg = ollama_pull_manager.start_pull(base_url, model_name)
                if ok:
                    self._send_json({"status": "ok", "message": msg, "model": model_name})
                else:
                    self._send_json({"status": "error", "message": msg}, status=409)
            except Exception as e:
                logger.error(f"Error starting Ollama model pull: {e}")
                self._send_json({"status": "error", "message": str(e)}, status=500)
            return

        # Ollama Model Management: Delete
        if path == "/api/ollama/delete":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                data = json.loads(body) if body.strip() else {}
                model_name = str(data.get("name") or "").strip()
                if not model_name:
                    self._send_json({"status": "error", "message": "Model name is required"}, status=400)
                    return
                base_url = self.config.ai.ollama.base_url
                ok, msg = delete_ollama_model(base_url, model_name)
                if ok:
                    self._send_json({"status": "ok", "message": msg})
                else:
                    self._send_json({"status": "error", "message": msg}, status=500)
            except Exception as e:
                logger.error(f"Error deleting Ollama model: {e}")
                self._send_json({"status": "error", "message": str(e)}, status=500)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")

class WebServerRunner:
    """Manager for the background ThreadingHTTPServer."""

    def __init__(
        self,
        host: str,
        port: int,
        metrics_instance: Optional[MetricsTracker] = None,
        config_instance: Optional[AppConfig] = None,
        on_config_updated: Optional[Callable[[AppConfig], None]] = None,
        on_trigger_scan: Optional[Callable[[str], None]] = None,
        config_path: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.metrics = metrics_instance or default_metrics
        self.config = config_instance or load_config(config_path)
        self.on_config_updated = on_config_updated
        self.on_trigger_scan = on_trigger_scan
        self.config_path = config_path
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        handler_factory = lambda *args, **kwargs: MetricsHTTPHandler(
            *args,
            metrics_instance=self.metrics,
            config_instance=self.config,
            on_config_updated=self.on_config_updated,
            on_trigger_scan=self.on_trigger_scan,
            config_path=self.config_path,
            **kwargs,
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
