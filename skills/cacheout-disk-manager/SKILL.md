# cacheout-disk-manager

Proactive disk space management for macOS agents using cacheout-mcp. Designed for OpenClaw and Claude Code agents running on Mac Minis that need to maintain free SSD space for swap, builds, and Docker operations.

## When to Use This Skill

Use this skill when ANY of these conditions apply:

- Agent detects low disk space (< 15 GB free)
- Before starting a large build, Docker pull, or model download
- Periodic health checks (every 30-60 minutes during long sessions)
- After a failed operation that may be disk-space related (ENOSPC, Docker OOM, swap thrashing)
- System feels sluggish (potential swap pressure from low disk)

## Prerequisites

The `cacheout-mcp` server must be configured in your MCP settings. Add to your agent's MCP config:

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

If Cacheout.app is installed on the machine, omit the `env` block — app mode is auto-detected and provides exact parity with the GUI.

For uv-based setups (no install needed):
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

## Two-Tier Monitoring Architecture

Cacheout uses a two-tier system so agents don't waste cycles polling disk stats:

**Tier 1 — Watchdog Daemon (Autopilot, no agent involvement)**
A launchd daemon (`com.cacheout.watchdog`) runs every 30 seconds, tracking disk/swap/memory over a 5-minute rolling window. It detects rapid deterioration using rate-of-change thresholds (disk dropping >5 GB/5min, swap rising >3 GB/5min) and hard floors (disk <5 GB, swap >40 GB, memory_pressure critical). When triggered, it runs emergency cleanup via `Cacheout --cli smart-clean` and writes an alert sentinel to `~/.cacheout/alert.json`.

**Tier 2 — Agent Advisory (via `cacheout_check_alerts`)**
Agents call `cacheout_check_alerts` at natural checkpoints (start of tasks, before builds, after errors). This is a near-zero-cost file read — no scanning, no computation. If the watchdog has flagged something, the agent sees the alert and can take targeted action with `smart_clean`.

This means: even if no agent is running, the watchdog protects the system. When an agent IS running, it picks up alerts cheaply without needing its own monitoring loop.

## Available Tools

| Tool | Purpose | Destructive? |
|------|---------|-------------|
| `cacheout_check_alerts` | Read watchdog alerts (near-zero-cost) | No |
| `cacheout_get_disk_usage` | Check free/used/total disk space | No |
| `cacheout_scan_caches` | Scan all cache categories and report sizes | No |
| `cacheout_clear_cache` | Clear specific cache categories by slug | **Yes** |
| `cacheout_smart_clean` | Intelligently free target GB (safest first) | **Yes** |
| `cacheout_get_memory_stats` | Check RAM, swap, pressure, memory tier | No |
| `cacheout_get_recommendations` | Get predictive memory recommendations | No |
| `cacheout_status` | Server mode, binary path, available categories | No |

## Pre-Task Memory Check

Before spawning memory-intensive processes (Ollama, large model inference, Docker builds), check whether the system has enough RAM to avoid swap thrashing and OOM kills.

### Memory Check Decision Tree

```
1. Call cacheout_get_memory_stats
   │
   ├─ memory_tier = "abundant" or "comfortable"
   │  └─ Proceed — system has headroom
   │
   ├─ memory_tier = "moderate"
   │  └─ Proceed with caution. Monitor after launch.
   │
   ├─ memory_tier = "constrained" or "critical"
   │  ├─ Run: Cacheout --cli purge        (reclaims inactive/compressed pages)
   │  ├─ Recheck: cacheout_get_memory_stats
   │  │   ├─ Now "moderate" or better → proceed
   │  │   └─ Still constrained/critical:
   │  │       ├─ Run: Cacheout --cli smart-clean <gb>  (free disk-backed caches for swap room)
   │  │       ├─ Recheck: cacheout_get_memory_stats
   │  │       └─ If still insufficient → advise user to close apps or reduce model size
   │  └─ (end)
   │
   └─ estimated_available_mb < model requirement (see table below)
      └─ Same intervention path as constrained/critical above
```

