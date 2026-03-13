"""Smoke tests for cacheout-mcp — covers all MCP tools.

Test tiers (pytest markers):
  - (default)     Unit/mocked smoke tests — no bundled app, helper, or socket required.
                  Run with: pytest -m "not integration and not hardware"
  - integration   Requires real system (no mocks); tests MCP tool functions directly.
                  Run with: pytest -m integration
  - hardware      Manual certification runs on specific machines; self-skips on wrong tier.
                  Run with: pytest -m hardware

Note: This harness tests MCP tool response schemas via mocked dependencies.
CLI binary coverage (--cli scan, --cli clean, etc.) requires AppEngine
integration tests against the bundled binary and is out of scope here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────

def run_async(coro):
    """Run an async coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Unit Smoke: cacheout_get_disk_usage ──────────────────────────────

class TestGetDiskUsage:
    """Smoke tests for cacheout_get_disk_usage."""

    def test_returns_disk_info_standalone(self):
        @dataclass
        class FakeDisk:
            total_bytes: int = 500_100_000_000
            free_bytes: int = 23_400_000_000
            used_bytes: int = 476_700_000_000
            @property
            def free_gb(self):
                return self.free_bytes / 1_073_741_824
            @property
            def used_pct(self):
                return self.used_bytes / self.total_bytes * 100
            def to_dict(self):
                return {
                    "total": "500.1 GB", "free": "23.4 GB", "used": "476.7 GB",
                    "free_gb": round(self.free_gb, 2), "used_percent": round(self.used_pct, 1),
                }

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.get_disk_info", return_value=FakeDisk()):
            from cacheout_mcp.server import cacheout_get_disk_usage, GetDiskUsageInput
            result = run_async(cacheout_get_disk_usage(GetDiskUsageInput()))

        data = json.loads(result)
        assert data["free_gb"] > 0
        assert data["used_percent"] > 0
        assert "total" in data


# ── Unit Smoke: cacheout_scan_caches ─────────────────────────────────

class TestScanCaches:
    """Smoke tests for cacheout_scan_caches."""

    def test_returns_scan_results_standalone(self):
        """Patches scan_category (the leaf call used by standalone path)."""
        @dataclass
        class FakeScanResult:
            slug: str = "xcode_derived_data"
            name: str = "Xcode DerivedData"
            size_bytes: int = 15_000_000_000
            size_human: str = "15.0 GB"
            item_count: int = 42
            risk_level: str = "safe"
            description: str = "Build artifacts"
            rebuild_note: str = "Regenerates on next build"
            exists: bool = True

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.scan_category", return_value=FakeScanResult()), \
             patch("cacheout_mcp.server.ALL_CATEGORIES", [MagicMock()]):
            from cacheout_mcp.server import cacheout_scan_caches, ScanCachesInput
            result = run_async(cacheout_scan_caches(ScanCachesInput()))

        data = json.loads(result)
        assert "categories" in data
        assert "total_cleanable" in data
        assert len(data["categories"]) == 1
        assert data["categories"][0]["slug"] == "xcode_derived_data"


# ── Unit Smoke: cacheout_clear_cache ─────────────────────────────────

class TestClearCache:
    """Smoke tests for cacheout_clear_cache."""

    def test_clear_returns_results_standalone(self):
        @dataclass
        class FakeClean:
            category: str = "npm Cache"
            slug: str = "npm_cache"
            bytes_freed: int = 500_000_000
            success: bool = True
            error: str = None

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.clean_category", new_callable=AsyncMock, return_value=FakeClean()):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            result = run_async(cacheout_clear_cache(ClearCacheInput(
                categories=["npm_cache"], dry_run=True
            )))

        data = json.loads(result)
        assert "results" in data
        assert data["results"][0]["slug"] == "npm_cache"
        assert data["dry_run"] is True


# ── Unit Smoke: cacheout_smart_clean ─────────────────────────────────

