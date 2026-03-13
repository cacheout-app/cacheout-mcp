#!/usr/bin/env python3
"""
Cacheout MCP Server — macOS disk cache management for AI agents.

Two execution modes (auto-detected):
  • STANDALONE — cleans caches directly via Python (no Cacheout.app needed)
  • APP — delegates to Cacheout CLI binary if installed

Environment variables:
  CACHEOUT_MODE=standalone|app   Force a specific mode
  CACHEOUT_BIN=/path/to/binary   Override binary location
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import stat
import tempfile
from pathlib import Path
import sys
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .categories import ALL_CATEGORIES, CATEGORY_MAP, RiskLevel
from .engine import (
    AppEngine,
    CleanResult,
    DiskInfo,
    ScanResult,
    clean_category,
    detect_mode,
    get_disk_info,
    scan_all,
    scan_category,
    smart_clean,
    socket_recommendations,
    _find_cacheout_binary,
    _get_state_dir,
    _get_socket_path,
    _human_bytes,
    _socket_command,
    _socket_connectable,
)
from .memory_models import (
    GetCompressorHealthInput,
    GetProcessMemoryInput,
    MemoryInterventionInput,
)
from .memory_tools import (
    _async_run,
    _async_sysctl_int,
    _async_parse_swap_total,
    _async_parse_swap_used,
    _STANDALONE_PURGE_TIMEOUT,
    _APP_PURGE_TIMEOUT,
    get_app_compressor_health,
    get_app_process_memory,
    get_standalone_compressor_health,
    get_standalone_memory_stats,
    get_standalone_process_memory,
    parse_sysctl_pressure_level,
    parse_sysctl_compressor_ratio,
    run_app_intervention,
    run_standalone_intervention,
)

# ── Server Init ──────────────────────────────────────────────────────

mcp = FastMCP("cacheout_mcp")

# Detect mode once at startup (async, but run synchronously at import time)
_MODE = asyncio.run(detect_mode())
_APP_ENGINE: Optional[AppEngine] = None

if _MODE == "app":
    _bin = _find_cacheout_binary()
    if _bin:
        _APP_ENGINE = AppEngine(_bin)
    else:
        _MODE = "standalone"  # fallback

print(f"[cacheout-mcp] Mode: {_MODE}", file=sys.stderr)


# ── Input Models ─────────────────────────────────────────────────────

class GetDiskUsageInput(BaseModel):
    """Input for disk usage query. No parameters required."""
    model_config = ConfigDict(extra="forbid")


class ScanCachesInput(BaseModel):
    """Input for scanning cache categories."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    categories: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of category slugs to scan. If omitted, scans ALL categories. "
            "Available slugs: xcode_derived_data, homebrew_cache, npm_cache, "
            "yarn_cache, pnpm_store, playwright_browsers, cocoapods_cache, "
            "swift_pm_cache, gradle_cache, pip_cache, browser_caches, "
            "vscode_cache, electron_cache, xcode_device_support, docker_disk"
        ),
    )
    min_size_mb: Optional[float] = Field(
        default=None,
        description="Only return categories larger than this (in MB). Useful for filtering noise.",
        ge=0,
    )


