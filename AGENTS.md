# Cacheout MCP — Agent Integration Guide

## Overview

The `cacheout-mcp` server exposes macOS cache management as MCP tools. AI agents (Claude, OpenClaw, custom) can scan, analyze, and clean developer caches without a GUI — ideal for automated disk pressure management, CI/CD cleanup, and proactive maintenance.

## Quick Start

```json
{
  "mcpServers": {
    "cacheout": {
      "command": "uvx",
      "args": ["--from", "cacheout-mcp", "cacheout-mcp"]
    }
  }
}
```

## Available Tools

### `cacheout_get_disk_usage`
**Read-only.** Returns total/free/used disk space with percentages. Use this first to assess whether cleanup is needed.

### `cacheout_scan_caches`
**Read-only.** Scans all 23 cache categories (or a filtered subset) and returns sizes sorted largest-first. Accepts optional `categories` (list of slugs) and `min_size_mb` filters.

### `cacheout_clear_cache`
**Destructive.** Clears specified categories by slug. Supports `dry_run: true` for preview. Always scan first.

### `cacheout_smart_clean`
**Destructive.** The primary agent tool. Specify `target_gb` and the server clears caches in priority order (safest first) until the target is met. Supports `dry_run`, `include_caution`, and `free_memory` (also runs memory purge after disk cleanup; adds `memory_freed`/`purge_result` to response).

### `cacheout_status`
**Read-only.** Returns server mode (`standalone`, `app`, or `socket`), version, and full category list with risk levels.

### `cacheout_get_memory_stats`
**Read-only.** Returns `total_physical_mb`, swap usage (`swap_used_mb`), memory pressure level, memory tier (`abundant`/`comfortable`/`moderate`/`constrained`/`critical`), and `estimated_available_mb`. No `physical_ram_gb` field — use `total_physical_mb / 1024` if needed.

### `cacheout_get_process_memory`
**Read-only.** Lists top memory-consuming processes. Accepts optional `top_n` and `sort_by` parameters. Returns envelope: `{mode, capabilities, data: {processes, count, sort_by_applied}, partial}`.

### `cacheout_get_compressor_health`
**Read-only.** Returns compressor ratio, compression/decompression rates (dual-sample), thrashing detection, and pressure label. Returns envelope: `{mode, capabilities, data: {compressor_ratio, thrashing, ...}, partial}`.

### `cacheout_memory_intervention`
**Potentially disruptive.** Runs memory interventions. Parameters: `intervention_name` (required, e.g., `"purge"`), `confirm` (default `false` for dry-run, `true` to execute). In standalone mode, only `purge` is supported (calls `/usr/sbin/purge`). Returns envelope: `{mode, capabilities, data: {dry_run, intervention, ...}, partial}`.

### `cacheout_system_health`
**Read-only.** Combined health check with a 0–100 score. In socket mode, fetches from daemon (<1ms). In standalone, computes locally from memory stats + swap. Returns: `{score, source, alerts, _meta}`.

### `cacheout_check_alerts`
**Read-only.** Reads watchdog alert sentinel (`~/.cacheout/alert.json`). Near-zero cost. Supports `acknowledge: true` to clear alerts.

### `cacheout_get_recommendations`
**Read-only.** Returns predictive recommendations. In socket/daemon mode: includes trend-based types (`exhaustion_imminent`, `compressor_degrading`). In standalone mode: snapshot types only (`compressor_low_ratio`, `swap_pressure`).

### `cacheout_configure_autopilot`
**Config (write-only).** Validates and applies autopilot/watchdog configuration. Required `config` parameter: dict with `version` (must be 1), `enabled` (bool), optional `rules`/`webhook`/`telegram`. No read/get-current-config path — this is a validate-then-write tool.

## Category Slugs & Risk Levels (23 categories)

