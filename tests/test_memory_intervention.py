"""Tests for cacheout_memory_intervention: confirm gating, capability reporting, name validation, envelope shape."""

from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch

from cacheout_mcp.memory_tools import (
    CANONICAL_INTERVENTIONS,
    _intervention_capabilities,
    _intervention_envelope,
    run_standalone_intervention,
    run_app_intervention,
    _STANDALONE_PURGE_TIMEOUT,
    _APP_PURGE_TIMEOUT,
)
from cacheout_mcp.memory_models import MemoryInterventionInput


# ── Envelope shape tests ─────────────────────────────────────────────

class TestEnvelopeShape:
    def test_envelope_has_required_keys(self):
        env = _intervention_envelope("standalone", {"test": True})
        assert set(env.keys()) == {"mode", "capabilities", "data", "partial"}

    def test_envelope_mode(self):
        env = _intervention_envelope("app", {"test": True})
        assert env["mode"] == "app"

    def test_envelope_partial_default_false(self):
        env = _intervention_envelope("standalone", {})
        assert env["partial"] is False

    def test_envelope_partial_true(self):
        env = _intervention_envelope("standalone", {}, partial=True)
        assert env["partial"] is True


# ── Capabilities tests ───────────────────────────────────────────────

class TestCapabilities:
    def test_standalone_only_purge_available(self):
        caps = _intervention_capabilities("standalone")
        assert caps["purge"] is True
        assert caps["trigger_pressure_warn"] is False
        assert caps["reduce_transparency"] is False
        assert caps["delete_sleepimage"] is False
        assert caps["cleanup_snapshots"] is False
        assert caps["flush_compositor"] is False

    def test_app_only_purge_available(self):
        caps = _intervention_capabilities("app")
        assert caps["purge"] is True
        assert caps["trigger_pressure_warn"] is False
        assert caps["reduce_transparency"] is False

    def test_capabilities_list_all_canonical(self):
        """All canonical intervention names must appear in capabilities."""
        caps = _intervention_capabilities("standalone")
        for name in CANONICAL_INTERVENTIONS:
            assert name in caps, f"Missing canonical intervention: {name}"


# ── Name validation tests ────────────────────────────────────────────

class TestNameValidation:
    def test_unknown_name_standalone(self):
        result = asyncio.run(run_standalone_intervention("bogus_name", False))
        assert result["data"]["error"] == "unknown_intervention"
        assert "bogus_name" in result["data"]["note"]
        assert CANONICAL_INTERVENTIONS == result["data"]["available"]

    def test_unknown_name_app(self):
        mock_engine = type("E", (), {"binary": "/usr/bin/fake"})()
        result = asyncio.run(run_app_intervention(mock_engine, "bogus_name", False))
        assert result["data"]["error"] == "unknown_intervention"
        assert result["mode"] == "app"

    def test_canonical_names_list(self):
        assert "purge" in CANONICAL_INTERVENTIONS
        assert "trigger_pressure_warn" in CANONICAL_INTERVENTIONS
        assert "reduce_transparency" in CANONICAL_INTERVENTIONS
        assert "delete_sleepimage" in CANONICAL_INTERVENTIONS
        assert "cleanup_snapshots" in CANONICAL_INTERVENTIONS
        assert "flush_compositor" in CANONICAL_INTERVENTIONS
        assert len(CANONICAL_INTERVENTIONS) == 6


# ── Standalone confirm gating tests ──────────────────────────────────