class TestSmartClean:
    """Smoke tests for cacheout_smart_clean."""

    def test_smart_clean_returns_summary(self):
        fake_result = {
            "target_met": True,
            "target_gb": 10.0,
            "total_freed_bytes": 12_300_000_000,
            "total_freed_human": "12.3 GB",
            "categories_cleaned": [],
            "disk_before": {"free_gb": 5.0},
            "disk_after": {"free_gb": 17.3},
        }

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.smart_clean", new_callable=AsyncMock, return_value=fake_result):
            from cacheout_mcp.server import cacheout_smart_clean, SmartCleanInput
            result = run_async(cacheout_smart_clean(SmartCleanInput(target_gb=10.0)))

        data = json.loads(result)
        assert data["target_met"] is True
        assert "total_freed_human" in data

    def test_include_caution_gates_docker(self):
        """Verify include_caution=false prevents caution-level categories from being cleaned."""
        from cacheout_mcp.engine import smart_clean, scan_all, get_disk_info, clean_category, RiskLevel

        @dataclass
        class FakeScan:
            slug: str
            name: str
            exists: bool = True
            size_bytes: int = 5_000_000_000
            size_human: str = "5.0 GB"
            clean_priority: int = 10
            risk_level: str = "safe"

        @dataclass
        class FakeDisk:
            total_bytes: int = 500_000_000_000
            free_bytes: int = 10_000_000_000
            used_bytes: int = 490_000_000_000
            total_human: str = "500 GB"
            free_human: str = "10 GB"
            used_percent: float = 98.0
            def to_dict(self):
                return {"total_bytes": self.total_bytes, "free_bytes": self.free_bytes,
                        "free_gb": self.free_bytes / (1024**3)}

        @dataclass
        class FakeClean:
            bytes_freed: int = 5_000_000_000
            success: bool = True
            error: str = None

        # Only Docker available (caution-level, priority 90)
        fake_scans = [FakeScan(slug="docker_disk", name="Docker", clean_priority=90)]
        fake_docker_cat = MagicMock()
        fake_docker_cat.risk_level = RiskLevel.CAUTION

        with patch("cacheout_mcp.engine.scan_all", return_value=fake_scans), \
             patch("cacheout_mcp.engine.get_disk_info", return_value=FakeDisk()), \
             patch("cacheout_mcp.engine.clean_category", new_callable=AsyncMock, return_value=FakeClean()), \
             patch("cacheout_mcp.engine.CATEGORY_MAP", {"docker_disk": fake_docker_cat}):
            # Without include_caution: Docker should be skipped
            result_no_caution = run_async(smart_clean(target_gb=5.0, include_caution=False))
            assert len(result_no_caution["cleaned"]) == 0
            assert len(result_no_caution["skipped"]) == 1
            assert result_no_caution["skipped"][0]["slug"] == "docker_disk"

            # With include_caution: Docker should still be skipped (80% threshold not met, freed=0)
            result_caution = run_async(smart_clean(target_gb=5.0, include_caution=True))
            assert len(result_caution["cleaned"]) == 0  # 0% freed < 80% threshold


# ── Unit Smoke: cacheout_status ──────────────────────────────────────

class TestStatus:
    """Smoke tests for cacheout_status."""

    def test_status_returns_mode_and_categories(self):
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None):
            from cacheout_mcp.server import cacheout_status, ServerStatusInput
            result = run_async(cacheout_status(ServerStatusInput()))

        data = json.loads(result)
        assert data["mode"] == "standalone"
        assert "categories" in data


# ── Unit Smoke: cacheout_get_memory_stats ────────────────────────────

class TestGetMemoryStats:
    """Smoke tests for cacheout_get_memory_stats."""

    def test_memory_stats_standalone(self):
        """Mocks get_standalone_memory_stats with canonical schema fields."""
        fake_stats = {
            "total_physical_mb": 16384.0,
            "free_mb": 4096.0,
            "active_mb": 6000.0,
            "inactive_mb": 2000.0,
            "wired_mb": 3000.0,
            "compressed_mb": 500.0,
            "compressor_ratio": 2.5,
            "swap_used_mb": 256.0,
            "pressure_level": 1,
            "memory_tier": "comfortable",
            "estimated_available_mb": 6096.0,
            "mode": "standalone",
        }

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.get_standalone_memory_stats", new_callable=AsyncMock, return_value=fake_stats):
            from cacheout_mcp.server import cacheout_get_memory_stats, GetMemoryStatsInput
            result = run_async(cacheout_get_memory_stats(GetMemoryStatsInput()))

        data = json.loads(result)
        assert data["total_physical_mb"] == 16384.0
        assert data["memory_tier"] == "comfortable"
        assert data["mode"] == "standalone"
        # Verify all canonical fields from get_standalone_memory_stats
        for field in ("free_mb", "active_mb", "inactive_mb", "wired_mb",
                      "compressed_mb", "compressor_ratio", "swap_used_mb",
                      "pressure_level", "estimated_available_mb"):
            assert field in data, f"Missing canonical field: {field}"


# ── Unit Smoke: cacheout_check_alerts ────────────────────────────────

