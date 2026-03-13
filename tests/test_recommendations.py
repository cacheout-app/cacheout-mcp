"""Tests for cacheout_get_recommendations MCP tool.

Covers all three modes (socket, app, standalone) with mocked responses,
degraded partial behavior, confidence fields, canonical schema, and
verifies standalone uses compressor_low_ratio (not compressor_degrading).
"""

from __future__ import annotations

import asyncio
import json
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


# ── Socket Mode Tests ────────────────────────────────────────────────

class TestRecommendationsSocketMode:
    """Socket mode: full recommendations from daemon."""

    def test_socket_mode_returns_daemon_recommendations(self):
        daemon_response = {
            "recommendations": [
                {
                    "type": "compressor_degrading",
                    "message": "Compression ratio declining over 5 samples",
                    "process": None,
                    "pid": None,
                    "impact_value": 1.8,
                    "impact_unit": "ratio_per_second",
                    "confidence": "high",
                    "source": "daemon",
                },
                {
                    "type": "high_growth_process",
                    "message": "node (PID 12345) at 2.1 GB — near lifetime peak",
                    "process": "node",
                    "pid": 12345,
                    "impact_value": 2.1,
                    "impact_unit": "GB",
                    "confidence": "high",
                    "source": "daemon",
                },
            ],
            "_meta": {
                "count": 2,
                "source": "daemon",
                "scan_partial": False,
            },
        }

        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server.socket_recommendations", new_callable=AsyncMock, return_value=daemon_response):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert len(data["recommendations"]) == 2
        assert data["_meta"]["mode"] == "socket"
        assert data["_meta"]["count"] == 2
        assert data["_meta"]["partial"] is False
        assert data["_meta"]["source"] == "daemon"

        # Verify canonical schema fields on first recommendation
        rec = data["recommendations"][0]
        assert rec["type"] == "compressor_degrading"
        assert "message" in rec
        assert "confidence" in rec
        assert "source" in rec
        assert "impact_value" in rec
        assert "impact_unit" in rec

    def test_socket_mode_partial_when_daemon_scan_partial(self):
        """Socket mode with partial daemon scan propagates partial:true."""
        daemon_response = {
            "recommendations": [
                {
                    "type": "swap_pressure",
                    "message": "Swap usage at 65% — system performance may be degraded",
                    "process": None,
                    "pid": None,
                    "impact_value": 65.0,
                    "impact_unit": "percent",
                    "confidence": "high",
                    "source": "daemon",
                },
            ],
            "_meta": {
                "count": 1,
                "source": "daemon",
                "scan_partial": True,
            },
        }

        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server.socket_recommendations", new_callable=AsyncMock, return_value=daemon_response):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert data["_meta"]["partial"] is True
        assert data["_meta"]["source"] == "daemon"

    def test_socket_mode_falls_through_on_failure(self):
        """When socket returns None, falls through to app/standalone."""
        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server.socket_recommendations", new_callable=AsyncMock, return_value=None), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=3.0), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        # Fell through to standalone
        assert data["_meta"]["mode"] == "standalone"
        assert data["_meta"]["partial"] is True

    def test_socket_mode_can_emit_all_seven_types(self):
        """Socket mode can emit any of the 7 canonical types."""
        all_types = [
            "exhaustion_imminent", "compressor_degrading", "compressor_low_ratio",
            "high_growth_process", "rosetta_detected", "agent_memory_pressure",
            "swap_pressure",
        ]
        recs = [
            {"type": t, "message": f"Test {t}", "process": None, "pid": None,
             "impact_value": 1.0, "impact_unit": "test", "confidence": "high", "source": "daemon"}
            for t in all_types
        ]
        daemon_response = {
            "recommendations": recs,
            "_meta": {"count": 7, "source": "daemon", "scan_partial": False},
        }

        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server.socket_recommendations", new_callable=AsyncMock, return_value=daemon_response):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        types_returned = [r["type"] for r in data["recommendations"]]
        assert set(types_returned) == set(all_types)
        assert data["_meta"]["count"] == 7


# ── App Mode Tests ───────────────────────────────────────────────────

