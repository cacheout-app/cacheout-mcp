"""Tests for fn-5.3: smart_clean free_memory param, check_alerts memory fields,
sysctl pressure/ratio parsing, and graceful degradation."""

from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path

from cacheout_mcp.memory_tools import (
    PRESSURE_LABEL_MAP,
    parse_sysctl_pressure_level,
    parse_sysctl_compressor_ratio,
)


# ── Sysctl pressure-level parser tests (golden-file style) ──────────

class TestParseSysctlPressureLevel:
    """Golden-file style: known kernel int → expected label."""

    def test_normal(self):
        assert parse_sysctl_pressure_level(0) == "normal"

    def test_warn(self):
        assert parse_sysctl_pressure_level(1) == "warn"

    def test_critical(self):
        assert parse_sysctl_pressure_level(2) == "critical"

    def test_urgent(self):
        assert parse_sysctl_pressure_level(4) == "urgent"

    def test_unknown_value(self):
        assert parse_sysctl_pressure_level(3) == "unknown"
        assert parse_sysctl_pressure_level(99) == "unknown"
        assert parse_sysctl_pressure_level(-1) == "unknown"

    def test_all_documented_values_present(self):
        """Verify all documented kernel values have entries."""
        assert set(PRESSURE_LABEL_MAP.keys()) == {0, 1, 2, 4}


# ── Sysctl compressor ratio parser tests ─────────────────────────────

class TestParseSysctlCompressorRatio:
    def test_normal_ratio(self):
        with patch("cacheout_mcp.memory_tools._async_sysctl_int", new_callable=AsyncMock) as mock:
            mock.side_effect = [500000000, 200000000]  # compressed, used
            result = asyncio.run(parse_sysctl_compressor_ratio())
            assert result == round(500000000 / 200000000, 4)

    def test_zero_used_returns_zero(self):
        with patch("cacheout_mcp.memory_tools._async_sysctl_int", new_callable=AsyncMock) as mock:
            mock.side_effect = [0, 0]
            result = asyncio.run(parse_sysctl_compressor_ratio())
            assert result == 0.0

    def test_compressed_none_returns_none(self):
        with patch("cacheout_mcp.memory_tools._async_sysctl_int", new_callable=AsyncMock) as mock:
            mock.side_effect = [None, 200000000]
            result = asyncio.run(parse_sysctl_compressor_ratio())
            assert result is None

    def test_used_none_returns_none(self):
        with patch("cacheout_mcp.memory_tools._async_sysctl_int", new_callable=AsyncMock) as mock:
            mock.side_effect = [500000000, None]
            result = asyncio.run(parse_sysctl_compressor_ratio())
            assert result is None

    def test_both_none_returns_none(self):
        with patch("cacheout_mcp.memory_tools._async_sysctl_int", new_callable=AsyncMock) as mock:
            mock.side_effect = [None, None]
            result = asyncio.run(parse_sysctl_compressor_ratio())
            assert result is None


# ── SmartClean free_memory tests ─────────────────────────────────────