| Slug | Name | Risk | Priority |
|------|------|------|----------|
| `xcode_derived_data` | Xcode DerivedData | ✅ safe | 10 |
| `uv_cache` | uv Cache | ✅ safe | 15 |
| `homebrew_cache` | Homebrew Cache | ✅ safe | 15 |
| `npm_cache` | npm Cache | ✅ safe | 20 |
| `yarn_cache` | Yarn Cache | ✅ safe | 20 |
| `pnpm_store` | pnpm Store | ✅ safe | 20 |
| `bun_cache` | Bun Cache | ✅ safe | 20 |
| `typescript_cache` | TypeScript Build Cache | ✅ safe | 20 |
| `playwright_browsers` | Playwright Browsers | ✅ safe | 25 |
| `cocoapods_cache` | CocoaPods Cache | ✅ safe | 25 |
| `node_gyp_cache` | node-gyp Cache | ✅ safe | 25 |
| `prisma_engines` | Prisma Engines | ✅ safe | 25 |
| `swift_pm_cache` | Swift PM Cache | ✅ safe | 25 |
| `gradle_cache` | Gradle Cache | ✅ safe | 30 |
| `pip_cache` | pip Cache | ✅ safe | 30 |
| `chatgpt_desktop_cache` | ChatGPT Desktop Cache | ✅ safe | 30 |
| `vscode_cache` | VS Code Cache | ✅ safe | 35 |
| `electron_cache` | Electron Cache | ✅ safe | 35 |
| `browser_caches` | Browser Caches | 🟡 review | 40 |
| `xcode_device_support` | Xcode Device Support | 🟡 review | 45 |
| `simulator_devices` | Simulator Devices | 🟡 review | 50 |
| `torch_hub` | PyTorch Hub Models | 🟡 review | 55 |
| `docker_disk` | Docker Disk Image | 🔴 caution | 90 |

**Priority** determines smart_clean order — lower numbers are cleaned first (safest).

## Agent Workflows

### Proactive Disk Monitor
```
1. cacheout_get_disk_usage → check free_gb
2. If free_gb < 10: cacheout_smart_clean(target_gb=15, dry_run=true) → preview
3. If acceptable: cacheout_smart_clean(target_gb=15) → execute
4. Report before/after disk state
```

### Targeted Category Cleanup
```
1. cacheout_scan_caches(min_size_mb=500) → find large caches
2. cacheout_clear_cache(categories=["xcode_derived_data", "npm_cache"])
3. cacheout_get_disk_usage → verify space recovered
```

### Pre-Build Space Check
```
1. cacheout_get_disk_usage → check if enough room for build
2. If tight: cacheout_smart_clean(target_gb=5, dry_run=true)
3. Execute if needed, then proceed with build
```

### Docker Reset (Caution)
```
1. cacheout_scan_caches(categories=["docker_disk"]) → check Docker size
2. Warn user: Docker cleanup destroys images/containers/volumes
3. cacheout_clear_cache(categories=["docker_disk"]) → only with explicit consent
```

## Execution Modes

The server auto-detects its mode at startup (checked in order):

1. **Socket**: If a Cacheout daemon is running with a Unix socket at `~/.cacheout/status.sock`, connects for real-time trend data, health scores, and process scans.
2. **App**: If Cacheout.app CLI binary is found, delegates to `--cli` commands. Shares the cleanup log.
3. **Standalone** (fallback): Scans and cleans directly via Python. Reads sysctl for memory stats. No dependencies beyond the MCP server.

Override with environment variables:
```bash
CACHEOUT_MODE=standalone  # Force standalone
CACHEOUT_BIN=/path/to/Cacheout  # Force app mode with specific binary
```

### Mode-Dependent Tool Behavior

| Tool | Socket | App | Standalone |
|------|--------|-----|-----------|
| Disk/scan/clean tools | Full | Full | Full |
| `cacheout_get_memory_stats` | Full | Full (via CLI) | sysctl-based |
| `cacheout_get_recommendations` | Trend + snapshot types | Snapshot types | Snapshot types only |
| `cacheout_system_health` | Daemon health score | Computed locally | Computed locally |
| `cacheout_configure_autopilot` | Validates + hot-reloads daemon | Validates + writes file | Validates + writes file |
| `cacheout_check_alerts` | MCP-only (reads sentinel) | MCP-only | MCP-only |

## Safety Guarantees

1. **No admin privileges** — only touches `~/Library/` and `~/.` directories
2. **Directory preservation** — contents are deleted, parent directories remain
3. **Risk-tiered** — safe/review/caution levels prevent accidental Docker nuking
4. **smart_clean guards** — caution-level categories only cleaned when 80%+ of target already met
5. **Shared cleanup log** — all deletions logged to `~/.cacheout/cleanup.log` with timestamps
6. **dry_run support** — every destructive tool supports preview mode

## Response Format

All tools return JSON. Scan results include `size_bytes`, `size_human`, `item_count`, `risk_level`, `rebuild_note`. Clean results include `bytes_freed`, `success`, `error`. Smart clean includes `disk_before`/`disk_after` snapshots.