class TestRecommendationsAppMode:
    """App mode: degraded recommendations from CLI one-shot."""

    def test_app_mode_returns_cli_recommendations(self):
        """App mode wraps CLI array in standard envelope with partial=true."""
        cli_recs = [
            {
                "type": "compressor_low_ratio",
                "message": "Compression ratio 1.5 below threshold",
                "process": None,
                "pid": None,
                "impact_value": 1.5,
                "impact_unit": "ratio",
                "confidence": "low",
                "source": "cli",
            },
            {
                "type": "swap_pressure",
                "message": "Swap usage at 60% — system performance may be degraded",
                "process": None,
                "pid": None,
                "impact_value": 60.0,
                "impact_unit": "percent",
                "confidence": "low",
                "source": "cli",
            },
        ]

        mock_engine = MagicMock()
        mock_engine.recommendations = AsyncMock(return_value=cli_recs)

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", mock_engine):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert data["_meta"]["mode"] == "app"
        assert data["_meta"]["partial"] is True
        assert data["_meta"]["source"] == "cli"
        assert data["_meta"]["count"] == 2
        assert len(data["recommendations"]) == 2

    def test_app_mode_snapshot_types_only(self):
        """App mode should only contain snapshot types (no trend-based)."""
        # Snapshot types: compressor_low_ratio, high_growth_process, rosetta_detected,
        # agent_memory_pressure, swap_pressure
        snapshot_types = {"compressor_low_ratio", "high_growth_process",
                          "rosetta_detected", "agent_memory_pressure", "swap_pressure"}
        trend_types = {"exhaustion_imminent", "compressor_degrading"}

        cli_recs = [
            {"type": "compressor_low_ratio", "message": "test", "process": None,
             "pid": None, "impact_value": 1.5, "impact_unit": "ratio",
             "confidence": "low", "source": "cli"},
            {"type": "high_growth_process", "message": "test", "process": "node",
             "pid": 123, "impact_value": 2.0, "impact_unit": "GB",
             "confidence": "low", "source": "cli"},
        ]

        mock_engine = MagicMock()
        mock_engine.recommendations = AsyncMock(return_value=cli_recs)

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", mock_engine):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        types_returned = {r["type"] for r in data["recommendations"]}
        # All returned types should be snapshot types
        assert types_returned.issubset(snapshot_types)
        # No trend types should be present
        assert types_returned.isdisjoint(trend_types)

    def test_app_mode_filters_trend_types_from_cli(self):
        """App mode must reject trend-based types even if CLI emits them."""
        cli_recs = [
            {"type": "compressor_low_ratio", "message": "ok", "process": None,
             "pid": None, "impact_value": 1.5, "impact_unit": "ratio",
             "confidence": "low", "source": "cli"},
            {"type": "compressor_degrading", "message": "trend", "process": None,
             "pid": None, "impact_value": 1.8, "impact_unit": "ratio",
             "confidence": "high", "source": "cli"},
            {"type": "exhaustion_imminent", "message": "trend", "process": None,
             "pid": None, "impact_value": 300, "impact_unit": "seconds",
             "confidence": "high", "source": "cli"},
        ]

        mock_engine = MagicMock()
        mock_engine.recommendations = AsyncMock(return_value=cli_recs)

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", mock_engine):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        types = [r["type"] for r in data["recommendations"]]
        assert "compressor_low_ratio" in types
        assert "compressor_degrading" not in types
        assert "exhaustion_imminent" not in types
        assert data["_meta"]["count"] == 1

    def test_app_mode_non_list_falls_through_to_standalone(self):
        """App mode with non-list CLI response falls through to standalone."""
        mock_engine = MagicMock()
        mock_engine.recommendations = AsyncMock(return_value={"error": "unexpected"})

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", mock_engine), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=3.0), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert data["_meta"]["mode"] == "standalone"
        assert data["_meta"]["source"] == "standalone"

    def test_app_mode_falls_back_to_standalone_on_error(self):
        """When CLI fails, falls back to standalone mode."""
        mock_engine = MagicMock()
        mock_engine.recommendations = AsyncMock(side_effect=RuntimeError("CLI timeout"))

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", mock_engine), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=3.0), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert data["_meta"]["mode"] == "standalone"


# ── Socket Fallback Tests ────────────────────────────────────────────