### Model Size → Memory Requirements

| Model Size | Approx. RAM Needed | Minimum `estimated_available_mb` |
|-----------|-------------------|--------------------------------|
| 7B (Q4)  | ~4 GB             | 4096                           |
| 13B (Q4) | ~8 GB             | 8192                           |
| 34B (Q4) | ~20 GB            | 20480                          |
| 70B (Q4) | ~40 GB            | 40960                          |

These are rough estimates for quantized (Q4) GGUF models. Full-precision models need ~2× more.

### Interpreting Compressor Health

The `compressor_ratio` field indicates how effectively macOS is compressing memory:

- **ratio > 3.0** — Good compression efficiency. The compressor is working well.
- **ratio 1.5–3.0** — Moderate. Compression is helping but memory is getting tight.
- **ratio < 1.5** — Poor compression / memory thrashing. Data isn't compressing well, meaning the system is likely swapping heavily. Intervene immediately.
- **ratio = 0** — Nothing is compressed (normal when memory usage is low).

### Tools Used in This Workflow

| Step | Tool | Notes |
|------|------|-------|
| Check memory | `cacheout_get_memory_stats` | MCP tool, returns memory_tier + estimated_available_mb |
| Reclaim RAM | `Cacheout --cli purge` | CLI command, runs `/usr/sbin/purge` to reclaim inactive pages |
| Free disk for swap | `Cacheout --cli smart-clean <gb>` | CLI command, frees disk caches (improves swap headroom) |
| Check alerts | `cacheout_check_alerts` | MCP tool, reads watchdog sentinel |

## Predictive Recommendations

The `cacheout_get_recommendations` tool returns advisory recommendations about memory health. Results vary by mode:

### Recommendation Types

| Type | Description | Modes | Trend? |
|------|-------------|-------|--------|
| `exhaustion_imminent` | Time-to-exhaustion prediction below threshold | daemon only | Yes |
| `compressor_degrading` | Compression ratio declining over multiple samples | daemon only | Yes |
| `compressor_low_ratio` | Single-sample compression ratio below 2.0 | all modes | No (snapshot) |
| `high_growth_process` | Process at/near lifetime peak with large footprint | daemon, cli | No (snapshot) |
| `rosetta_detected` | Rosetta-translated process consuming significant memory | daemon, cli | No (snapshot) |
| `agent_memory_pressure` | Known AI agent with large memory footprint | daemon, cli | No (snapshot) |
| `swap_pressure` | Swap usage above 50% of physical RAM | all modes | No (snapshot) |

**Key distinction:** `compressor_degrading` (trend-based, daemon-only) vs `compressor_low_ratio` (snapshot, all modes). Standalone mode never produces `compressor_degrading`.

### Interpreting Confidence Levels

- **high**: Daemon mode with sufficient history (30+ samples) or fresh process scan (<30s old)
- **low**: CLI one-shot, standalone sysctl read, or stale daemon cache

### Recommendation Decision Tree

```
1. Call cacheout_get_recommendations
   │
   ├─ recommendations = [] (empty)
   │  └─ System looks healthy. No action needed.
   │
   ├─ _meta.partial = true
   │  └─ Results are degraded. In standalone/CLI modes, trend-based types
   │     (exhaustion_imminent, compressor_degrading) are absent. In socket mode,
   │     partial=true means the daemon's process scan was incomplete — trend-based
   │     types may still be present but process-based recommendations have low
   │     confidence. Act on available types but note confidence levels.
   │
   ├─ type = "compressor_low_ratio" or "compressor_degrading"
   │  ├─ confidence = "high" → Strong signal. Run purge, investigate memory consumers.
   │  └─ confidence = "low"  → Snapshot only. Monitor; recheck in 2-5 minutes.
   │
   ├─ type = "swap_pressure"
   │  └─ Swap > 50% of physical RAM. Run smart_clean to free disk for swap headroom.
   │
   ├─ type = "high_growth_process" or "agent_memory_pressure"
   │  └─ Consider restarting the named process or reducing its memory allocation.
   │
   ├─ type = "rosetta_detected"
   │  └─ Rosetta process using >200 MB. Consider native ARM build if available.
   │
   └─ type = "exhaustion_imminent"
      └─ URGENT: System predicted to exhaust memory soon.
         Run purge + smart_clean immediately. Investigate top memory consumers.
```