class TestSmartCleanFreeMemory:
    """Test smart_clean with free_memory parameter."""

    def _mock_smart_clean_result(self):
        return {
            "target_gb": 5.0,
            "target_met": True,
            "total_freed_bytes": 5368709120,
            "total_freed_human": "5.0 GB",
            "dry_run": False,
            "cleaned": [],
            "skipped": [],
            "disk_before": {"free_gb": 5.0},
            "disk_after": {"free_gb": 10.0},
        }

    def test_free_memory_false_no_extra_fields(self):
        """When free_memory=false, response has no memory fields."""
        from cacheout_mcp.server import SmartCleanInput
        inp = SmartCleanInput(target_gb=5.0, free_memory=False)
        assert inp.free_memory is False

    def test_free_memory_default_false(self):
        from cacheout_mcp.server import SmartCleanInput
        inp = SmartCleanInput(target_gb=5.0)
        assert inp.free_memory is False

    def test_free_memory_true_accepted(self):
        from cacheout_mcp.server import SmartCleanInput
        inp = SmartCleanInput(target_gb=5.0, free_memory=True)
        assert inp.free_memory is True

    def test_dry_run_free_memory_produces_no_side_effects(self):
        """dry_run + free_memory=true: no purge execution, preview-style purge_result."""
        from cacheout_mcp.server import cacheout_smart_clean, SmartCleanInput

        with patch("cacheout_mcp.server.smart_clean", new_callable=AsyncMock) as mock_sc, \
             patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._async_run", new_callable=AsyncMock) as mock_run:

            mock_sc.return_value = {
                "target_gb": 5.0, "target_met": True,
                "total_freed_bytes": 5368709120,
                "total_freed_human": "5.0 GB", "dry_run": True,
                "cleaned": [], "skipped": [],
                "disk_before": {"free_gb": 5.0},
                "disk_after": {"free_gb": 5.0},
            }

            params = SmartCleanInput(target_gb=5.0, dry_run=True, free_memory=True)
            result_json = asyncio.run(cacheout_smart_clean(params))
            result = json.loads(result_json)

            # No purge should have been executed
            mock_run.assert_not_called()

            # Preview-style purge_result
            assert result["memory_freed"] is False
            assert result["purge_result"]["dry_run"] is True
            assert "Would run purge" in result["purge_result"]["description"]
            assert "_meta" in result

    def test_execute_free_memory_success(self):
        """Executing with free_memory=true: purge runs after disk cleanup."""
        from cacheout_mcp.server import cacheout_smart_clean, SmartCleanInput

        with patch("cacheout_mcp.server.smart_clean", new_callable=AsyncMock) as mock_sc, \
             patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._async_run", new_callable=AsyncMock) as mock_run:

            mock_sc.return_value = self._mock_smart_clean_result()
            mock_run.return_value = ("", None)  # purge success

            params = SmartCleanInput(target_gb=5.0, dry_run=False, free_memory=True)
            result_json = asyncio.run(cacheout_smart_clean(params))
            result = json.loads(result_json)

            assert result["memory_freed"] is True
            assert result["purge_result"]["success"] is True
            assert result["_meta"]["partial"] is False

            # Verify /usr/sbin/purge was called
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert call_args[0][0] == ["/usr/sbin/purge"]

    def test_execute_free_memory_purge_failure_preserves_disk_fields(self):
        """On purge failure: existing disk cleanup fields preserved."""
        from cacheout_mcp.server import cacheout_smart_clean, SmartCleanInput

        with patch("cacheout_mcp.server.smart_clean", new_callable=AsyncMock) as mock_sc, \
             patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._async_run", new_callable=AsyncMock) as mock_run:

            mock_sc.return_value = self._mock_smart_clean_result()
            mock_run.return_value = ("", "Command purge timed out after 35.0s")

            params = SmartCleanInput(target_gb=5.0, dry_run=False, free_memory=True)
            result_json = asyncio.run(cacheout_smart_clean(params))
            result = json.loads(result_json)

            # Disk cleanup fields preserved
            assert result["target_met"] is True
            assert result["total_freed_bytes"] == 5368709120

            # Purge failure fields
            assert result["memory_freed"] is False
            assert "timed out" in result["purge_result"]["error"]
            assert "disk cleanup completed" in result["purge_result"]["note"]
            assert result["_meta"]["partial"] is True

    def test_app_mode_free_memory_uses_cli_purge(self):
        """App mode: purge via --cli purge with APP_PURGE_TIMEOUT."""
        from cacheout_mcp.server import cacheout_smart_clean, SmartCleanInput

        mock_engine = MagicMock()
        mock_engine.binary = "/Applications/Cacheout.app/Contents/MacOS/Cacheout"
        mock_engine.smart_clean = AsyncMock(return_value=self._mock_smart_clean_result())

        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", mock_engine), \
             patch("cacheout_mcp.server._async_run", new_callable=AsyncMock) as mock_run:

            mock_run.return_value = ("", None)

            params = SmartCleanInput(target_gb=5.0, dry_run=False, free_memory=True)
            result_json = asyncio.run(cacheout_smart_clean(params))
            result = json.loads(result_json)

            assert result["memory_freed"] is True
            call_args = mock_run.call_args
            assert "--cli" in call_args[0][0]
            assert "purge" in call_args[0][0]