class TestRecommendationsSocketFallback:
    """Socket failure cascades correctly to app/standalone."""

    def test_socket_failure_falls_through_to_app_via_lazy_init(self):
        """When socket fails and _APP_ENGINE is None, lazily discovers binary for app mode."""
        cli_recs = [
            {"type": "swap_pressure", "message": "test", "process": None,
             "pid": None, "impact_value": 75.0, "impact_unit": "percent",
             "confidence": "low", "source": "cli"},
        ]

        mock_engine = MagicMock()
        mock_engine.recommendations = AsyncMock(return_value=cli_recs)

        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server.socket_recommendations", new_callable=AsyncMock, return_value=None), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value="/fake/cacheout"), \
             patch("cacheout_mcp.server.AppEngine", return_value=mock_engine):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert data["_meta"]["mode"] == "app"
        assert data["_meta"]["source"] == "cli"
        assert len(data["recommendations"]) == 1

    def test_socket_malformed_recommendations_falls_through(self):
        """Socket mode with non-list recommendations falls through to app/standalone."""
        daemon_response = {
            "recommendations": "not_a_list",
            "_meta": {"count": 0, "source": "daemon", "scan_partial": False},
        }

        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server.socket_recommendations", new_callable=AsyncMock, return_value=daemon_response), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value=None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=3.0), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        # Fell through to standalone since socket payload was malformed and no binary found
        assert data["_meta"]["mode"] == "standalone"

    def test_socket_non_dict_payload_falls_through(self):
        """Socket returning a non-dict top-level (e.g. list) falls through gracefully."""
        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server.socket_recommendations", new_callable=AsyncMock, return_value=["not", "a", "dict"]), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value=None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=3.0), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert data["_meta"]["mode"] == "standalone"

    def test_socket_non_dict_meta_degrades_gracefully(self):
        """Socket returning non-dict _meta still works (scan_partial defaults to False)."""
        daemon_response = {
            "recommendations": [
                {"type": "swap_pressure", "message": "test", "process": None,
                 "pid": None, "impact_value": 65.0, "impact_unit": "percent",
                 "confidence": "high", "source": "daemon"},
            ],
            "_meta": "bad_meta",
        }

        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server.socket_recommendations", new_callable=AsyncMock, return_value=daemon_response):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert data["_meta"]["mode"] == "socket"
        # Non-dict _meta is replaced with {}, so scan_partial defaults to False
        assert data["_meta"]["partial"] is False
        assert len(data["recommendations"]) == 1

    def test_standalone_mode_does_not_lazy_resolve_binary(self):
        """When _MODE is standalone, lazy app-engine resolution must NOT happen."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value="/fake/cacheout") as mock_find, \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=1.5), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        # Must remain standalone, not switch to app
        assert data["_meta"]["mode"] == "standalone"
        assert data["_meta"]["source"] == "standalone"
        # _find_cacheout_binary should NOT have been called
        mock_find.assert_not_called()


# ── Standalone Mode Tests ────────────────────────────────────────────

class TestRecommendationsStandaloneMode:
    """Standalone mode: basic sysctl-based recommendations."""

    def test_standalone_compressor_low_ratio(self):
        """Standalone detects compressor_low_ratio when ratio < 2.0."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=1.5), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert data["_meta"]["mode"] == "standalone"
        assert data["_meta"]["partial"] is True
        assert data["_meta"]["source"] == "standalone"

        types = [r["type"] for r in data["recommendations"]]
        assert "compressor_low_ratio" in types

        rec = next(r for r in data["recommendations"] if r["type"] == "compressor_low_ratio")
        assert rec["confidence"] == "low"
        assert rec["source"] == "standalone"
        assert rec["impact_value"] == 1.5
        assert rec["impact_unit"] == "ratio"

    def test_standalone_uses_compressor_low_ratio_not_degrading(self):
        """Standalone MUST use compressor_low_ratio, NOT compressor_degrading."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=1.2), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        types = [r["type"] for r in data["recommendations"]]
        assert "compressor_low_ratio" in types
        assert "compressor_degrading" not in types

    def test_standalone_swap_pressure(self):
        """Standalone detects swap_pressure when swap used > 50% of physical RAM."""
        physical_mem = int(16.0 * 1024**3)  # 16 GB physical RAM
        swap_used = 10.0 * 1024**3          # 10 GB used = 62.5% of physical RAM

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=3.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=physical_mem), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=swap_used):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        types = [r["type"] for r in data["recommendations"]]
        assert "swap_pressure" in types

        rec = next(r for r in data["recommendations"] if r["type"] == "swap_pressure")
        assert rec["confidence"] == "low"
        assert rec["source"] == "standalone"
        assert rec["impact_unit"] == "percent"
        # 10 GB used / 16 GB physical = 62.5%
        assert rec["impact_value"] == 62.5

    def test_standalone_no_recommendations_when_healthy(self):
        """Standalone returns empty array when everything is healthy."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=3.5), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=1.0 * 1024**3):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert data["recommendations"] == []
        assert data["_meta"]["count"] == 0
        assert data["_meta"]["partial"] is True

    def test_standalone_both_recommendations(self):
        """Standalone can return both compressor_low_ratio and swap_pressure."""
        physical_mem = int(16.0 * 1024**3)  # 16 GB physical RAM
        swap_used = 10.0 * 1024**3  # 62.5% of physical RAM

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=1.3), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=physical_mem), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=swap_used):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        types = [r["type"] for r in data["recommendations"]]
        assert "compressor_low_ratio" in types
        assert "swap_pressure" in types
        assert data["_meta"]["count"] == 2

    def test_standalone_only_compressor_low_ratio_and_swap_pressure(self):
        """Standalone can ONLY produce compressor_low_ratio and swap_pressure types."""
        # Even with bad conditions, standalone never produces process-level types
        physical_mem = int(8.0 * 1024**3)  # 8 GB physical RAM
        swap_used = 6.0 * 1024**3  # 75% of physical RAM

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=0.8), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=physical_mem), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=swap_used):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        allowed = {"compressor_low_ratio", "swap_pressure"}
        for rec in data["recommendations"]:
            assert rec["type"] in allowed, f"Standalone produced disallowed type: {rec['type']}"

    def test_standalone_skips_ratio_zero(self):
        """Standalone skips compressor_low_ratio when ratio is 0.0 (nothing compressed)."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        types = [r["type"] for r in data["recommendations"]]
        assert "compressor_low_ratio" not in types

    def test_standalone_skips_ratio_none(self):
        """Standalone skips compressor_low_ratio when ratio is unavailable."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=None), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        types = [r["type"] for r in data["recommendations"]]
        assert "compressor_low_ratio" not in types