## Decision Framework

### Step 0: Check Watchdog Alerts (Always Do This First)

```
Call: cacheout_check_alerts
```

This reads `~/.cacheout/alert.json` — a tiny file written by the watchdog daemon. Near-zero cost.

Interpret the result:
- **alert: null** — No active alert. System is healthy. You can skip Steps 1-3 unless you have reason to check deeper.
- **alert.level: "warning"** — Watchdog detected rapid disk/swap deterioration. Review triggers and proceed to Step 3 (smart_clean with the recommended target_gb).
- **alert.level: "emergency"** — Watchdog already ran emergency cleanup. Check if it freed enough space. If not, run smart_clean with a higher target.

After handling an alert, acknowledge it:
```
Call: cacheout_check_alerts
  - acknowledge: true
```

### Step 1: Check Disk Pressure (If No Alert, or for Details)

```
Call: cacheout_get_disk_usage
```

Interpret the result:
- **free_gb > 30**: No action needed. Disk is healthy.
- **free_gb 15-30**: Monitor. Consider scanning to know what's available.
- **free_gb 5-15**: Action recommended. Run smart_clean with dry_run first.
- **free_gb < 5**: URGENT. Run smart_clean immediately to prevent swap death.

### Step 2: Understand What's Cleanable (Optional)

```
Call: cacheout_scan_caches
  - min_size_mb: 100  (skip noise)
```

This tells you exactly where the space is locked up. Useful for reporting to the user or deciding whether to proceed.

### Step 3: Free Space

**Option A — Smart Clean (Recommended)**
Let the server decide what to clean, safest first:

```
Call: cacheout_smart_clean
  - target_gb: 15.0      (how much space you need freed)
  - dry_run: true         (preview first — ALWAYS do this on first run)
```

Review the dry_run output, then if satisfied:

```
Call: cacheout_smart_clean
  - target_gb: 15.0
  - dry_run: false
```

**Option B — Targeted Clean**
If you know exactly which caches to clear:

```
Call: cacheout_clear_cache
  - categories: ["xcode_derived_data", "homebrew_cache", "npm_cache"]
  - dry_run: false
```

### Step 4: Verify

```
Call: cacheout_get_disk_usage
```

Confirm free_gb has increased to acceptable levels.

## Smart Clean Priority Order

The smart_clean tool clears caches in this order (safest first):

1. **Xcode DerivedData** (safe) — build artifacts, always regenerates
2. **uv Cache** (safe) — fast Python installer cache, often 1+ GB
3. **Homebrew Cache** (safe) — downloaded bottles/tarballs
4. **npm Cache** (safe) — package download cache
5. **Yarn Cache** (safe) — package download cache
6. **pnpm Store** (safe) — content-addressable store
7. **Bun Cache** (safe) — Bun package manager cache
8. **TypeScript Build Cache** (safe) — tsc disk cache and Next.js SWC
9. **Playwright Browsers** (safe) — browser binaries
10. **CocoaPods Cache** (safe) — specs and pods
11. **node-gyp Cache** (safe) — native addon headers
12. **Prisma Engines** (safe) — ORM query engine binaries
13. **Swift PM Cache** (safe) — package manager cache
14. **Gradle Cache** (safe) — build cache
15. **pip Cache** (safe) — Python package cache
16. **ChatGPT Desktop Cache** (safe) — desktop app cache
17. **VS Code Cache** (safe) — updates and extensions
18. **Electron Cache** (safe) — shared framework cache
19. **Browser Caches** (review) — Brave/Chrome cached content
20. **Xcode Device Support** (review) — iOS debug symbols
21. **Simulator Devices** (review) — iOS/watchOS simulator data (uses xcrun cleanup)
22. **PyTorch Hub Models** (review) — ML models (slow to re-download)
23. **Docker Disk** (caution) — virtual disk with ALL images/containers

