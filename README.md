# cacheout-mcp

MCP server for macOS disk cache management — lets AI agents free disk space on demand.

## Why

When you run AI agents (OpenClaw, Claude Code, etc.) on a Mac Mini or any macOS machine, disk pressure can silently degrade performance. Swap thrashing, failed builds, Docker OOM — all from running out of space that's locked up in developer caches.

**cacheout-mcp** gives your agent a set of tools to detect and fix disk pressure in real-time:

```
Agent detects 2 GB free → calls cacheout_smart_clean(target_gb=15) → 15 GB freed in 3 seconds
```

## Three Execution Modes

| Mode | When | How it works |
|------|------|-------------|
| **Socket** | Cacheout daemon running with Unix socket | Connects to daemon for real-time data, trend analysis, health scores |
| **App** | Cacheout.app installed, no running daemon | Delegates to Cacheout CLI binary (`--cli` flag) |
| **Standalone** | No Cacheout.app (headless servers) | Cleans caches directly via Python, reads sysctl for memory stats |

Mode is **auto-detected** at startup (socket → app → standalone). Override with `CACHEOUT_MODE=standalone` or `CACHEOUT_MODE=app`.

## Install

```bash
# From PyPI (when published)
pip install cacheout-mcp

# From source
cd cacheout-mcp
pip install -e .
```

## MCP Configuration

### Claude Code / Claude Desktop

Add to `~/.claude/claude_desktop_config.json` or your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "cacheout": {
      "command": "cacheout-mcp",
      "env": {}
    }
  }
}
```

### OpenClaw / Custom Agents

```json
{
  "mcpServers": {
    "cacheout": {
      "command": "python",
      "args": ["-m", "cacheout_mcp.server"],
      "env": {
        "CACHEOUT_MODE": "standalone"
      }
    }
  }
}
```

### With uv (no install needed)

```json
{
  "mcpServers": {
    "cacheout": {
      "command": "uvx",
      "args": ["cacheout-mcp"]
    }
  }
}
```

## Tools

### `cacheout_get_disk_usage`
Check current disk space. No parameters.

```json
→ {"total": "500.1 GB", "free": "23.4 GB", "used_percent": 95.3}
```

### `cacheout_scan_caches`
Scan all cache directories and report sizes. Optional filters:
- `categories`: List of slugs to scan (omit for all)
- `min_size_mb`: Only show categories above this size

```json
→ {"total_cleanable": "45.2 GB", "categories": [{"slug": "xcode_derived_data", "size_human": "15.0 GB", ...}]}
```

### `cacheout_clear_cache`
Clear specific categories by slug. Requires explicit category list.
- `categories`: **Required** list of slugs
- `dry_run`: Preview without deleting

```json
← {"categories": ["xcode_derived_data", "homebrew_cache"], "dry_run": false}
→ {"total_freed": "18.2 GB", "results": [...]}
```

### `cacheout_smart_clean`
**The primary tool for agents.** Specify how much space you need; it clears safest caches first.
- `target_gb`: **Required** — how many GB to free
- `dry_run`: Preview mode
- `include_caution`: Include Docker and other high-risk categories
- `free_memory`: Also run memory purge after disk cleanup (adds `memory_freed`, `purge_result` to response)

```json
← {"target_gb": 10.0}
→ {"target_met": true, "total_freed_human": "12.3 GB", "disk_after": {"free_gb": 17.5}}
```

### `cacheout_status`
Server status, mode, and available categories.

### `cacheout_get_memory_stats`
Check RAM, swap, memory pressure, and memory tier. No parameters.

```json
→ {"total_physical_mb": 16384.0, "memory_tier": "comfortable", "estimated_available_mb": 6096.0, ...}
```

### `cacheout_get_process_memory`
List top memory-consuming processes. Optional `top_n` and `sort_by` parameters.
Returns envelope: `{mode, capabilities, data: {processes, count, sort_by_applied}, partial}`.

### `cacheout_get_compressor_health`
Check macOS memory compressor ratio, compression/decompression rates, and thrashing detection. No parameters.
Returns envelope: `{mode, capabilities, data: {compressor_ratio, thrashing, ...}, partial}`.

### `cacheout_memory_intervention`
Run memory interventions. Required parameters:
- `intervention_name`: Canonical name (e.g., `"purge"`)
- `confirm`: `false` for dry-run preview, `true` to execute

Returns envelope: `{mode, capabilities, data: {dry_run, intervention, ...}, partial}`.

### `cacheout_system_health`
Combined disk + memory + alert health check with a 0-100 score. No parameters.
In socket mode, fetches from daemon. In standalone, computes locally.

### `cacheout_check_alerts`
Read watchdog alerts (near-zero cost file read). Optional `acknowledge` parameter.

### `cacheout_get_recommendations`
Get predictive memory/disk recommendations. Socket mode includes trend-based types
(`exhaustion_imminent`, `compressor_degrading`). Standalone returns snapshot types only.

### `cacheout_configure_autopilot`
Validate and apply autopilot/watchdog configuration. Required `config` parameter (dict with
`version`, `enabled`, optional `rules`/`webhook`/`telegram`). This is a write/validate/apply tool.

## Cache Categories (23 total)

| Slug | Risk | What it cleans |
|------|------|---------------|
| `xcode_derived_data` | Safe | Build artifacts, indexes |
| `uv_cache` | Safe | Fast Python installer cache |
| `homebrew_cache` | Safe | Downloaded bottles/tarballs |
| `npm_cache` | Safe | npm package cache |
| `yarn_cache` | Safe | Yarn package cache |
| `pnpm_store` | Safe | pnpm content-addressable store |
| `bun_cache` | Safe | Bun package manager cache |
| `typescript_cache` | Safe | TypeScript compiler + Next.js SWC cache |
| `playwright_browsers` | Safe | Playwright browser binaries |
| `cocoapods_cache` | Safe | CocoaPods specs and pods |
| `node_gyp_cache` | Safe | Native Node.js addon headers |
| `prisma_engines` | Safe | Prisma ORM query engine binaries |
| `swift_pm_cache` | Safe | Swift Package Manager cache |
| `gradle_cache` | Safe | Gradle build cache |
| `pip_cache` | Safe | Python pip cache |
| `chatgpt_desktop_cache` | Safe | ChatGPT desktop app cache |
| `vscode_cache` | Safe | VS Code updates and extensions |
| `electron_cache` | Safe | Shared Electron framework cache |
| `browser_caches` | Review | Brave/Chrome cached web content |
| `xcode_device_support` | Review | iOS device debug symbols |
| `simulator_devices` | Review | iOS/watchOS simulator data (uses xcrun) |
| `torch_hub` | Review | PyTorch models (slow to re-download) |
| `docker_disk` | Caution | Docker virtual disk (all images/containers) |

## Smart Clean Priority

When `smart_clean` is called, categories are cleaned in this order:
1. **Safe** categories, sorted by clean_priority (build artifacts first)
2. **Review** categories (browser caches, device support)
3. **Caution** categories (Docker) — only if `include_caution=true`

Stops as soon as `target_gb` is freed.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHEOUT_MODE` | auto-detect | Force `standalone` or `app` mode |
| `CACHEOUT_BIN` | auto-detect | Path to Cacheout binary |

## Adding the CLI to Cacheout.app

If you maintain the Cacheout Swift app, add the `CLIHandler.swift` file to your Sources
and update `CacheoutApp.swift` to check `CLIHandler.shouldHandleCLI()` on init.
This enables `Cacheout --cli scan`, `Cacheout --cli clean`, etc. for app-mode integration.

## License

MIT