# ── Canonical Schema Tests ───────────────────────────────────────────

class TestRecommendationSchema:
    """Verify all recommendations follow the canonical schema."""

    def test_recommendation_has_all_required_fields(self):
        """Every recommendation must have type, message, process, pid,
        impact_value, impact_unit, confidence, source."""
        required = {"type", "message", "process", "pid",
                    "impact_value", "impact_unit", "confidence", "source"}

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=1.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(8.0 * 1024**3)), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=6.0 * 1024**3):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert len(data["recommendations"]) > 0
        for rec in data["recommendations"]:
            assert required.issubset(rec.keys()), f"Missing fields: {required - rec.keys()}"

    def test_meta_has_all_required_fields(self):
        """_meta must have mode, count, partial, source."""
        required_meta = {"mode", "count", "partial", "source"}

        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=3.0), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert required_meta.issubset(data["_meta"].keys())

    def test_response_envelope_structure(self):
        """Top-level must be {recommendations: [...], _meta: {...}}."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._socket_connectable", return_value=False), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.parse_sysctl_compressor_ratio", new_callable=AsyncMock, return_value=3.0), \
             patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0.0), \
             patch("cacheout_mcp.server._async_sysctl_int", new_callable=AsyncMock, return_value=int(16.0 * 1024**3)):
            from cacheout_mcp.server import cacheout_get_recommendations
            result = run_async(cacheout_get_recommendations())

        data = json.loads(result)
        assert "recommendations" in data
        assert "_meta" in data
        assert isinstance(data["recommendations"], list)
        assert isinstance(data["_meta"], dict)


# ── Engine Socket Recommendations Tests ──────────────────────────────

class TestEngineSocketRecommendations:
    """Test engine.socket_recommendations() wrapper."""

    def test_socket_recommendations_calls_socket_command(self):
        with patch("cacheout_mcp.engine._socket_command", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.return_value = {"recommendations": [], "_meta": {"count": 0}}
            from cacheout_mcp.engine import socket_recommendations
            result = run_async(socket_recommendations())

        mock_cmd.assert_called_once_with("recommendations", timeout=5.0)
        assert result is not None
        assert result["recommendations"] == []

    def test_socket_recommendations_returns_none_on_failure(self):
        with patch("cacheout_mcp.engine._socket_command", new_callable=AsyncMock, return_value=None):
            from cacheout_mcp.engine import socket_recommendations
            result = run_async(socket_recommendations())

        assert result is None


# ── AppEngine.recommendations Tests ──────────────────────────────────

class TestAppEngineRecommendations:
    """Test AppEngine.recommendations() method."""

    def test_app_engine_recommendations_delegates_to_cli(self):
        from cacheout_mcp.engine import AppEngine

        engine = AppEngine("/fake/cacheout")
        with patch.object(engine, "_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = [{"type": "compressor_low_ratio"}]
            result = run_async(engine.recommendations())

        mock_run.assert_called_once_with("recommendations")
        assert isinstance(result, list)
        assert result[0]["type"] == "compressor_low_ratio"