class TestStandaloneConfirmGating:
    def test_dry_run_purge(self):
        result = asyncio.run(run_standalone_intervention("purge", confirm=False))
        assert result["mode"] == "standalone"
        assert result["data"]["dry_run"] is True
        assert result["data"]["intervention"] == "purge"
        assert result["data"]["estimated_reclaim_mb"] is None
        assert "estimate_note" in result["data"]
        assert result["data"]["description"] != ""

    def test_confirm_purge_success(self):
        with patch("cacheout_mcp.memory_tools._async_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ("", None)
            result = asyncio.run(run_standalone_intervention("purge", confirm=True))

        assert result["data"]["dry_run"] is False
        assert result["data"]["success"] is True
        assert result["data"]["intervention"] == "purge"
        assert "result" in result["data"]

        # Verify /usr/sbin/purge was called with correct timeout
        mock_run.assert_called_once_with(
            ["/usr/sbin/purge"], timeout=_STANDALONE_PURGE_TIMEOUT
        )

    def test_confirm_purge_failure(self):
        with patch("cacheout_mcp.memory_tools._async_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ("", "Command purge timed out after 35.0s")
            result = asyncio.run(run_standalone_intervention("purge", confirm=True))

        assert result["data"]["success"] is False
        assert "timed out" in result["data"]["error"]

    def test_non_purge_unsupported_standalone(self):
        for name in CANONICAL_INTERVENTIONS:
            if name == "purge":
                continue
            result = asyncio.run(run_standalone_intervention(name, confirm=False))
            assert result["data"]["error"] == "unsupported_in_standalone"
            assert result["data"]["available"] == ["purge"]


# ── App mode confirm gating tests ───────────────────────────────────

class TestAppConfirmGating:
    def _mock_engine(self):
        return type("E", (), {"binary": "/Applications/Cacheout.app/Contents/MacOS/Cacheout"})()

    def test_dry_run_purge(self):
        result = asyncio.run(run_app_intervention(self._mock_engine(), "purge", confirm=False))
        assert result["mode"] == "app"
        assert result["data"]["dry_run"] is True
        assert result["data"]["intervention"] == "purge"

    def test_confirm_purge_success(self):
        with patch("cacheout_mcp.memory_tools._async_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ('{"freed_mb": 512}', None)
            result = asyncio.run(run_app_intervention(self._mock_engine(), "purge", confirm=True))

        assert result["data"]["success"] is True
        assert result["data"]["result"]["freed_mb"] == 512

        # Verify --cli purge was called with correct timeout
        mock_run.assert_called_once_with(
            [self._mock_engine().binary, "--cli", "purge"],
            timeout=_APP_PURGE_TIMEOUT,
        )

    def test_confirm_purge_non_json_output(self):
        with patch("cacheout_mcp.memory_tools._async_run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ("Purge complete.", None)
            result = asyncio.run(run_app_intervention(self._mock_engine(), "purge", confirm=True))

        assert result["data"]["success"] is True
        assert result["data"]["result"]["cli_output"] == "Purge complete."

    def test_gated_interventions_app(self):
        for name in CANONICAL_INTERVENTIONS:
            if name == "purge":
                continue
            result = asyncio.run(run_app_intervention(self._mock_engine(), name, confirm=False))
            assert result["data"]["error"] == "unavailable"
            assert "not yet implemented" in result["data"]["note"]
            assert result["data"]["intervention"] == name

    def test_gated_interventions_confirm_also_gated(self):
        """Confirm=True for gated interventions should also return gated error, not execute."""
        result = asyncio.run(
            run_app_intervention(self._mock_engine(), "flush_compositor", confirm=True)
        )
        assert result["data"]["error"] == "unavailable"


# ── Input model tests ────────────────────────────────────────────────

class TestMemoryInterventionInput:
    def test_valid_input(self):
        inp = MemoryInterventionInput(intervention_name="purge", confirm=False)
        assert inp.intervention_name == "purge"
        assert inp.confirm is False
        assert inp.target_pid is None

    def test_confirm_default_false(self):
        inp = MemoryInterventionInput(intervention_name="purge")
        assert inp.confirm is False

    def test_with_target_pid(self):
        inp = MemoryInterventionInput(intervention_name="purge", target_pid=1234)
        assert inp.target_pid == 1234

    def test_rejects_extra_fields(self):
        with pytest.raises(Exception):
            MemoryInterventionInput(
                intervention_name="purge", confirm=False, unknown_field="bad"
            )


# ── Timeout constants tests ──────────────────────────────────────────

class TestTimeoutConstants:
    def test_standalone_purge_timeout(self):
        assert _STANDALONE_PURGE_TIMEOUT == 35.0

    def test_app_purge_timeout(self):
        assert _APP_PURGE_TIMEOUT == 40.0