class ClearCacheInput(BaseModel):
    """Input for clearing specific cache categories."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    categories: List[str] = Field(
        ...,
        description=(
            "List of category slugs to clear. REQUIRED. "
            "Use cacheout_scan_caches first to see available categories and sizes."
        ),
        min_length=1,
    )
    dry_run: bool = Field(
        default=False,
        description="If true, report what WOULD be cleaned without actually deleting anything.",
    )

    @field_validator("categories")
    @classmethod
    def validate_slugs(cls, v: list[str]) -> list[str]:
        invalid = [s for s in v if s not in CATEGORY_MAP]
        if invalid:
            valid = ", ".join(CATEGORY_MAP.keys())
            raise ValueError(
                f"Unknown category slug(s): {', '.join(invalid)}. "
                f"Valid slugs: {valid}"
            )
        return v


class SmartCleanInput(BaseModel):
    """Input for intelligent space-freeing."""
    model_config = ConfigDict(extra="forbid")

    target_gb: float = Field(
        ...,
        description=(
            "How many GB of space to free. The server clears caches in "
            "priority order (safest first) until this target is met. "
            "Example: 5.0 to free 5 GB."
        ),
        gt=0,
        le=500,
    )
    dry_run: bool = Field(
        default=False,
        description="If true, shows what WOULD be cleaned without deleting.",
    )
    include_caution: bool = Field(
        default=False,
        description=(
            "If true, includes caution-level categories (like Docker) "
            "if needed to meet the target. Default: false (only safe/review)."
        ),
    )
    free_memory: bool = Field(
        default=False,
        description=(
            "If true, also runs a memory purge after disk cleanup. "
            "In dry-run mode, reports what would happen without executing."
        ),
    )


class ServerStatusInput(BaseModel):
    """Input for server status query."""
    model_config = ConfigDict(extra="forbid")


class GetMemoryStatsInput(BaseModel):
    """Input for memory statistics query. No parameters required."""
    model_config = ConfigDict(extra="forbid")


class SystemHealthInput(BaseModel):
    """Input for system health query. No parameters required."""
    model_config = ConfigDict(extra="forbid")


class ConfigureAutopilotInput(BaseModel):
    """Input for configuring the autopilot policy."""
    model_config = ConfigDict(extra="forbid")

    config: Dict[str, Any] = Field(
        ...,
        description=(
            "Autopilot policy configuration object. Must include "
            "'version' (1), 'enabled' (bool). Optionally includes "
            "'rules' (array), 'webhook' (object), 'telegram' (object)."
        ),
    )


# ── Tools ────────────────────────────────────────────────────────────

@mcp.tool(
    name="cacheout_get_disk_usage",
    annotations={
        "title": "Get Disk Usage",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cacheout_get_disk_usage(params: GetDiskUsageInput) -> str:
    """Get current disk space on the boot volume.

    Returns total, used, and free disk space with human-readable sizes.
    Use this to check if disk pressure exists before deciding to clean.

    Returns:
        str: JSON with total, free, used space and percentages.
            {
                "total": "500.1 GB",
                "free": "23.4 GB",
                "used": "476.7 GB",
                "free_gb": 23.4,
                "used_percent": 95.3
            }
    """
    if _APP_ENGINE:
        data = await _APP_ENGINE.disk_info()
        return json.dumps(data, indent=2)

    disk = get_disk_info()
    return json.dumps(disk.to_dict(), indent=2)


@mcp.tool(
    name="cacheout_scan_caches",
    annotations={
        "title": "Scan Cache Categories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cacheout_scan_caches(params: ScanCachesInput) -> str:
    """Scan macOS cache directories and report their sizes.

    Scans developer tool caches (Xcode, Homebrew, npm, pip, Docker, etc.)
    and reports the size of each. Results are sorted by size (largest first).

    Use this to understand what's consuming disk space before cleaning.

    Args:
        params: Optional filters — specific category slugs or minimum size.

    Returns:
        str: JSON array of cache categories with sizes, sorted largest first.
            [
                {
                    "slug": "xcode_derived_data",
                    "name": "Xcode DerivedData",
                    "size_bytes": 15032000000,
                    "size_human": "15.0 GB",
                    "item_count": 4230,
                    "risk_level": "safe",
                    "description": "Build artifacts...",
                    "rebuild_note": "Xcode rebuilds on next build"
                }
            ]
    """
    if _APP_ENGINE and not params.categories:
        data = await _APP_ENGINE.scan_all()
        return json.dumps(data, indent=2)

    # Determine which categories to scan
    if params.categories:
        cats = [CATEGORY_MAP[s] for s in params.categories if s in CATEGORY_MAP]
    else:
        cats = ALL_CATEGORIES

    results = [scan_category(c) for c in cats]
    results.sort(key=lambda r: r.size_bytes, reverse=True)

    # Filter by minimum size
    if params.min_size_mb is not None:
        min_bytes = int(params.min_size_mb * 1024 * 1024)
        results = [r for r in results if r.size_bytes >= min_bytes]

    # Format output
    output = []
    total_bytes = 0
    for r in results:
        if not r.exists:
            continue
        total_bytes += r.size_bytes
        output.append({
            "slug": r.slug,
            "name": r.name,
            "size_bytes": r.size_bytes,
            "size_human": r.size_human,
            "item_count": r.item_count,
            "risk_level": r.risk_level,
            "description": r.description,
            "rebuild_note": r.rebuild_note,
        })

    return json.dumps({
        "total_cleanable": _human_bytes(total_bytes),
        "total_cleanable_bytes": total_bytes,
        "category_count": len(output),
        "categories": output,
    }, indent=2)


@mcp.tool(
    name="cacheout_clear_cache",
    annotations={
        "title": "Clear Cache Categories",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cacheout_clear_cache(params: ClearCacheInput) -> str:
    """Clear specific cache categories to free disk space.

    Removes the contents of the specified cache directories. The directories
    themselves are preserved — only their contents are deleted. All cleared
    caches will regenerate automatically when their respective tools are used.

    IMPORTANT: Use cacheout_scan_caches first to see sizes, then pass the
    slugs of categories you want to clear. Use dry_run=true to preview.

    Args:
        params: Categories to clear and whether to dry-run.

    Returns:
        str: JSON report of what was cleaned and total space freed.
            {
                "total_freed": "12.3 GB",
                "total_freed_bytes": 13204889600,
                "dry_run": false,
                "results": [
                    {
                        "slug": "xcode_derived_data",
                        "name": "Xcode DerivedData",
                        "bytes_freed": 8500000000,
                        "freed_human": "8.5 GB",
                        "success": true,
                        "error": null
                    }
                ]
            }
    """
    if _APP_ENGINE:
        data = await _APP_ENGINE.clean(params.categories, dry_run=params.dry_run)
        return json.dumps(data, indent=2)

    results = []
    total = 0

    for slug in params.categories:
        cat = CATEGORY_MAP[slug]
        cr = await clean_category(cat, dry_run=params.dry_run)
        total += cr.bytes_freed
        results.append({
            "slug": cr.slug,
            "name": cr.category,
            "bytes_freed": cr.bytes_freed,
            "freed_human": _human_bytes(cr.bytes_freed),
            "success": cr.success,
            "error": cr.error,
        })

    return json.dumps({
        "total_freed": _human_bytes(total),
        "total_freed_bytes": total,
        "dry_run": params.dry_run,
        "results": results,
    }, indent=2)


@mcp.tool(
    name="cacheout_smart_clean",
    annotations={
        "title": "Smart Clean — Free Target GB",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def cacheout_smart_clean(params: SmartCleanInput) -> str:
    """Intelligently free disk space by clearing caches in priority order.

    This is the PRIMARY tool for agents managing disk pressure. Specify how
    many GB you need freed, and the server clears the safest caches first:
      1. Build artifacts (Xcode DerivedData) — always regenerates
      2. Package manager caches (Homebrew, npm, pip) — re-downloads as needed
      3. Browser caches — rebuilds on browsing
      4. Docker (only if include_caution=true) — destructive, last resort

    The server stops as soon as the target is met. Use dry_run=true to preview
    which categories would be cleaned and how much space would be freed.

    Typical use: An agent detects low disk space (or needs room for swap/builds)
    and calls smart_clean(target_gb=10.0) to free 10 GB immediately.

    Args:
        params: Target GB to free, dry_run flag, and caution inclusion.

    Returns:
        str: JSON report with before/after disk state and what was cleaned.
            {
                "target_gb": 10.0,
                "target_met": true,
                "total_freed_human": "12.3 GB",
                "dry_run": false,
                "disk_before": {"free_gb": 5.2, ...},
                "disk_after": {"free_gb": 17.5, ...},
                "cleaned": [...],
                "skipped": [...]
            }
    """
    if _APP_ENGINE:
        data = await _APP_ENGINE.smart_clean(
            params.target_gb, dry_run=params.dry_run, include_caution=params.include_caution,
        )
    else:
        data = await smart_clean(
            params.target_gb, dry_run=params.dry_run, include_caution=params.include_caution,
        )

    # Always include _meta for enhanced tools (additive, backward-compatible)
    meta = {"mode": _MODE, "partial": False}

    # Memory purge augmentation (additive — never changes existing fields)
    if params.free_memory:
        if params.dry_run:
            data["memory_freed"] = False
            data["purge_result"] = {
                "dry_run": True,
                "description": "Would run purge after disk cleanup",
            }
        else:
            try:
                if _APP_ENGINE:
                    stdout, err = await _async_run(
                        [_APP_ENGINE.binary, "--cli", "purge"],
                        timeout=_APP_PURGE_TIMEOUT,
                    )
                else:
                    stdout, err = await _async_run(
                        ["/usr/sbin/purge"],
                        timeout=_STANDALONE_PURGE_TIMEOUT,
                    )

                if err is not None:
                    data["memory_freed"] = False
                    data["purge_result"] = {
                        "error": err,
                        "note": "Purge failed but disk cleanup completed successfully",
                    }
                    meta["partial"] = True
                else:
                    data["memory_freed"] = True
                    data["purge_result"] = {"success": True}
            except Exception as e:
                data["memory_freed"] = False
                data["purge_result"] = {
                    "error": str(e),
                    "note": "Purge failed but disk cleanup completed successfully",
                }
                meta["partial"] = True

    data["_meta"] = meta
    return json.dumps(data, indent=2)


@mcp.tool(
    name="cacheout_status",
    annotations={
        "title": "Server Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cacheout_status(params: ServerStatusInput) -> str:
    """Get cacheout-mcp server status, mode, and available categories.

    Returns the execution mode (standalone or app), the Cacheout binary
    path if in app mode, and a list of all available cache category slugs.

    Use this to verify the server is running and understand its capabilities.

    Returns:
        str: JSON with mode, binary path, and category list.
    """
    categories = [
        {"slug": c.slug, "name": c.name, "risk_level": c.risk_level.value}
        for c in ALL_CATEGORIES
    ]

    return json.dumps({
        "mode": _MODE,
        "binary": _find_cacheout_binary() if _MODE == "app" else None,
        "version": "0.1.0",
        "categories": categories,
    }, indent=2)


@mcp.tool(
    name="cacheout_get_memory_stats",
    annotations={
        "title": "Get System Memory Stats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cacheout_get_memory_stats(params: GetMemoryStatsInput) -> str:
    """Get current system memory statistics on macOS.

    Returns physical RAM breakdown (free, active, inactive, wired, compressed),
    swap usage, compressor ratio, memory pressure level, and an actionable
    memory tier classification.

    Use this to check memory health before builds, heavy tasks, or when
    investigating performance issues. The memory_tier field provides a
    quick assessment: abundant > comfortable > moderate > constrained > critical.

    In app mode, delegates to CacheOut CLI. In standalone mode, reads sysctl
    values directly (no CacheOut.app needed).

    Returns:
        str: JSON with memory statistics.
            {
                "total_physical_mb": 8192.0,
                "free_mb": 512.3,
                "active_mb": 3200.1,
                "inactive_mb": 1024.5,
                "wired_mb": 2048.7,
                "compressed_mb": 800.2,
                "compressor_ratio": 2.5,
                "swap_used_mb": 256.0,
                "pressure_level": 1,
                "memory_tier": "moderate",
                "estimated_available_mb": 1536.8,
                "mode": "standalone"
            }
    """
    if _APP_ENGINE:
        try:
            data = await _APP_ENGINE.memory_stats()
            data["mode"] = "app"
            return json.dumps(data, indent=2)
        except Exception as e:
            # Fall back to standalone if app-mode fails
            print(
                f"[cacheout-mcp] App-mode memory_stats failed, "
                f"falling back to standalone: {e}",
                file=sys.stderr,
            )

    stats = await get_standalone_memory_stats()
    if "error" in stats:
        return json.dumps({"error": stats["error"], "mode": "standalone"}, indent=2)

    return json.dumps(stats, indent=2)


@mcp.tool(
    name="cacheout_get_process_memory",
    annotations={
        "title": "Get Process Memory Usage",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cacheout_get_process_memory(params: GetProcessMemoryInput) -> str:
    """Get top processes by memory usage on macOS.

    Returns a ranked list of the top N processes sorted by memory consumption.
    In standalone mode, uses RSS from ``ps`` (labeled as an estimate).
    In app mode, uses physical footprint from the Cacheout CLI.

    Sort keys are mode-dependent:
      - standalone: 'rss' (default)
      - app: 'phys_footprint' (default)

    The ``capabilities`` map in the response shows which sort keys are available.
    ``sort_by_pageins`` is gated to false in all modes this phase.

    Args:
        params: top_n (default 10) and optional sort_by key.

    Returns:
        str: JSON envelope with mode, capabilities, data (processes + sort info), partial.
    """
    if _APP_ENGINE:
        try:
            result = await get_app_process_memory(_APP_ENGINE, params.top_n, params.sort_by)
            return json.dumps(result, indent=2)
        except Exception as e:
            print(
                f"[cacheout-mcp] App-mode get_process_memory failed, "
                f"falling back to standalone: {e}",
                file=sys.stderr,
            )

    result = await get_standalone_process_memory(params.top_n, params.sort_by)
    return json.dumps(result, indent=2)


@mcp.tool(
    name="cacheout_get_compressor_health",
    annotations={
        "title": "Get Compressor Health",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cacheout_get_compressor_health(params: GetCompressorHealthInput) -> str:
    """Get macOS memory compressor health metrics.

    Returns compressor ratio, compression/decompression rates (via dual-sample),
    thrashing detection, pressure level, and trend information.

    Takes two samples ~1 second apart to compute instantaneous rates.
    Thrashing is flagged when decompression_rate > 100/sec AND > 2x compression_rate
    (aligned with CompressorTracker.swift thresholds).

    Trend requires multiple invocations over time — a single call returns
    "unknown" with partial=true.

    In standalone mode, reads vm_stat and sysctl directly.
    In app mode, delegates to ``--cli memory-stats``.

    Returns:
        str: JSON envelope with mode, capabilities, data (ratio, rates, thrashing), partial.
    """
    if _APP_ENGINE:
        try:
            result = await get_app_compressor_health(_APP_ENGINE)
            return json.dumps(result, indent=2)
        except Exception as e:
            print(
                f"[cacheout-mcp] App-mode get_compressor_health failed, "
                f"falling back to standalone: {e}",
                file=sys.stderr,
            )

    result = await get_standalone_compressor_health()
    return json.dumps(result, indent=2)


@mcp.tool(
    name="cacheout_memory_intervention",
    annotations={
        "title": "Memory Intervention (purge/reclaim)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def cacheout_memory_intervention(params: MemoryInterventionInput) -> str:
    """Execute a memory reclamation intervention on macOS.

    IMPORTANT: Always call with confirm=false first to preview what will happen,
    then call again with confirm=true to execute.

    Available interventions:
      - purge: Flush the Unified Buffer Cache (UBC) to reclaim purgeable memory.
               Works in both standalone and app modes.
      - trigger_pressure_warn: Manual pressure event (app only, not yet available)
      - reduce_transparency: Toggle transparency setting (app only, not yet available)
      - delete_sleepimage: Remove sleepimage file (app only, not yet available)
      - cleanup_snapshots: Clean orphaned APFS snapshots (app only, not yet available)
      - flush_compositor: Display mode toggle (app only, not yet available)

    In standalone mode, only 'purge' is supported. In app mode, only 'purge'
    is currently wired; other interventions will unlock as the CLI surface grows.

    The response always includes a ``capabilities`` map showing which
    interventions are available in the current mode.

    Args:
        params: intervention_name, confirm flag, and optional target_pid.

    Returns:
        str: JSON envelope with mode, capabilities, data, partial.
    """
    if _APP_ENGINE:
        try:
            result = await run_app_intervention(
                _APP_ENGINE, params.intervention_name, params.confirm
            )
            return json.dumps(result, indent=2)
        except Exception as e:
            print(
                f"[cacheout-mcp] App-mode intervention failed, "
                f"falling back to standalone: {e}",
                file=sys.stderr,
            )

    result = await run_standalone_intervention(
        params.intervention_name, params.confirm
    )
    return json.dumps(result, indent=2)


# ── Autopilot Local Validator ────────────────────────────────────────

# Valid autopilot action names (must match daemon InterventionRegistry.autopilotActions)
_AUTOPILOT_ACTIONS = {"pressure-trigger", "reduce-transparency"}

# Valid pressure tier values for config conditions
# Must match Swift PressureTier.validConfigValues: all enum cases + "warn" alias
_VALID_PRESSURE_TIERS = {"normal", "elevated", "warn", "warning", "critical"}


def _validate_autopilot_config(config: dict) -> list[str]:
    """Validate an autopilot config dict against the v1 schema.

    This mirrors the daemon's AutopilotConfigValidator exactly:
    - version must be 1
    - enabled must be boolean
    - Rules: action names must be in _AUTOPILOT_ACTIONS
    - Webhook (if present): url required, format = "generic", timeout_s 1-60
    - Telegram (if present): bot_token + chat_id required, timeout_s 1-60
    """
    errors: list[str] = []

    # version (bool is subclass of int in Python, so exclude it explicitly)
    version = config.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        errors.append("Missing or non-integer 'version' field")
        return errors
    if version != 1:
        errors.append(f"Unsupported version: {version} (expected 1)")
        return errors

    # enabled
    if not isinstance(config.get("enabled"), bool):
        errors.append("Missing or non-boolean 'enabled' field")
        return errors

    # rules
    rules = config.get("rules")
    if rules is not None:
        if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
            errors.append("'rules' must be an array of objects")
        else:
            for i, rule in enumerate(rules):

                action = rule.get("action")
                if isinstance(action, str):
                    if action not in _AUTOPILOT_ACTIONS:
                        allowed = ", ".join(sorted(_AUTOPILOT_ACTIONS))
                        errors.append(
                            f"Rule[{i}]: unsupported action '{action}' "
                            f"(allowed: {allowed})"
                        )
                else:
                    errors.append(f"Rule[{i}]: missing or non-string 'action' field")

                condition = rule.get("condition")
                if isinstance(condition, dict):
                    tier = condition.get("pressure_tier")
                    if isinstance(tier, str):
                        if tier not in _VALID_PRESSURE_TIERS:
                            allowed = ", ".join(sorted(_VALID_PRESSURE_TIERS))
                            errors.append(
                                f"Rule[{i}]: invalid pressure_tier '{tier}' "
                                f"(allowed: {allowed})"
                            )
                    elif tier is not None:
                        errors.append(f"Rule[{i}]: pressure_tier must be a string")
                    else:
                        errors.append(f"Rule[{i}]: condition missing 'pressure_tier'")

                    consecutive = condition.get("consecutive_samples")
                    if isinstance(consecutive, int) and not isinstance(consecutive, bool):
                        if consecutive < 1:
                            errors.append(
                                f"Rule[{i}]: consecutive_samples must be >= 1, "
                                f"got {consecutive}"
                            )
                    elif consecutive is not None:
                        errors.append(f"Rule[{i}]: consecutive_samples must be an integer")

                    ratio_window = condition.get("compression_ratio_window")
                    if isinstance(ratio_window, int) and not isinstance(ratio_window, bool):
                        if ratio_window < 1:
                            errors.append(
                                f"Rule[{i}]: compression_ratio_window must be >= 1, "
                                f"got {ratio_window}"
                            )
                    elif ratio_window is not None:
                        errors.append(
                            f"Rule[{i}]: compression_ratio_window must be an integer"
                        )

                    ratio_below = condition.get("compression_ratio_below")
                    if isinstance(ratio_below, (int, float)) and not isinstance(ratio_below, bool):
                        if ratio_below <= 0:
                            errors.append(
                                f"Rule[{i}]: compression_ratio_below must be > 0, "
                                f"got {ratio_below}"
                            )
                    elif ratio_below is not None:
                        errors.append(
                            f"Rule[{i}]: compression_ratio_below must be a number"
                        )
                elif condition is not None:
                    errors.append(f"Rule[{i}]: 'condition' must be an object")
                else:
                    errors.append(f"Rule[{i}]: missing 'condition' field")

    # webhook
    webhook = config.get("webhook")
    if webhook is not None:
        if not isinstance(webhook, dict):
            errors.append("'webhook' must be an object")
        else:
            url = webhook.get("url")
            if isinstance(url, str):
                # URL validation matching Swift: parseable, http(s) scheme, has host.
                # urlparse().hostname can raise ValueError on malformed bracketed
                # IPv6 hosts (e.g. "https://[::1"), so wrap in try/except.
                try:
                    parsed = urlparse(url)
                    scheme = (parsed.scheme or "").lower()
                    if scheme not in ("http", "https"):
                        errors.append(
                            f"webhook: url must use http or https scheme, got '{scheme}'"
                        )
                    # Independent check (not elif) to match Swift which emits
                    # scheme and host errors independently.
                    if not parsed.hostname:
                        errors.append(
                            "webhook: url must be an absolute URL with a host"
                        )
                except ValueError:
                    errors.append("webhook: url is not a valid URL")
            else:
                errors.append("webhook: missing or non-string 'url'")

            fmt = webhook.get("format")
            if isinstance(fmt, str):
                if fmt != "generic":
                    errors.append(
                        f"webhook: unsupported format '{fmt}' (must be 'generic')"
                    )
            else:
                errors.append(
                    "webhook: missing or non-string 'format' (must be 'generic')"
                )

            timeout_s = webhook.get("timeout_s")
            if isinstance(timeout_s, int) and not isinstance(timeout_s, bool):
                if timeout_s < 1 or timeout_s > 60:
                    errors.append(
                        f"webhook: timeout_s must be 1-60, got {timeout_s}"
                    )
            else:
                errors.append(
                    "webhook: missing or non-integer 'timeout_s' (must be 1-60)"
                )

    # telegram
    telegram = config.get("telegram")
    if telegram is not None:
        if not isinstance(telegram, dict):
            errors.append("'telegram' must be an object")
        else:
            if not isinstance(telegram.get("bot_token"), str):
                errors.append("telegram: missing or non-string 'bot_token'")
            if not isinstance(telegram.get("chat_id"), str):
                errors.append("telegram: missing or non-string 'chat_id'")

            timeout_s = telegram.get("timeout_s")
            if isinstance(timeout_s, int) and not isinstance(timeout_s, bool):
                if timeout_s < 1 or timeout_s > 60:
                    errors.append(
                        f"telegram: timeout_s must be 1-60, got {timeout_s}"
                    )
            else:
                errors.append(
                    "telegram: missing or non-integer 'timeout_s' (must be 1-60)"
                )

    return errors


# ── PID Identity Validation ────────────────────────────────────────

async def _validate_daemon_pid(pid: int, state_dir: str) -> bool:
    """Validate that a PID belongs to our Cacheout daemon.

    Checks the process command line for exact token matches of the Cacheout
    binary path, --daemon flag, and --state-dir value. Prevents signaling
    an unrelated process if PIDs are reused after daemon exit.

    Fails closed: returns False if the binary cannot be located, if the
    process argv cannot be read, or if any token does not match.
    """
    # Resolve the expected binary path
    expected_bin = _find_cacheout_binary()
    if not expected_bin:
        return False  # Cannot validate without knowing the binary

    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-p", str(pid), "-o", "args=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
        if proc.returncode != 0 or not stdout_bytes:
            return False

        full_cmd = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not full_cmd:
            return False

        tokens = full_cmd.split()

        # Require the exact binary path as a token (argv[0])
        if expected_bin not in tokens:
            return False

        # Require --daemon as a standalone token
        if "--daemon" not in tokens:
            return False

        # Require --state-dir followed by the expected directory,
        # or --state-dir=<state_dir> as a single token.
        for i, token in enumerate(tokens):
            if token == "--state-dir" and i + 1 < len(tokens):
                if tokens[i + 1] == state_dir:
                    return True
            elif token == f"--state-dir={state_dir}":
                return True

        return False
    except (asyncio.TimeoutError, OSError):
        return False


# ── Pressure Tier (matches Swift PressureTier.from exactly) ──────────

def _pressure_tier_from(pressure_level: int, available_mb: float) -> str:
    """Classify pressure state from raw kernel level and available memory.

    Mirrors Swift PressureTier.from(pressureLevel:availableMB:) exactly.
    """
    if pressure_level >= 4 or available_mb < 512:
        return "critical"
    if pressure_level >= 2 or available_mb < 1500:
        return "warning"
    if pressure_level >= 1 or available_mb < 4000:
        return "elevated"
    return "normal"


# ── Health Score (Python, matches Swift HealthScore.compute exactly) ──

def _health_score(pressure_tier: str, swap_used_percent: float,
                  compression_ratio: float) -> int:
    """Canonical health score formula. Identical to Swift HealthScore.compute.

    Returns int in [0, 100]. -1 sentinel is handled by callers.
    """
    base = 100
    if pressure_tier == "critical":
        base -= 50
    elif pressure_tier in ("warn", "warning"):
        base -= 25

    swap_penalty = min(50, int(swap_used_percent / 2))
    compressor_penalty = min(30, max(0, int((3.0 - compression_ratio) * 10)))
    return max(0, base - swap_penalty - compressor_penalty)


# ── System Health Tool ──────────────────────────────────────────────

@mcp.tool(
    name="cacheout_system_health",
    annotations={
        "title": "Get System Health Score",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cacheout_system_health(params: SystemHealthInput) -> str:
    """Get overall system health score with alerts.

    Returns a health score (0-100, -1 if no data), the data source,
    and any active alerts from the daemon.

    In socket mode (daemon running), fetches health data directly from the
    daemon's Unix socket for <1ms latency. In CLI/standalone mode, computes
    the health score locally using the canonical formula.

    The health score formula:
      base = 100
      critical pressure: -50, warn pressure: -25
      swap penalty: min(50, swap_used_percent / 2)
      compressor penalty: min(30, max(0, (3.0 - ratio) * 10))
      score = max(0, base - penalties)

    Returns:
        str: JSON with score (Int, -1 if no data), source, and alerts array.
    """
    # Try socket mode first (daemon running)
    if _MODE == "socket":
        health_data = await _socket_command("health")
        if health_data is not None:
            return json.dumps({
                "score": health_data.get("health_score", -1),
                "source": "daemon",
                "alerts": health_data.get("alerts", []),
                "helper_available": health_data.get("helper_available"),
                "_meta": {"mode": "socket", "partial": False},
            }, indent=2)

    # Fall back to local computation
    if _APP_ENGINE:
        try:
            stats = await _APP_ENGINE.memory_stats()
            pressure_raw = stats.get("pressureLevel", 0)
            # Use PressureTier.from logic: need available MB
            page_size = stats.get("pageSize", 16384)
            free_pages = stats.get("freePages", 0)
            inactive_pages = stats.get("inactivePages", 0)
            available_mb = (free_pages + inactive_pages) * page_size / 1048576.0
            tier = _pressure_tier_from(pressure_raw, available_mb)
            swap_total = stats.get("swapTotalBytes", 0)
            swap_used = stats.get("swapUsedBytes", 0)
            swap_pct = (swap_used / swap_total * 100.0) if swap_total > 0 else 0.0
            ratio = stats.get("compressionRatio", 0.0)
            score = _health_score(tier, swap_pct, ratio)
            return json.dumps({
                "score": score,
                "source": "app",
                "alerts": [],
                "_meta": {"mode": "app", "partial": False},
            }, indent=2)
        except Exception as e:
            print(
                f"[cacheout-mcp] App-mode health failed, falling back: {e}",
                file=sys.stderr,
            )

    # Standalone: compute from sysctl using same PressureTier.from logic
    try:
        mem_stats = await get_standalone_memory_stats()
        if "error" in mem_stats:
            return json.dumps({
                "score": -1,
                "source": "standalone",
                "alerts": [],
                "_meta": {"mode": "standalone", "partial": True},
            }, indent=2)

        pressure_raw = mem_stats.get("pressure_level", 0)
        available_mb = mem_stats.get("estimated_available_mb", 0.0)
        tier = _pressure_tier_from(pressure_raw, available_mb)

        # Get real swap total from sysctl (not a heuristic).
        # If swap data is unavailable, return score=-1 with partial=true
        # to match Swift daemon behavior (no data → no canonical score).
        swap_used_bytes = await _async_parse_swap_used()
        swap_total_bytes = await _async_parse_swap_total()
        swap_available = (
            swap_used_bytes is not None
            and swap_total_bytes is not None
            and swap_total_bytes > 0
        )

        if not swap_available:
            return json.dumps({
                "score": -1,
                "source": "standalone",
                "alerts": [],
                "_meta": {"mode": "standalone", "partial": True},
                "note": "swap data unavailable; cannot compute canonical score",
            }, indent=2)

        swap_pct = swap_used_bytes / swap_total_bytes * 100.0
        ratio = mem_stats.get("compressor_ratio", 0.0)
        score = _health_score(tier, swap_pct, ratio)

        return json.dumps({
            "score": score,
            "source": "standalone",
            "alerts": [],
            "_meta": {"mode": "standalone", "partial": False},
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "score": -1,
            "source": "standalone",
            "alerts": [],
            "_meta": {"mode": "standalone", "partial": True},
            "error": str(e),
        }, indent=2)


# ── Configure Autopilot Tool ────────────────────────────────────────

# In-process async lock serializes concurrent configure calls within the
# same MCP server process. Paired with fcntl.flock for cross-process safety.
_configure_lock = asyncio.Lock()

@mcp.tool(
    name="cacheout_configure_autopilot",
    annotations={
        "title": "Configure Autopilot Policy",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cacheout_configure_autopilot(params: ConfigureAutopilotInput) -> str:
    """Configure the autopilot policy for the headless daemon.

    Uses a validate-then-write flow:
    1. Writes candidate config to autopilot.candidate.json (0600)
    2. If daemon running: validates via socket; else validates locally
    3. Invalid: deletes candidate, returns errors
    4. Valid: atomically renames candidate to autopilot.json
    5. If daemon: sends SIGHUP and polls for config generation increment

    The local validator mirrors the daemon's shared validator exactly:
    - version must be 1
    - enabled must be boolean
    - Rules: actions must be 'pressure-trigger' or 'reduce-transparency'
    - Webhook (if present): url required, format = 'generic', timeout_s 1-60
    - Telegram (if present): bot_token + chat_id required, timeout_s 1-60

    Args:
        params: Config object to validate and apply.

    Returns:
        str: JSON with success status, validation errors, and any warnings.
    """
    # In-process async lock prevents event-loop stalls from concurrent calls.
    # File lock (LOCK_NB) prevents cross-process races.
    async with _configure_lock:
        state_dir = _get_state_dir()
        state_path = Path(state_dir)
        state_path.mkdir(parents=True, exist_ok=True)

        config_path = state_path / "autopilot.json"
        lock_path = state_path / "autopilot.lock"

        config = params.config
        config_json = json.dumps(config, indent=2)

        lock_fd = None
        try:
            lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            return await _configure_autopilot_locked(
                state_path, config_path, config, config_json
            )
        except BlockingIOError:
            return json.dumps({
                "success": False,
                "errors": ["Another configure operation is in progress"],
                "_meta": {"mode": _MODE, "partial": False},
            }, indent=2)
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)


async def _configure_autopilot_locked(
    state_path: Path,
    config_path: Path,
    config: dict,
    config_json: str,
) -> str:
    """Inner configure flow, called while holding autopilot.lock."""

    # Step 1: Write candidate to a unique temp file (0600)
    try:
        fd_tuple = tempfile.mkstemp(
            prefix="autopilot.candidate.",
            suffix=".json",
            dir=str(state_path),
        )
        candidate_path = Path(fd_tuple[1])
        os.fchmod(fd_tuple[0], 0o600)
        with os.fdopen(fd_tuple[0], "w") as f:
            f.write(config_json)
    except OSError as e:
        return json.dumps({
            "success": False,
            "errors": [f"Failed to write candidate file: {e}"],
            "_meta": {"mode": _MODE, "partial": False},
        }, indent=2)

    # Step 2: Validate
    daemon_available = _socket_connectable(_get_socket_path())
    validation_errors: list[str] = []

    if daemon_available:
        # Use daemon's validate_config socket command
        result = await _socket_command(
            "validate_config", {"path": str(candidate_path)}
        )
        if result is not None:
            if not result.get("valid", False):
                validation_errors = result.get("errors", ["Daemon validation failed"])
        else:
            # Socket command failed; fall back to local validation
            validation_errors = _validate_autopilot_config(config)
    else:
        # No daemon; validate locally
        validation_errors = _validate_autopilot_config(config)

    # Step 3: If invalid, delete candidate and return errors
    if validation_errors:
        try:
            candidate_path.unlink(missing_ok=True)
        except OSError:
            pass
        return json.dumps({
            "success": False,
            "errors": validation_errors,
            "_meta": {"mode": _MODE, "partial": False},
        }, indent=2)

    # Step 4: Valid — atomically rename candidate to autopilot.json
    try:
        os.rename(str(candidate_path), str(config_path))
    except OSError as e:
        try:
            candidate_path.unlink(missing_ok=True)
        except OSError:
            pass
        return json.dumps({
            "success": False,
            "errors": [f"Failed to activate config: {e}"],
            "_meta": {"mode": _MODE, "partial": False},
        }, indent=2)

    # Step 5: If daemon running, read generation, SIGHUP, poll for increment
    warnings: list[str] = []
    reload_result: Optional[str] = None
    saved = True  # File was written successfully at this point

    if daemon_available:
        # Read current generation before SIGHUP. A successful baseline is
        # required to reliably detect generation increments. Without it we
        # cannot distinguish "daemon already had generation N" from "daemon
        # just reloaded to generation N".
        config_status = await _socket_command("config_status")
        baseline_ok = config_status is not None
        old_generation = config_status.get("generation", 0) if baseline_ok else -1

        if not baseline_ok:
            warnings.append(
                "Could not read baseline config_status; "
                "cannot confirm activation"
            )

        # Send SIGHUP to daemon (with PID identity validation to prevent
        # signaling an unrelated process if the daemon exited and the PID
        # was reused between our socket checks and this signal).
        pid_file = state_path / "daemon.pid"
        sighup_sent = False
        if pid_file.exists():
            try:
                pid_str = pid_file.read_text().strip()
                daemon_pid = int(pid_str)
                # Validate PID is actually our daemon before signaling
                if await _validate_daemon_pid(daemon_pid, str(state_path)):
                    os.kill(daemon_pid, signal.SIGHUP)
                    sighup_sent = True
                else:
                    warnings.append(
                        f"PID {daemon_pid} from pidfile is not our daemon; "
                        "config saved but SIGHUP skipped"
                    )
            except (ValueError, OSError, ProcessLookupError) as e:
                warnings.append(f"Failed to send SIGHUP to daemon: {e}")

        # Poll for generation increment (up to 3s), then check reload status
        if sighup_sent and baseline_ok:
            new_status = None
            for _ in range(6):  # 6 * 0.5s = 3s
                await asyncio.sleep(0.5)
                new_status = await _socket_command("config_status")
                if new_status and new_status.get("generation", 0) > old_generation:
                    break
            else:
                warnings.append(
                    "Daemon did not increment config generation within 3s"
                )
                reload_result = "timeout"

            # Check whether the reload actually succeeded
            if reload_result != "timeout" and new_status:
                load_status = new_status.get("status", "")
                if load_status == "ok":
                    reload_result = "reloaded"
                elif load_status == "error":
                    reload_error = new_status.get("error", "Unknown error")
                    warnings.append(
                        f"Daemon reloaded config but reported error: {reload_error}"
                    )
                    reload_result = "reload_error"
                else:
                    reload_result = "reloaded"
        elif sighup_sent and not baseline_ok:
            # SIGHUP was sent but we cannot verify activation
            reload_result = "unverified"
            warnings.append(
                "SIGHUP sent but activation cannot be confirmed "
                "without baseline generation"
            )
        else:
            reload_result = "sighup_failed"
    else:
        warnings.append("Daemon not running; config saved but not active until daemon starts")

    # Determine overall success:
    # - "reloaded" = full success (saved + daemon confirmed)
    # - no daemon = saved only (success=true, active=false)
    # - timeout/sighup_failed/reload_error when daemon present = failure
    active = reload_result == "reloaded"
    if daemon_available:
        success = active
    else:
        success = saved  # No daemon: saving is sufficient

    result_data: dict[str, Any] = {
        "success": success,
        "saved": saved,
        "active": active,
        "config_path": str(config_path),
        "_meta": {"mode": _MODE, "partial": False},
    }
    if reload_result:
        result_data["reload"] = reload_result
    if warnings:
        result_data["warnings"] = warnings

    return json.dumps(result_data, indent=2)


# ── Tier 2: Watchdog Alert Check ─────────────────────────────────────
ALERT_FILE = Path.home() / ".cacheout" / "alert.json"
HISTORY_FILE = Path.home() / ".cacheout" / "watchdog-history.json"


class CheckAlertsInput(BaseModel):
    """Input for checking watchdog alerts."""
    acknowledge: bool = Field(
        default=False,
        description="Set true to acknowledge and clear the current alert after handling it"
    )


@mcp.tool(
    name="cacheout_check_alerts",
    description=(
        "Check if the Cacheout watchdog has raised any disk/swap/memory alerts. "
        "This is a near-zero-cost check (reads a small JSON file). Use this at the "
        "start of tasks, before builds, or after errors — NOT on a polling loop. "
        "Returns null if no alert is active. If an alert exists, review it and take "
        "action with smart_clean, then call again with acknowledge=true to clear it."
    )
)
async def cacheout_check_alerts(params: CheckAlertsInput) -> str:
    if params.acknowledge:
        ack_meta = {"mode": _MODE, "partial": False}
        if ALERT_FILE.exists():
            # Read before deleting so we can confirm what was cleared
            try:
                alert = json.loads(ALERT_FILE.read_text())
                ALERT_FILE.unlink()
                return json.dumps({
                    "acknowledged": True,
                    "cleared_alert": alert["level"],
                    "cleared_triggers": alert["triggers"],
                    "_meta": ack_meta,
                })
            except Exception:
                ALERT_FILE.unlink(missing_ok=True)
                return json.dumps({"acknowledged": True, "note": "alert file was corrupt, cleared", "_meta": ack_meta})
        return json.dumps({"acknowledged": False, "note": "no active alert to acknowledge", "_meta": ack_meta})

    # Read current alert
    alert = None
    if ALERT_FILE.exists():
        try:
            alert = json.loads(ALERT_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            alert = {"level": "unknown", "note": "alert file corrupt"}

    # Read latest watchdog sample for current stats
    current = None
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
            if history:
                latest = history[-1]
                current = {
                    "disk_free_gb": round(latest["disk_bytes"] / 1073741824, 2),
                    "swap_used_gb": round(latest["swap_bytes"] / 1073741824, 2),
                    "memory_pressure": latest.get("pressure", "unknown"),
                    "sampled_at": latest["ts"],
                }
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # Determine watchdog health
    watchdog_running = HISTORY_FILE.exists()
    if watchdog_running and current:
        import time
        age = time.time() - current["sampled_at"]
        watchdog_running = age < 120  # stale if no sample in 2 min

    # ── Memory augmentation fields (additive, never changes existing) ──
    pressure_level = None
    pressure_label = None
    pressure_note = None
    compressor_ratio = None
    compressor_ratio_note = None
    recommended_action = None
    meta_partial = False

    if _APP_ENGINE:
        # App mode: use --cli memory-stats
        try:
            stdout, err = await _async_run(
                [_APP_ENGINE.binary, "--cli", "memory-stats"],
                timeout=15.0,
            )
            if err is not None:
                pressure_note = f"CLI memory-stats failed: {err}"
                compressor_ratio_note = pressure_note
                meta_partial = True
            else:
                import json as _json
                try:
                    dto = _json.loads(stdout)
                    pressure_level = dto.get("pressureLevel")
                    if pressure_level is not None:
                        pressure_label = parse_sysctl_pressure_level(pressure_level)
                    compressor_ratio = dto.get("compressionRatio")
                except (ValueError, KeyError) as e:
                    pressure_note = f"Failed to parse CLI output: {e}"
                    compressor_ratio_note = pressure_note
                    meta_partial = True
        except Exception as e:
            pressure_note = f"CLI memory-stats exception: {e}"
            compressor_ratio_note = pressure_note
            meta_partial = True
    else:
        # Standalone: read sysctl directly
        try:
            raw_pressure = await _async_sysctl_int(
                "kern.memorystatus_vm_pressure_level", timeout=5.0
            )
            if raw_pressure is not None:
                pressure_level = raw_pressure
                pressure_label = parse_sysctl_pressure_level(raw_pressure)
            else:
                pressure_note = "sysctl read returned None"
                meta_partial = True
        except Exception as e:
            pressure_note = f"sysctl read timed out: {e}"
            meta_partial = True

        try:
            ratio = await parse_sysctl_compressor_ratio()
            compressor_ratio = ratio
            if ratio is None:
                compressor_ratio_note = "sysctl compressor bytes unavailable"
                meta_partial = True
        except Exception as e:
            compressor_ratio_note = f"sysctl compressor read failed: {e}"
            meta_partial = True

    # If any new memory field is None, mark partial.
    # Note: swap_velocity_gb_per_5m is always None (requires watchdog history),
    # so partial is always True until velocity sources become available.
    if pressure_level is None or compressor_ratio is None:
        meta_partial = True
    # swap_velocity is always unavailable → always partial
    meta_partial = True

    # Recommended action
    if pressure_level is not None and pressure_level >= 1:
        recommended_action = "consider purge" if pressure_level == 1 else "purge recommended"
        if pressure_level >= 4:
            recommended_action = "urgent: purge and investigate memory consumers"
    elif pressure_level is not None:
        recommended_action = "no action needed"

    return json.dumps({
        "alert": alert,
        "current": current,
        "watchdog_running": watchdog_running,
        "pressure_level": pressure_level,
        "pressure_label": pressure_label,
        "pressure_note": pressure_note,
        "compressor_ratio": compressor_ratio,
        "compressor_ratio_note": compressor_ratio_note,
        "compressor_trend": "unknown",
        "compressor_trend_note": "requires repeated sampling or watchdog history",
        "swap_velocity_gb_per_5m": None,
        "swap_velocity_note": "requires watchdog history or repeated sampling",
        "recommended_action": recommended_action,
        "_meta": {"mode": _MODE, "partial": meta_partial},
    }, indent=2)

# ── Recommendations Tool ──────────────────────────────────────────────

@mcp.tool(
    name="cacheout_get_recommendations",
    annotations={
        "title": "Get Predictive Recommendations",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def cacheout_get_recommendations() -> str:
    """Get predictive memory recommendations from the Cacheout engine.

    Returns advisory recommendations about memory health, including
    compressor degradation, swap pressure, high-growth processes,
    Rosetta-translated processes, and agent memory pressure.

    Mode-dependent behavior:
      - socket: Full recommendations from daemon (all 7 types when conditions apply)
      - app: Snapshot-only recommendations from CLI (no trend-based types)
      - standalone: Basic recommendations from sysctl (compressor_low_ratio, swap_pressure only)

    The ``partial`` flag in ``_meta`` indicates degraded results:
      - Always true in app/standalone modes (no trend data)
      - True in socket mode only when daemon's process scan was incomplete

    Returns:
        str: JSON with recommendations array and _meta.
            {
                "recommendations": [
                    {
                        "type": "compressor_low_ratio",
                        "message": "Compression ratio 1.5 is below 2.0",
                        "process": null,
                        "pid": null,
                        "impact_value": 1.5,
                        "impact_unit": "ratio",
                        "confidence": "low",
                        "source": "standalone"
                    }
                ],
                "_meta": {
                    "mode": "standalone",
                    "count": 1,
                    "partial": true,
                    "source": "standalone"
                }
            }
    """
    # Snapshot-only types allowed in app mode (no trend-based types)
    _APP_MODE_TYPES = {
        "compressor_low_ratio", "high_growth_process", "rosetta_detected",
        "agent_memory_pressure", "swap_pressure",
    }

    # Socket mode: get full recommendations from daemon
    # Only use socket when explicitly in socket mode — do not opportunistically
    # upgrade app/standalone modes, which would break their degraded contracts.
    if _MODE == "socket":
        data = await socket_recommendations()
        if data is not None and isinstance(data, dict):
            # Daemon returns: {"recommendations": [...], "_meta": {"count": N, "source": "daemon", "scan_partial": bool}}
            raw_recs = data.get("recommendations", [])
            meta = data.get("_meta", {})
            if not isinstance(meta, dict):
                meta = {}
            # Validate shape: must be a list of dicts
            if isinstance(raw_recs, list) and all(isinstance(r, dict) for r in raw_recs):
                scan_partial = meta.get("scan_partial", False)
                return json.dumps({
                    "recommendations": raw_recs,
                    "_meta": {
                        "mode": "socket",
                        "count": len(raw_recs),
                        "partial": scan_partial,
                        "source": "daemon",
                    },
                }, indent=2)
            # Malformed payload: fall through to app/standalone

    # App mode: get snapshot-only recommendations from CLI
    # Use _APP_ENGINE if available; lazily resolve binary only for socket-started
    # processes where _APP_ENGINE was never initialized at startup.
    app_engine = _APP_ENGINE
    if app_engine is None and _MODE == "socket":
        _bin = _find_cacheout_binary()
        if _bin:
            app_engine = AppEngine(_bin)

    if app_engine is not None:
        try:
            cli_recs = await app_engine.recommendations()
            # CLI returns a raw JSON array of recommendation objects
            # Non-list output is a semantic failure — fall through to standalone
            if not isinstance(cli_recs, list):
                print(
                    f"[cacheout-mcp] App-mode recommendations returned non-list, "
                    f"falling back to standalone",
                    file=sys.stderr,
                )
            else:
                # Enforce snapshot-only type contract
                recs = [
                    r for r in cli_recs
                    if isinstance(r, dict) and r.get("type") in _APP_MODE_TYPES
                ]
                return json.dumps({
                    "recommendations": recs,
                    "_meta": {
                        "mode": "app",
                        "count": len(recs),
                        "partial": True,
                        "source": "cli",
                    },
                }, indent=2)
        except Exception as e:
            print(
                f"[cacheout-mcp] App-mode recommendations failed, "
                f"falling back to standalone: {e}",
                file=sys.stderr,
            )

    # Standalone mode: basic recommendations from sysctl
    recs = []

    # compressor_low_ratio check
    ratio = await parse_sysctl_compressor_ratio()
    if ratio is not None and ratio > 0.0 and ratio < 2.0:
        recs.append({
            "type": "compressor_low_ratio",
            "message": f"Compression ratio {ratio:.2f} is below 2.0 threshold",
            "process": None,
            "pid": None,
            "impact_value": round(ratio, 4),
            "impact_unit": "ratio",
            "confidence": "low",
            "source": "standalone",
        })

    # swap_pressure check — threshold: swap used > 50% of physical RAM
    # Spec defines threshold relative to physical memory, not total swap space.
    # Contract: impact_value = swap usage as % of physical RAM, impact_unit = "percent"
    swap_used = await _async_parse_swap_used()
    physical_mem = await _async_sysctl_int("hw.memsize")
    if physical_mem is not None and swap_used is not None and physical_mem > 0:
        swap_pct = (swap_used / float(physical_mem)) * 100.0
        if swap_pct >= 50.0:
            recs.append({
                "type": "swap_pressure",
                "message": f"Swap usage at {swap_pct:.0f}% — system performance may be degraded",
                "process": None,
                "pid": None,
                "impact_value": round(swap_pct, 1),
                "impact_unit": "percent",
                "confidence": "low",
                "source": "standalone",
            })

    return json.dumps({
        "recommendations": recs,
        "_meta": {
            "mode": "standalone",
            "count": len(recs),
            "partial": True,
            "source": "standalone",
        },
    }, indent=2)


# ── Entry Point ──────────────────────────────────────────────────────

def main():
    """Run the MCP server via stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