class TestCheckAlerts:
    """Smoke tests for cacheout_check_alerts."""

    def test_no_alert_file_returns_null(self, tmp_path):
        """Patches module-level ALERT_FILE and HISTORY_FILE paths."""
        fake_alert = tmp_path / "alert.json"
        fake_history = tmp_path / "watchdog-history.json"

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", fake_alert), \
             patch("cacheout_mcp.server.HISTORY_FILE", fake_history):
            from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput
            result = run_async(cacheout_check_alerts(CheckAlertsInput()))

        data = json.loads(result)
        assert data["alert"] is None

    def test_with_alert_file(self, tmp_path):
        """Verify alert is returned when sentinel file exists."""
        import time
        fake_alert = tmp_path / "alert.json"
        fake_alert.write_text(json.dumps({
            "level": "warning",
            "triggers": ["disk_velocity"],
            "timestamp": time.time(),
        }))
        fake_history = tmp_path / "watchdog-history.json"

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", fake_alert), \
             patch("cacheout_mcp.server.HISTORY_FILE", fake_history):
            from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput
            result = run_async(cacheout_check_alerts(CheckAlertsInput()))

        data = json.loads(result)
        assert data["alert"]["level"] == "warning"


# ── Unit Smoke: cacheout_get_recommendations ─────────────────────────

