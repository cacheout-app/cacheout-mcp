"""Pydantic models for memory statistics and process memory tools.

Schema matches the output of `Cacheout --cli memory-stats` (fn-1.1).
All `_mb` fields use MiB (mebibytes, 1 MiB = 1048576 bytes).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class MemoryStatsOutput(BaseModel):
    """System memory statistics returned by cacheout_get_memory_stats."""

    model_config = ConfigDict(extra="forbid")

    total_physical_mb: float = Field(
        description="Total physical RAM in MiB"
    )
    free_mb: float = Field(
        description="Free (unused) memory in MiB"
    )
    active_mb: float = Field(
        description="Active memory in MiB (recently used)"
    )
    inactive_mb: float = Field(
        description="Inactive memory in MiB (not recently used, reclaimable)"
    )
    wired_mb: float = Field(
        description="Wired memory in MiB (kernel, cannot be paged out)"
    )
    compressed_mb: float = Field(
        description="Compressed memory in MiB (compressor footprint)"
    )
    compressor_ratio: float = Field(
        description="Compression ratio (original / compressed); 0 if nothing compressed"
    )
    swap_used_mb: float = Field(
        description="Swap space used in MiB"
    )
    pressure_level: int = Field(
        description="macOS memory pressure level (1=normal, 2=warn, 4=critical)"
    )
    memory_tier: str = Field(
        description=(
            "Human-friendly tier: abundant (>8GB avail), comfortable (4-8GB), "
            "moderate (1.5-4GB), constrained (<1.5GB or pressure>=2), "
            "critical (<512MB or pressure>=4)"
        )
    )
    estimated_available_mb: float = Field(
        description="Estimated available memory in MiB (free + inactive)"
    )
    mode: str = Field(
        description="How stats were gathered: 'standalone' or 'app'"
    )


# ── Process Memory Models ────────────────────────────────────────────

class GetProcessMemoryInput(BaseModel):
    """Input for cacheout_get_process_memory tool."""
    model_config = ConfigDict(extra="forbid")

    top_n: int = Field(
        default=10,
        description="Number of top processes to return (by memory usage).",
        ge=1,
        le=200,
    )
    sort_by: Optional[str] = Field(
        default=None,
        description=(
            "Sort key for process list. Available keys depend on mode: "
            "standalone supports 'rss'; app mode supports 'phys_footprint'. "
            "If omitted, uses the mode-specific default."
        ),
    )


class GetCompressorHealthInput(BaseModel):
    """Input for cacheout_get_compressor_health tool."""
    model_config = ConfigDict(extra="forbid")


class MemoryInterventionInput(BaseModel):
    """Input for cacheout_memory_intervention tool.

    Single ``confirm`` boolean controls dry-run vs execute:
      - confirm=False (default): dry-run, describes what would happen
      - confirm=True: executes the intervention
    """
    model_config = ConfigDict(extra="forbid")

    intervention_name: str = Field(
        description=(
            "Canonical intervention name. Available: purge, trigger_pressure_warn, "
            "reduce_transparency, delete_sleepimage, cleanup_snapshots, flush_compositor. "
            "Standalone mode only supports 'purge'."
        ),
    )
    confirm: bool = Field(
        default=False,
        description=(
            "If false (default), returns a dry-run description without side effects. "
            "If true, executes the intervention. Always call with confirm=false first "
            "to preview, then confirm=true to execute."
        ),
    )
    target_pid: Optional[int] = Field(
        default=None,
        description="Optional target process ID (reserved for future per-process interventions).",
    )