Docker is only touched if `include_caution: true` AND 80%+ of the target has already been freed from safer categories.

## Category Slugs Reference

Use these exact slugs with `cacheout_clear_cache` and `cacheout_scan_caches`:

```
xcode_derived_data    homebrew_cache        npm_cache
yarn_cache            pnpm_store            playwright_browsers
cocoapods_cache       swift_pm_cache        gradle_cache
pip_cache             browser_caches        vscode_cache
electron_cache        xcode_device_support  docker_disk
simulator_devices     bun_cache             node_gyp_cache
uv_cache              torch_hub             chatgpt_desktop_cache
prisma_engines        typescript_cache
```

## Proactive Monitoring Pattern

For long-running agent sessions, implement this lightweight check at natural task boundaries:

```
1. Call cacheout_check_alerts              ← near-zero cost, always safe
2. If alert exists:
   a. Log alert level and triggers
   b. Call cacheout_smart_clean(target_gb=alert.recommended_target_gb, dry_run=true)
   c. Review, then smart_clean(dry_run=false)
   d. Call cacheout_check_alerts(acknowledge=true)
   e. Verify with cacheout_get_disk_usage
3. If no alert: continue working — the watchdog daemon has your back
```

**When to call `cacheout_check_alerts`:**
- At the START of any new task or build
- BEFORE large disk operations (Docker pull, model download, Xcode build)
- AFTER encountering ENOSPC, OOM, or unexpected slowdowns
- You do NOT need a polling loop — the watchdog daemon monitors continuously

**When to escalate to `cacheout_get_disk_usage` directly:**
- If you want exact current numbers (not just alert/no-alert)
- If planning a large operation and want to pre-check headroom
- If smart_clean didn't free enough and you need to assess remaining options

## Swap Protection Guidelines

macOS needs free SSD space for virtual memory (swap). When disk space drops below ~5 GB:
- The system starts thrashing swap, causing severe slowdowns
- Builds fail with ENOSPC errors
- Docker containers get OOM-killed
- The agent itself may become unresponsive

**Recommended minimum free space targets:**
- General operation: **15 GB** free
- Before large builds (Xcode, Docker): **25 GB** free
- Before model downloads or large data operations: **30 GB** free

## Error Recovery

If you encounter these errors, run cacheout immediately:

| Error Pattern | Action |
|--------------|--------|
| `ENOSPC` / "No space left on device" | `smart_clean(target_gb=20.0)` |
| Docker OOM / container killed | `smart_clean(target_gb=25.0, include_caution=true)` |
| Xcode build failed (disk) | `clear_cache(categories=["xcode_derived_data"])` |
| `swift build` failure | `clear_cache(categories=["swift_pm_cache", "xcode_derived_data"])` |
| npm/yarn/bun install failure | `clear_cache(categories=["npm_cache", "yarn_cache", "bun_cache"])` |
| `uv`/`pip` install failure | `clear_cache(categories=["uv_cache", "pip_cache"])` |
| Simulator disk bloat | `clear_cache(categories=["simulator_devices"])` (uses xcrun) |
| `prisma generate` failure | `clear_cache(categories=["prisma_engines"])` |
| System sluggish / swap pressure | `smart_clean(target_gb=30.0)` |

## Watchdog Daemon

The watchdog daemon (`com.cacheout.watchdog`) is a launchd agent that runs independently of any AI agent. It monitors system pressure every 30 seconds using rate-of-change detection over a 5-minute rolling window.

**Thresholds:**
- Disk velocity: alert if free space drops >5 GB in 5 minutes
- Swap velocity: alert if swap grows >3 GB in 5 minutes
- Hard floor: immediate cleanup if disk <5 GB, swap >40 GB, or memory_pressure critical

**Files written by the watchdog:**
- `~/.cacheout/watchdog-history.json` — Rolling window of disk/swap/pressure samples
- `~/.cacheout/watchdog.log` — Daemon log (rotated at 1 MB)
- `~/.cacheout/alert.json` — Alert sentinel (read by `cacheout_check_alerts`, auto-cleared after 10 min)