class TestGetRecommendations:
    """Smoke tests for cacheout_get_recommendations."""

    def test_standalone_returns_recommendations(self):
        """Patches all leaf dependencies including parse_sysctl_compressor_ratio."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, side_effect=[
                 17179869184,  # hw.memsize
                 4,            # memorystatus_level
             ]), \
             patch("cacheout_mcp.server._async_parse_swap_total", new_callable=AsyncMock, return_value=2147483648), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=536870912), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=2.5), \
             patch("cacheout_mcp.server._async_run", new_callable=AsyncMock, return_value=""):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert "recommendations" in data
        assert "_meta" in data
        assert data["_meta"]["mode"] == "standalone"


# ── Unit Smoke: cacheout_system_health ───────────────────────────────

class TestSystemHealth:
    """Smoke tests for cacheout_system_health."""

    def test_system_health_standalone(self):
        """Mocks get_standalone_memory_stats and swap helpers for health score."""
        fake_stats = {
            "total_physical_mb": 16384.0,
            "free_mb": 4096.0,
            "active_mb": 6000.0,
            "inactive_mb": 2000.0,
            "wired_mb": 3000.0,
            "compressed_mb": 500.0,
            "compressor_ratio": 2.5,
            "swap_used_mb": 256.0,
            "pressure_level": 1,
            "memory_tier": "comfortable",
            "estimated_available_mb": 6096.0,
            "mode": "standalone",
        }

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.get_standalone_memory_stats", new_callable=AsyncMock, return_value=fake_stats), \
             patch("cacheout_mcp.server._async_parse_swap_total", new_callable=AsyncMock, return_value=2147483648), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=536870912):
            from cacheout_mcp.server import cacheout_system_health, SystemHealthInput
            result = run_async(cacheout_system_health(SystemHealthInput()))

        data = json.loads(result)
        assert "score" in data
        assert data["source"] == "standalone"
        assert isinstance(data["score"], (int, float))


# ── Unit Smoke: cacheout_configure_autopilot ─────────────────────────

class TestConfigureAutopilot:
    """Smoke tests for cacheout_configure_autopilot."""

    def test_set_autopilot_config(self, tmp_path):
        """ConfigureAutopilotInput requires a config dict (write/validate/apply tool)."""
        config = {"version": 1, "enabled": False}
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._get_state_dir", return_value=str(tmp_path)), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False):
            from cacheout_mcp.server import cacheout_configure_autopilot, ConfigureAutopilotInput
            result = run_async(cacheout_configure_autopilot(
                ConfigureAutopilotInput(config=config)
            ))

        data = json.loads(result)
        assert data["success"] is True
        assert data["saved"] is True


# ── Unit Smoke: cacheout_get_process_memory ──────────────────────────

class TestGetProcessMemory:
    """Smoke tests for cacheout_get_process_memory."""

    def test_process_memory_standalone(self):
        """Patches get_standalone_process_memory with canonical envelope."""
        fake_result = {
            "mode": "standalone",
            "partial": False,
            "capabilities": {"sort_by_rss": True, "sort_by_phys_footprint": False, "sort_by_pageins": False},
            "data": {
                "processes": [
                    {"pid": 12345, "command": "python3", "rss_kb": 102400,
                     "note": "RSS-based estimate (not true physical footprint)"},
                ],
                "count": 1,
                "sort_by_applied": "rss",
                "available_sort_keys": ["rss"],
            },
        }

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.get_standalone_process_memory", new_callable=AsyncMock, return_value=fake_result):
            from cacheout_mcp.server import cacheout_get_process_memory
            from cacheout_mcp.memory_models import GetProcessMemoryInput
            result = run_async(cacheout_get_process_memory(GetProcessMemoryInput()))

        data = json.loads(result)
        assert data["mode"] == "standalone"
        assert "data" in data
        assert len(data["data"]["processes"]) == 1
        assert data["data"]["processes"][0]["rss_kb"] == 102400


# ── Unit Smoke: cacheout_get_compressor_health ───────────────────────

class TestGetCompressorHealth:
    """Smoke tests for cacheout_get_compressor_health."""

    def test_compressor_health_standalone(self):
        """Patches get_standalone_compressor_health with canonical envelope."""
        fake_result = {
            "mode": "standalone",
            "partial": True,
            "capabilities": {
                "ratio": True, "rates": True,
                "thrashing_instantaneous": True, "thrashing_sustained": False, "trend": False,
            },
            "data": {
                "compressor_ratio": 2.5,
                "compressed_mb": 1024.0,
                "original_data_mb": 2560.0,
                "compression_rate_per_sec": 10.0,
                "decompression_rate_per_sec": 5.0,
                "thrashing": False,
                "thrashing_sustained": None,
                "thrashing_note": "requires 30s+ sustained sampling",
                "pressure_level": 0,
                "pressure_label": "normal",
                "trend": "unknown",
                "trend_note": "insufficient history for trend",
            },
        }

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.get_standalone_compressor_health", new_callable=AsyncMock, return_value=fake_result):
            from cacheout_mcp.server import cacheout_get_compressor_health
            from cacheout_mcp.memory_models import GetCompressorHealthInput
            result = run_async(cacheout_get_compressor_health(GetCompressorHealthInput()))

        data = json.loads(result)
        assert data["mode"] == "standalone"
        assert data["data"]["compressor_ratio"] == 2.5
        assert data["data"]["thrashing"] is False


# ── Unit Smoke: cacheout_memory_intervention ─────────────────────────

class TestMemoryIntervention:
    """Smoke tests for cacheout_memory_intervention."""

    def test_purge_dry_run_standalone(self):
        """Patches run_standalone_intervention with canonical dry-run envelope."""
        fake_result = {
            "mode": "standalone",
            "capabilities": {"purge": True},
            "data": {
                "dry_run": True,
                "intervention": "purge",
                "description": "Flush the Unified Buffer Cache (UBC) to reclaim purgeable memory pages.",
                "estimated_reclaim_mb": None,
                "estimate_note": "Estimate unavailable pre-execution",
            },
            "partial": False,
        }

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.run_standalone_intervention", new_callable=AsyncMock, return_value=fake_result):
            from cacheout_mcp.server import cacheout_memory_intervention
            from cacheout_mcp.memory_models import MemoryInterventionInput
            result = run_async(cacheout_memory_intervention(
                MemoryInterventionInput(intervention_name="purge", confirm=False)
            ))

        data = json.loads(result)
        assert data["mode"] == "standalone"
        assert data["data"]["dry_run"] is True
        assert data["data"]["intervention"] == "purge"


# ══════════════════════════════════════════════════════════════════════
# Integration Tests — require real system (no mocks)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestIntegrationDiskUsage:
    """Integration: cacheout_get_disk_usage against real system."""

    def test_real_disk_usage(self):
        from cacheout_mcp.server import cacheout_get_disk_usage, GetDiskUsageInput
        result = run_async(cacheout_get_disk_usage(GetDiskUsageInput()))
        data = json.loads(result)
        assert data["free_gb"] > 0
        assert data["used_percent"] > 0


@pytest.mark.integration
class TestIntegrationScanCaches:
    """Integration: cacheout_scan_caches against real system."""

    def test_real_scan(self):
        from cacheout_mcp.server import cacheout_scan_caches, ScanCachesInput
        result = run_async(cacheout_scan_caches(ScanCachesInput()))
        data = json.loads(result)
        assert "categories" in data
        assert "total_cleanable" in data


@pytest.mark.integration
class TestIntegrationStatus:
    """Integration: cacheout_status against real system."""

    def test_real_status(self):
        from cacheout_mcp.server import cacheout_status, ServerStatusInput
        result = run_async(cacheout_status(ServerStatusInput()))
        data = json.loads(result)
        assert data["mode"] in ("standalone", "app", "socket")


@pytest.mark.integration
class TestIntegrationMemoryStats:
    """Integration: cacheout_get_memory_stats against real system."""

    def test_real_memory_stats(self):
        from cacheout_mcp.server import cacheout_get_memory_stats, GetMemoryStatsInput
        result = run_async(cacheout_get_memory_stats(GetMemoryStatsInput()))
        data = json.loads(result)
        # Canonical standalone fields
        assert data["total_physical_mb"] > 0
        assert data["memory_tier"] in ("abundant", "comfortable", "moderate", "constrained", "critical")


# ══════════════════════════════════════════════════════════════════════
# Hardware Tests — manual certification on specific machines
# Self-skip on wrong hardware tier to prevent brittle failures.
# ══════════════════════════════════════════════════════════════════════

def _get_total_ram_gb():
    """Helper: read total physical RAM in GB for hardware tier detection."""
    from cacheout_mcp.server import cacheout_get_memory_stats, GetMemoryStatsInput
    result = run_async(cacheout_get_memory_stats(GetMemoryStatsInput()))
    data = json.loads(result)
    return data["total_physical_mb"] / 1024.0


@pytest.mark.hardware
class TestHardware8GB:
    """Hardware certification: 8 GB Mac.

    Asserts stable facts (RAM band). Workload-dependent values like
    memory_tier are printed for the certification log but not hard-asserted.
    """

    def test_ram_band_and_stats(self):
        """Verify 8GB machine reports correct RAM band and valid memory stats."""
        ram_gb = _get_total_ram_gb()
        if ram_gb > 10:
            pytest.skip(f"Not an 8GB machine (has {ram_gb:.1f} GB RAM)")

        from cacheout_mcp.server import cacheout_get_memory_stats, GetMemoryStatsInput
        result = run_async(cacheout_get_memory_stats(GetMemoryStatsInput()))
        data = json.loads(result)
        # Stable: RAM is in the 8GB band
        assert 6 <= data["total_physical_mb"] / 1024.0 <= 10
        # Stable: valid tier returned
        assert data["memory_tier"] in ("abundant", "comfortable", "moderate", "constrained", "critical")
        # Log for certification matrix (workload-dependent, not asserted)
        print(f"  8GB certification: memory_tier={data['memory_tier']}, "
              f"estimated_available_mb={data.get('estimated_available_mb')}")


@pytest.mark.hardware
class TestHardware16GB:
    """Hardware certification: 16 GB Mac."""

    def test_ram_band_and_stats(self):
        ram_gb = _get_total_ram_gb()
        if ram_gb < 14 or ram_gb > 18:
            pytest.skip(f"Not a 16GB machine (has {ram_gb:.1f} GB RAM)")

        from cacheout_mcp.server import cacheout_get_memory_stats, GetMemoryStatsInput
        result = run_async(cacheout_get_memory_stats(GetMemoryStatsInput()))
        data = json.loads(result)
        assert 14 <= data["total_physical_mb"] / 1024.0 <= 18
        assert data["memory_tier"] in ("abundant", "comfortable", "moderate", "constrained", "critical")
        print(f"  16GB certification: memory_tier={data['memory_tier']}, "
              f"estimated_available_mb={data.get('estimated_available_mb')}")


@pytest.mark.hardware
class TestHardware128GB:
    """Hardware certification: 96-128+ GB Mac (memlimit workaround)."""

    def test_ram_band_and_stats(self):
        ram_gb = _get_total_ram_gb()
        if ram_gb < 90 or ram_gb > 140:
            pytest.skip(f"Not a 128GB machine (has {ram_gb:.1f} GB RAM)")

        from cacheout_mcp.server import cacheout_get_memory_stats, GetMemoryStatsInput
        result = run_async(cacheout_get_memory_stats(GetMemoryStatsInput()))
        data = json.loads(result)
        assert 90 <= data["total_physical_mb"] / 1024.0 <= 140
        assert data["memory_tier"] in ("abundant", "comfortable", "moderate", "constrained", "critical")
        print(f"  128GB certification: memory_tier={data['memory_tier']}, "
              f"estimated_available_mb={data.get('estimated_available_mb')}")

    def test_smart_clean_dry_run(self):
        """128GB machines should still be able to do dry-run smart cleans."""
        ram_gb = _get_total_ram_gb()
        if ram_gb < 90 or ram_gb > 140:
            pytest.skip(f"Not a 128GB machine (has {ram_gb:.1f} GB RAM)")

        from cacheout_mcp.server import cacheout_smart_clean, SmartCleanInput
        result = run_async(cacheout_smart_clean(SmartCleanInput(target_gb=5.0, dry_run=True)))
        data = json.loads(result)
        assert "target_met" in data