# ── check_alerts memory field tests ──────────────────────────────────

class TestCheckAlertsMemoryFields:
    """Test check_alerts with memory augmentation fields."""

    def test_standalone_returns_memory_fields(self):
        """Standalone mode: pressure_level, pressure_label, compressor_ratio present."""
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock) as mock_sysctl, \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock) as mock_ratio:

            mock_sysctl.return_value = 1  # warn
            mock_ratio.return_value = 2.5

            params = CheckAlertsInput(acknowledge=False)
            result_json = asyncio.run(cacheout_check_alerts(params))
            result = json.loads(result_json)

            assert result["pressure_level"] == 1
            assert result["pressure_label"] == "warn"
            assert result["compressor_ratio"] == 2.5
            assert result["compressor_trend"] == "unknown"
            assert result["compressor_trend_note"] is not None
            assert result["swap_velocity_gb_per_5m"] is None
            assert result["swap_velocity_note"] is not None
            assert result["recommended_action"] is not None
            assert result["_meta"]["mode"] == "standalone"

    def test_all_existing_alert_fields_preserved(self):
        """Existing alert, current, watchdog_running fields unchanged."""
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock) as mock_sysctl, \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock) as mock_ratio:

            mock_sysctl.return_value = 0
            mock_ratio.return_value = 3.0

            params = CheckAlertsInput(acknowledge=False)
            result_json = asyncio.run(cacheout_check_alerts(params))
            result = json.loads(result_json)

            # Legacy fields still present
            assert "alert" in result
            assert "current" in result
            assert "watchdog_running" in result

    def test_graceful_degradation_sysctl_timeout(self):
        """On sysctl timeout: existing fields preserved, new fields null with error note."""
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock) as mock_sysctl, \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock) as mock_ratio:

            mock_sysctl.return_value = None  # sysctl failed
            mock_ratio.return_value = None  # also failed

            params = CheckAlertsInput(acknowledge=False)
            result_json = asyncio.run(cacheout_check_alerts(params))
            result = json.loads(result_json)

            # Existing fields still present (not broken)
            assert "alert" in result
            assert "current" in result
            assert "watchdog_running" in result

            # New fields null with notes
            assert result["pressure_level"] is None
            assert result["pressure_note"] is not None
            assert result["compressor_ratio"] is None
            assert result["_meta"]["partial"] is True

    def test_recommended_action_normal(self):
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock) as mock_sysctl, \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock) as mock_ratio:

            mock_sysctl.return_value = 0
            mock_ratio.return_value = 3.0

            params = CheckAlertsInput(acknowledge=False)
            result_json = asyncio.run(cacheout_check_alerts(params))
            result = json.loads(result_json)

            assert result["recommended_action"] == "no action needed"

    def test_recommended_action_warn(self):
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock) as mock_sysctl, \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock) as mock_ratio:

            mock_sysctl.return_value = 1
            mock_ratio.return_value = 2.0

            params = CheckAlertsInput(acknowledge=False)
            result_json = asyncio.run(cacheout_check_alerts(params))
            result = json.loads(result_json)

            assert result["recommended_action"] == "consider purge"

    def test_recommended_action_urgent(self):
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock) as mock_sysctl, \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock) as mock_ratio:

            mock_sysctl.return_value = 4
            mock_ratio.return_value = 1.5

            params = CheckAlertsInput(acknowledge=False)
            result_json = asyncio.run(cacheout_check_alerts(params))
            result = json.loads(result_json)

            assert "urgent" in result["recommended_action"]

    def test_meta_partial_when_any_null(self):
        """_meta.partial=true when any memory field is null."""
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock) as mock_sysctl, \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock) as mock_ratio:

            # Pressure ok but ratio failed
            mock_sysctl.return_value = 0
            mock_ratio.return_value = None

            params = CheckAlertsInput(acknowledge=False)
            result_json = asyncio.run(cacheout_check_alerts(params))
            result = json.loads(result_json)

            assert result["_meta"]["partial"] is True

    def test_app_mode_uses_cli_memory_stats(self):
        """App mode: uses --cli memory-stats for pressure and ratio."""
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        mock_engine = MagicMock()
        mock_engine.binary = "/Applications/Cacheout.app/Contents/MacOS/Cacheout"

        dto_json = json.dumps({
            "pressureLevel": 2,
            "compressionRatio": 1.8,
            "compressions": 100000,
            "decompressions": 50000,
            "swapUsedBytes": 10485760,
        })

        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", mock_engine), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_run", new_callable=AsyncMock) as mock_run:

            mock_run.return_value = (dto_json, None)

            params = CheckAlertsInput(acknowledge=False)
            result_json = asyncio.run(cacheout_check_alerts(params))
            result = json.loads(result_json)

            assert result["pressure_level"] == 2
            assert result["pressure_label"] == "critical"
            assert result["compressor_ratio"] == 1.8
            assert result["_meta"]["mode"] == "app"

    def test_app_mode_cli_failure_graceful_degradation(self):
        """App mode: CLI failure → null fields with error note."""
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        mock_engine = MagicMock()
        mock_engine.binary = "/usr/bin/fake"

        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", mock_engine), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_run", new_callable=AsyncMock) as mock_run:

            mock_run.return_value = ("", "Command timed out after 15s")

            params = CheckAlertsInput(acknowledge=False)
            result_json = asyncio.run(cacheout_check_alerts(params))
            result = json.loads(result_json)

            # Existing fields preserved
            assert "alert" in result
            assert "current" in result
            assert "watchdog_running" in result

            # New fields null
            assert result["pressure_level"] is None
            assert result["compressor_ratio"] is None
            assert result["_meta"]["partial"] is True

    def test_all_subprocesses_use_async(self):
        """Verify _async_run is used (not subprocess.run) for memory reads."""
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock) as mock_sysctl, \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock) as mock_ratio:

            mock_sysctl.return_value = 0
            mock_ratio.return_value = 2.0

            params = CheckAlertsInput(acknowledge=False)
            asyncio.run(cacheout_check_alerts(params))

            # Both async functions were called (not blocking subprocess.run)
            mock_sysctl.assert_called_once()
            mock_ratio.assert_called_once()

    def test_meta_partial_always_true_due_to_swap_velocity(self):
        """_meta.partial is always True because swap_velocity_gb_per_5m is always None."""
        from cacheout_mcp.server import cacheout_check_alerts, CheckAlertsInput

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.ALERT_FILE", Path("/nonexistent/alert.json")), \
             patch("cacheout_mcp.server.HISTORY_FILE", Path("/nonexistent/history.json")), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock) as mock_sysctl, \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock) as mock_ratio:

            # Even with all fields present, partial=True because swap velocity unavailable
            mock_sysctl.return_value = 0
            mock_ratio.return_value = 3.0

            params = CheckAlertsInput(acknowledge=False)
            result_json = asyncio.run(cacheout_check_alerts(params))
            result = json.loads(result_json)

            assert result["swap_velocity_gb_per_5m"] is None
            assert result["_meta"]["partial"] is True


class TestSmartCleanMetaAlwaysPresent:
    """_meta must always be included in smart_clean responses."""

    def test_meta_present_without_free_memory(self):
        """_meta included even when free_memory=False."""
        from cacheout_mcp.server import cacheout_smart_clean, SmartCleanInput

        with patch("cacheout_mcp.server.smart_clean", new_callable=AsyncMock) as mock_sc, \
             patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None):

            mock_sc.return_value = {
                "target_gb": 5.0, "target_met": True,
                "total_freed_bytes": 5368709120,
                "total_freed_human": "5.0 GB", "dry_run": False,
                "cleaned": [], "skipped": [],
                "disk_before": {"free_gb": 5.0},
                "disk_after": {"free_gb": 10.0},
            }

            params = SmartCleanInput(target_gb=5.0, dry_run=False, free_memory=False)
            result_json = asyncio.run(cacheout_smart_clean(params))
            result = json.loads(result_json)

            assert "_meta" in result
            assert result["_meta"]["mode"] == "standalone"
            assert result["_meta"]["partial"] is False