**Installation:**
```bash
cd /path/to/cacheout/Watchdog
bash install-watchdog.sh
```

**Management:**
```bash
# Check status
launchctl list | grep cacheout

# Stop
launchctl bootout gui/$(id -u)/com.cacheout.watchdog

# Restart
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cacheout.watchdog.plist

# View recent logs
tail -20 ~/.cacheout/watchdog.log
```

## Hardware-Tier Limitations

Different Mac hardware tiers have distinct constraints that affect cache management strategy:

### 8 GB Machines (Critical)

- **Memory is the bottleneck, not disk.** Agents compete with macOS for the same 8 GB.
- `memory_tier` will frequently report `constrained` or `critical` under normal agent load.
- **Swap pressure is chronic** — even moderate agent activity pushes swap to multi-GB. Keep disk free space above 20 GB to ensure swap headroom.
- `cacheout_memory_intervention(intervention_name="purge", confirm=true)` is essential before any memory-intensive task.
- Avoid running Ollama or large models entirely — even 7B Q4 models need ~4 GB, leaving almost nothing for the OS.
- Docker is risky on 8 GB — container OOM kills are common. Use `include_caution: true` with smart_clean to reclaim Docker space.
- **Recommendation:** Run `cacheout_check_alerts` more frequently (every task boundary, not just every 30 min).

### 16 GB Machines (Standard)

- This is the baseline configuration for most agent workflows.
- `memory_tier` typically reports `comfortable` or `moderate` under normal load.
- 7B Q4 models run well; 13B Q4 models are feasible with careful monitoring.
- Smart clean targets of 15–25 GB are usually sufficient.
- No special workarounds needed — all features work as documented.

### 128 GB Machines (memlimit Caveats)

- macOS may impose a per-process memory limit (`memlimit`) that caps how much RAM a single process can use — even though the system has 128 GB total.
- `total_physical_mb` will report ~131072 (128 GB), but `estimated_available_mb` may be lower than expected if memlimit is active.
- **memlimit workaround status:** The `cacheout_get_memory_stats` tool reads `hw.memsize` and memory pressure, but does not currently detect per-process memlimit. Agents should check process-level limits via `ulimit -m` or `sysctl kern.memorystatus_level` if running into unexpected OOM on high-RAM machines.
- Disk pressure is rarely an issue — 128 GB machines typically have 1+ TB SSDs. Smart clean is mostly useful for hygiene rather than survival.
- All model sizes (up to 70B Q4) are feasible on these machines.

### CLI/MCP Parity Notes

- **Disk tools** (`get_disk_usage`, `scan_caches`, `clear_cache`, `smart_clean`, `status`): Full parity across all three modes (socket, app, standalone).
- **Memory tools** (`get_memory_stats`, `get_process_memory`, `get_compressor_health`): Available in all modes; app mode delegates to `--cli memory-stats`, standalone reads sysctl directly.
- **`memory_intervention`**: Uses `intervention_name="purge"` + `confirm=true/false`. In standalone, calls `/usr/sbin/purge` directly; in app mode, delegates to `Cacheout --cli purge`.
- **MCP-only tools** (no CLI equivalent): `check_alerts` (reads sentinel file), `configure_autopilot` (writes config file), `system_health` (computed from memory + disk + alerts).
- **Socket-enhanced tools**: `get_recommendations` in socket mode includes trend-based types (`compressor_degrading`, `exhaustion_imminent`). In standalone/app, only snapshot types are available (`compressor_low_ratio`, `swap_pressure`). `system_health` in socket mode fetches directly from daemon (<1ms); in other modes, computes locally.

## Important Notes

- All caches regenerate automatically when their tools are next used
- The `dry_run` flag is your friend — always preview destructive operations first time
- Docker disk cleanup is the nuclear option: it removes ALL images, containers, and volumes
- Cleanup is logged to `~/.cacheout/cleanup.log` (shared with the Cacheout GUI app)
- The server auto-detects whether Cacheout.app is installed and delegates accordingly
