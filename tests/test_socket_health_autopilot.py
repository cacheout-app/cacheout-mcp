"""Tests for socket mode, system health tool, autopilot config validator, and watchdog."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cacheout_mcp.engine import (
    _get_state_dir,
    _get_socket_path,
    _socket_connectable,
)
from cacheout_mcp.server import (
    _health_score,
    _pressure_tier_from,
    _validate_autopilot_config,
)


# ── Health Score Tests ───────────────────────────────────────────────

class TestHealthScore:
    """Verify canonical health score formula matches Swift HealthScore.compute."""

    def test_normal_pressure_no_swap_good_ratio(self):
        score = _health_score("normal", 0.0, 3.0)
        assert score == 100

    def test_warn_pressure(self):
        score = _health_score("warn", 0.0, 3.0)
        assert score == 75

    def test_warning_alias(self):
        """'warning' should be treated same as 'warn'."""
        assert _health_score("warning", 0.0, 3.0) == _health_score("warn", 0.0, 3.0)

    def test_critical_pressure(self):
        score = _health_score("critical", 0.0, 3.0)
        assert score == 50

    def test_swap_penalty(self):
        # 50% swap -> penalty = min(50, int(50/2)) = 25
        score = _health_score("normal", 50.0, 3.0)
        assert score == 75

    def test_swap_penalty_capped_at_50(self):
        score = _health_score("normal", 200.0, 3.0)
        assert score == 50  # 100 - min(50, 100) = 50

    def test_compressor_penalty(self):
        # ratio=2.0 -> penalty = min(30, max(0, int((3.0-2.0)*10))) = 10
        score = _health_score("normal", 0.0, 2.0)
        assert score == 90

    def test_compressor_penalty_low_ratio(self):
        # ratio=0.0 -> penalty = min(30, max(0, int(30))) = 30
        score = _health_score("normal", 0.0, 0.0)
        assert score == 70

    def test_compressor_penalty_high_ratio(self):
        # ratio=5.0 -> penalty = min(30, max(0, int((3.0-5.0)*10))) = max(0, -20) = 0
        score = _health_score("normal", 0.0, 5.0)
        assert score == 100

    def test_all_penalties_combined(self):
        # critical(-50), swap 60%(-30), ratio 1.0(-20)
        score = _health_score("critical", 60.0, 1.0)
        assert score == 0  # max(0, 50 - 30 - 20)

    def test_score_never_negative(self):
        score = _health_score("critical", 100.0, 0.0)
        assert score == 0

    def test_no_data_sentinel_is_minus_one(self):
        """Callers should use -1 for no-data; the function itself always returns >= 0."""
        score = _health_score("normal", 0.0, 3.0)
        assert score >= 0

    def test_int_type(self):
        score = _health_score("normal", 33.3, 2.5)
        assert isinstance(score, int)

    def test_elevated_tier_no_penalty(self):
        """'elevated' is not warn/critical, so no base penalty."""
        score = _health_score("elevated", 0.0, 3.0)
        assert score == 100


class TestHealthToolSwapUnavailable:
    """When swap data cannot be read, standalone health must return score=-1."""

    def test_swap_unavailable_returns_negative_one(self):
        from cacheout_mcp.server import cacheout_system_health

        async def _run():
            with patch("cacheout_mcp.server._MODE", "standalone"), \
                 patch("cacheout_mcp.server._APP_ENGINE", None), \
                 patch("cacheout_mcp.server._socket_connectable", return_value=False), \
                 patch("cacheout_mcp.server.get_standalone_memory_stats", new_callable=AsyncMock) as mock_mem, \
                 patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=None), \
                 patch("cacheout_mcp.server._async_parse_swap_total", new_callable=AsyncMock, return_value=None):
                mock_mem.return_value = {
                    "pressure_level": 1,
                    "estimated_available_mb": 5000.0,
                    "compressor_ratio": 2.5,
                }
                input_obj = MagicMock()
                result_json = await cacheout_system_health(input_obj)
                result = json.loads(result_json)
                assert result["score"] == -1
                assert result["_meta"]["partial"] is True

        asyncio.run(_run())

    def test_swap_total_zero_returns_negative_one(self):
        from cacheout_mcp.server import cacheout_system_health

        async def _run():
            with patch("cacheout_mcp.server._MODE", "standalone"), \
                 patch("cacheout_mcp.server._APP_ENGINE", None), \
                 patch("cacheout_mcp.server._socket_connectable", return_value=False), \
                 patch("cacheout_mcp.server.get_standalone_memory_stats", new_callable=AsyncMock) as mock_mem, \
                 patch("cacheout_mcp.server._async_parse_swap_used", new_callable=AsyncMock, return_value=0), \
                 patch("cacheout_mcp.server._async_parse_swap_total", new_callable=AsyncMock, return_value=0):
                mock_mem.return_value = {
                    "pressure_level": 1,
                    "estimated_available_mb": 5000.0,
                    "compressor_ratio": 2.5,
                }
                input_obj = MagicMock()
                result_json = await cacheout_system_health(input_obj)
                result = json.loads(result_json)
                assert result["score"] == -1
                assert result["_meta"]["partial"] is True

        asyncio.run(_run())


# ── Pressure Tier Tests ──────────────────────────────────────────────

class TestPressureTierFrom:
    """Verify _pressure_tier_from matches Swift PressureTier.from exactly."""

    def test_normal(self):
        assert _pressure_tier_from(0, 8000.0) == "normal"

    def test_elevated_by_pressure(self):
        assert _pressure_tier_from(1, 8000.0) == "elevated"

    def test_elevated_by_available_mb(self):
        assert _pressure_tier_from(0, 3000.0) == "elevated"

    def test_warning_by_pressure(self):
        assert _pressure_tier_from(2, 8000.0) == "warning"

    def test_warning_by_available_mb(self):
        assert _pressure_tier_from(0, 1000.0) == "warning"

    def test_critical_by_pressure(self):
        assert _pressure_tier_from(4, 8000.0) == "critical"

    def test_critical_by_available_mb(self):
        assert _pressure_tier_from(0, 400.0) == "critical"

    def test_critical_overrides_all(self):
        """Pressure level 4 is always critical regardless of available MB."""
        assert _pressure_tier_from(4, 50000.0) == "critical"

    def test_boundary_512mb(self):
        assert _pressure_tier_from(0, 511.9) == "critical"
        assert _pressure_tier_from(0, 512.0) == "warning"

    def test_boundary_1500mb(self):
        assert _pressure_tier_from(0, 1499.9) == "warning"
        assert _pressure_tier_from(0, 1500.0) == "elevated"

    def test_boundary_4000mb(self):
        assert _pressure_tier_from(0, 3999.9) == "elevated"
        assert _pressure_tier_from(0, 4000.0) == "normal"


# ── Autopilot Config Validator Tests ─────────────────────────────────

class TestAutopilotValidator:
    """Verify local validator matches daemon's shared AutopilotConfigValidator."""

    def test_valid_minimal_config(self):
        config = {"version": 1, "enabled": True}
        errors = _validate_autopilot_config(config)
        assert errors == []

    def test_valid_full_config(self):
        config = {
            "version": 1,
            "enabled": True,
            "rules": [
                {
                    "condition": {
                        "pressure_tier": "warn",
                        "consecutive_samples": 60,
                        "compression_ratio_below": 3.0,
                        "compression_ratio_window": 10,
                    },
                    "action": "pressure-trigger",
                }
            ],
            "webhook": {
                "url": "https://example.com/hook",
                "format": "generic",
                "timeout_s": 10,
            },
            "telegram": {
                "bot_token": "123:abc",
                "chat_id": "-100123",
                "timeout_s": 10,
            },
        }
        errors = _validate_autopilot_config(config)
        assert errors == []

    def test_missing_version(self):
        errors = _validate_autopilot_config({"enabled": True})
        assert any("version" in e for e in errors)

    def test_wrong_version(self):
        errors = _validate_autopilot_config({"version": 2, "enabled": True})
        assert any("Unsupported version" in e for e in errors)

    def test_non_integer_version(self):
        errors = _validate_autopilot_config({"version": "1", "enabled": True})
        assert any("non-integer" in e for e in errors)

    def test_missing_enabled(self):
        errors = _validate_autopilot_config({"version": 1})
        assert any("enabled" in e for e in errors)

    def test_non_boolean_enabled(self):
        errors = _validate_autopilot_config({"version": 1, "enabled": "yes"})
        assert any("non-boolean" in e for e in errors)

    def test_unsupported_action(self):
        config = {
            "version": 1,
            "enabled": True,
            "rules": [
                {
                    "condition": {"pressure_tier": "warn"},
                    "action": "delete-sleepimage",
                }
            ],
        }
        errors = _validate_autopilot_config(config)
        assert any("unsupported action" in e for e in errors)

    def test_valid_actions(self):
        for action in ("pressure-trigger", "reduce-transparency"):
            config = {
                "version": 1,
                "enabled": True,
                "rules": [
                    {
                        "condition": {"pressure_tier": "warn"},
                        "action": action,
                    }
                ],
            }
            errors = _validate_autopilot_config(config)
            assert errors == [], f"Action '{action}' should be valid but got: {errors}"

    def test_invalid_pressure_tier(self):
        config = {
            "version": 1,
            "enabled": True,
            "rules": [
                {
                    "condition": {"pressure_tier": "extreme"},
                    "action": "pressure-trigger",
                }
            ],
        }
        errors = _validate_autopilot_config(config)
        assert any("invalid pressure_tier" in e for e in errors)

    def test_missing_condition(self):
        config = {
            "version": 1,
            "enabled": True,
            "rules": [{"action": "pressure-trigger"}],
        }
        errors = _validate_autopilot_config(config)
        assert any("missing 'condition'" in e for e in errors)

    def test_rules_not_array(self):
        config = {"version": 1, "enabled": True, "rules": "not-an-array"}
        errors = _validate_autopilot_config(config)
        assert any("must be an array" in e for e in errors)

    def test_rules_mixed_array_rejected(self):
        """Array with non-object elements must be rejected as a whole,
        matching Swift's top-level [[String: Any]] cast behavior."""
        config = {
            "version": 1,
            "enabled": True,
            "rules": [
                {"action": "pressure-trigger", "condition": {"pressure_tier": "warn"}},
                "bad",
            ],
        }
        errors = _validate_autopilot_config(config)
        assert len(errors) == 1
        assert "must be an array of objects" in errors[0]

    def test_webhook_missing_url(self):
        config = {
            "version": 1,
            "enabled": True,
            "webhook": {"format": "generic", "timeout_s": 10},
        }
        errors = _validate_autopilot_config(config)
        assert any("webhook" in e and "url" in e for e in errors)

    def test_webhook_wrong_format(self):
        config = {
            "version": 1,
            "enabled": True,
            "webhook": {
                "url": "https://example.com",
                "format": "slack",
                "timeout_s": 10,
            },
        }
        errors = _validate_autopilot_config(config)
        assert any("unsupported format" in e for e in errors)

    def test_webhook_timeout_out_of_range(self):
        for bad_timeout in (0, 61):
            config = {
                "version": 1,
                "enabled": True,
                "webhook": {
                    "url": "https://example.com",
                    "format": "generic",
                    "timeout_s": bad_timeout,
                },
            }
            errors = _validate_autopilot_config(config)
            assert any("timeout_s" in e for e in errors), (
                f"timeout_s={bad_timeout} should fail"
            )

    def test_telegram_missing_bot_token(self):
        config = {
            "version": 1,
            "enabled": True,
            "telegram": {"chat_id": "123", "timeout_s": 10},
        }
        errors = _validate_autopilot_config(config)
        assert any("bot_token" in e for e in errors)

    def test_telegram_missing_chat_id(self):
        config = {
            "version": 1,
            "enabled": True,
            "telegram": {"bot_token": "123:abc", "timeout_s": 10},
        }
        errors = _validate_autopilot_config(config)
        assert any("chat_id" in e for e in errors)

    def test_telegram_timeout_out_of_range(self):
        config = {
            "version": 1,
            "enabled": True,
            "telegram": {"bot_token": "t", "chat_id": "c", "timeout_s": 0},
        }
        errors = _validate_autopilot_config(config)
        assert any("timeout_s" in e for e in errors)

    def test_consecutive_samples_must_be_positive(self):
        config = {
            "version": 1,
            "enabled": True,
            "rules": [
                {
                    "condition": {
                        "pressure_tier": "warn",
                        "consecutive_samples": 0,
                    },
                    "action": "pressure-trigger",
                }
            ],
        }
        errors = _validate_autopilot_config(config)
        assert any("consecutive_samples" in e for e in errors)

    def test_compression_ratio_below_must_be_positive(self):
        config = {
            "version": 1,
            "enabled": True,
            "rules": [
                {
                    "condition": {
                        "pressure_tier": "warn",
                        "compression_ratio_below": -1.0,
                    },
                    "action": "pressure-trigger",
                }
            ],
        }
        errors = _validate_autopilot_config(config)
        assert any("compression_ratio_below" in e for e in errors)

    def test_webhook_non_http_url(self):
        config = {
            "version": 1,
            "enabled": True,
            "webhook": {
                "url": "ftp://example.com",
                "format": "generic",
                "timeout_s": 10,
            },
        }
        errors = _validate_autopilot_config(config)
        assert any("http" in e.lower() for e in errors)

    def test_webhook_url_no_host(self):
        """URL like 'https://' or 'https:///path' must be rejected."""
        for bad_url in ("https://", "https:///path"):
            config = {
                "version": 1,
                "enabled": True,
                "webhook": {
                    "url": bad_url,
                    "format": "generic",
                    "timeout_s": 10,
                },
            }
            errors = _validate_autopilot_config(config)
            assert len(errors) > 0, f"URL '{bad_url}' should fail validation"
            assert any("host" in e or "url" in e for e in errors), (
                f"URL '{bad_url}' errors should mention host/url: {errors}"
            )

    def test_webhook_malformed_bracketed_ipv6(self):
        """Malformed bracketed IPv6 like 'https://[::1' must not raise, should return error."""
        for bad_url in ("https://[::1", "https://["):
            config = {
                "version": 1,
                "enabled": True,
                "webhook": {
                    "url": bad_url,
                    "format": "generic",
                    "timeout_s": 10,
                },
            }
            errors = _validate_autopilot_config(config)
            assert len(errors) > 0, f"URL '{bad_url}' should fail validation"
            assert any("url" in e.lower() for e in errors), (
                f"URL '{bad_url}' errors should mention url: {errors}"
            )

    def test_webhook_bad_scheme_and_no_host(self):
        """URL with bad scheme AND no host must emit both errors independently."""
        config = {
            "version": 1,
            "enabled": True,
            "webhook": {
                "url": "ftp:///hook",
                "format": "generic",
                "timeout_s": 10,
            },
        }
        errors = _validate_autopilot_config(config)
        scheme_errors = [e for e in errors if "scheme" in e.lower() or "http" in e.lower()]
        host_errors = [e for e in errors if "host" in e.lower()]
        assert len(scheme_errors) > 0, f"Should report scheme error: {errors}"
        assert len(host_errors) > 0, f"Should report host error independently: {errors}"

    def test_elevated_pressure_tier_accepted(self):
        """'elevated' must be a valid pressure_tier value."""
        config = {
            "version": 1,
            "enabled": True,
            "rules": [
                {
                    "condition": {"pressure_tier": "elevated"},
                    "action": "pressure-trigger",
                }
            ],
        }
        errors = _validate_autopilot_config(config)
        assert errors == [], f"elevated should be valid but got: {errors}"

    def test_all_swift_pressure_tiers_accepted(self):
        """All tiers from Swift PressureTier.validConfigValues must be accepted."""
        for tier in ("normal", "elevated", "warn", "warning", "critical"):
            config = {
                "version": 1,
                "enabled": True,
                "rules": [
                    {
                        "condition": {"pressure_tier": tier},
                        "action": "pressure-trigger",
                    }
                ],
            }
            errors = _validate_autopilot_config(config)
            assert errors == [], f"Tier '{tier}' should be valid but got: {errors}"


class TestAutopilotValidatorBoolGuard:
    """Regression: Python bool is subclass of int; bools must be rejected
    in integer/numeric fields to avoid schema-invalid configs."""

    def test_version_rejects_bool(self):
        errors = _validate_autopilot_config({"version": True, "enabled": True})
        assert any("version" in e for e in errors)

    def test_consecutive_samples_rejects_bool(self):
        config = {
            "version": 1, "enabled": True,
            "rules": [{
                "action": "pressure-trigger",
                "condition": {"pressure_tier": "warning", "consecutive_samples": False},
            }],
        }
        errors = _validate_autopilot_config(config)
        assert any("consecutive_samples" in e for e in errors)

    def test_compression_ratio_window_rejects_bool(self):
        config = {
            "version": 1, "enabled": True,
            "rules": [{
                "action": "pressure-trigger",
                "condition": {"pressure_tier": "warning", "compression_ratio_window": True},
            }],
        }
        errors = _validate_autopilot_config(config)
        assert any("compression_ratio_window" in e for e in errors)

    def test_compression_ratio_below_rejects_bool(self):
        config = {
            "version": 1, "enabled": True,
            "rules": [{
                "action": "pressure-trigger",
                "condition": {"pressure_tier": "warning", "compression_ratio_below": True},
            }],
        }
        errors = _validate_autopilot_config(config)
        assert any("compression_ratio_below" in e for e in errors)

    def test_webhook_timeout_rejects_bool(self):
        config = {
            "version": 1, "enabled": True,
            "webhook": {
                "url": "https://example.com/hook",
                "format": "generic",
                "timeout_s": True,
            },
        }
        errors = _validate_autopilot_config(config)
        assert any("timeout_s" in e for e in errors)

    def test_telegram_timeout_rejects_bool(self):
        config = {
            "version": 1, "enabled": True,
            "telegram": {
                "bot_token": "abc", "chat_id": "123",
                "timeout_s": False,
            },
        }
        errors = _validate_autopilot_config(config)
        assert any("timeout_s" in e for e in errors)


# ── State Dir / Socket Path Tests ────────────────────────────────────

class TestStateDirConfig:
    def test_default_state_dir(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove CACHEOUT_STATE_DIR if present
            os.environ.pop("CACHEOUT_STATE_DIR", None)
            d = _get_state_dir()
            assert d.endswith(".cacheout")

    def test_custom_state_dir(self):
        with patch.dict(os.environ, {"CACHEOUT_STATE_DIR": "/tmp/test-cacheout"}):
            d = _get_state_dir()
            assert d == "/tmp/test-cacheout"

    def test_socket_path_under_state_dir(self):
        with patch.dict(os.environ, {"CACHEOUT_STATE_DIR": "/tmp/test-cacheout"}):
            p = _get_socket_path()
            assert p == "/tmp/test-cacheout/status.sock"


# ── Socket Connectable Tests ────────────────────────────────────────

class TestSocketConnectable:
    def test_nonexistent_socket(self):
        assert _socket_connectable("/tmp/nonexistent-cacheout-test.sock") is False

    def test_connectable_with_timeout(self):
        """Non-existent path should return False quickly."""
        import time
        start = time.monotonic()
        result = _socket_connectable("/tmp/no-such-socket.sock", timeout=0.1)
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed < 1.0  # Should not wait full 2s default


# ── Configure Autopilot Flow Tests ──────────────────────────────────

class TestConfigureAutopilotFlow:
    """Test the configure autopilot tool's locking, baseline, and success semantics."""

    def test_baseline_failure_prevents_active_true(self):
        """When config_status baseline read fails, result must not claim active=true."""
        from cacheout_mcp.server import _configure_autopilot_locked

        async def _run():
            state_dir = Path(tempfile.mkdtemp(prefix="cacheout-test-"))
            config_path = state_dir / "autopilot.json"
            config = {"version": 1, "enabled": True}
            config_json = json.dumps(config)

            with patch("cacheout_mcp.server._socket_connectable", return_value=True), \
                 patch("cacheout_mcp.server._socket_command", new_callable=AsyncMock) as mock_cmd, \
                 patch("cacheout_mcp.server._get_socket_path", return_value="/tmp/fake.sock"):

                async def side_effect(cmd, params=None, timeout=2.0):
                    if cmd == "validate_config":
                        return {"valid": True, "errors": []}
                    return None  # config_status baseline fails

                mock_cmd.side_effect = side_effect

                result_json = await _configure_autopilot_locked(
                    state_dir, config_path, config, config_json
                )
                result = json.loads(result_json)

                assert result["active"] is False
                assert result["saved"] is True
                assert any("baseline" in w.lower() for w in result.get("warnings", []))

            import shutil
            shutil.rmtree(state_dir, ignore_errors=True)

        asyncio.run(_run())

    def test_configure_lock_is_asyncio_lock(self):
        """The _configure_lock must be an asyncio.Lock for async safety."""
        from cacheout_mcp.server import _configure_lock

        assert isinstance(_configure_lock, asyncio.Lock)

    def test_daemon_timeout_yields_failure(self):
        """When daemon is available but never increments generation, success=False."""
        from cacheout_mcp.server import _configure_autopilot_locked

        async def _run():
            state_dir = Path(tempfile.mkdtemp(prefix="cacheout-test-"))
            config_path = state_dir / "autopilot.json"
            config = {"version": 1, "enabled": False}
            config_json = json.dumps(config)

            pid_file = state_dir / "daemon.pid"
            pid_file.write_text(str(os.getpid()))

            with patch("cacheout_mcp.server._socket_connectable", return_value=True), \
                 patch("cacheout_mcp.server._socket_command", new_callable=AsyncMock) as mock_cmd, \
                 patch("cacheout_mcp.server._get_socket_path", return_value="/tmp/fake.sock"), \
                 patch("cacheout_mcp.server._validate_daemon_pid", new_callable=AsyncMock, return_value=True), \
                 patch("os.kill"):

                async def side_effect(cmd, params=None, timeout=2.0):
                    if cmd == "validate_config":
                        return {"valid": True, "errors": []}
                    if cmd == "config_status":
                        return {"generation": 5, "status": "ok"}
                    return None

                mock_cmd.side_effect = side_effect

                with patch("asyncio.sleep", new_callable=AsyncMock):
                    result_json = await _configure_autopilot_locked(
                        state_dir, config_path, config, config_json
                    )

                result = json.loads(result_json)
                assert result["success"] is False
                assert result["active"] is False
                assert result["reload"] == "timeout"

            import shutil
            shutil.rmtree(state_dir, ignore_errors=True)

        asyncio.run(_run())

    def test_stale_pid_skips_sighup(self):
        """When PID in pidfile is not our daemon, SIGHUP must be skipped."""
        from cacheout_mcp.server import _configure_autopilot_locked

        async def _run():
            state_dir = Path(tempfile.mkdtemp(prefix="cacheout-test-"))
            config_path = state_dir / "autopilot.json"
            config = {"version": 1, "enabled": True}
            config_json = json.dumps(config)

            pid_file = state_dir / "daemon.pid"
            pid_file.write_text("99999")  # Stale PID

            kill_called = False
            original_kill = os.kill

            def mock_kill(pid, sig):
                nonlocal kill_called
                kill_called = True

            with patch("cacheout_mcp.server._socket_connectable", return_value=True), \
                 patch("cacheout_mcp.server._socket_command", new_callable=AsyncMock) as mock_cmd, \
                 patch("cacheout_mcp.server._get_socket_path", return_value="/tmp/fake.sock"), \
                 patch("cacheout_mcp.server._validate_daemon_pid", new_callable=AsyncMock, return_value=False), \
                 patch("os.kill", side_effect=mock_kill):

                async def side_effect(cmd, params=None, timeout=2.0):
                    if cmd == "validate_config":
                        return {"valid": True, "errors": []}
                    if cmd == "config_status":
                        return {"generation": 5, "status": "ok"}
                    return None

                mock_cmd.side_effect = side_effect

                result_json = await _configure_autopilot_locked(
                    state_dir, config_path, config, config_json
                )
                result = json.loads(result_json)

                # SIGHUP must NOT have been sent
                assert kill_called is False
                # Must report saved but not active
                assert result["saved"] is True
                assert result["active"] is False
                # Must warn about stale PID
                assert any("not our daemon" in w for w in result.get("warnings", []))

            import shutil
            shutil.rmtree(state_dir, ignore_errors=True)

        asyncio.run(_run())


# ── Watchdog Script Behavior Tests ──────────────────────────────────

class TestWatchdogScript:
    """Test watchdog script logic by running individual functions via bash."""

    def test_watchdog_script_syntax(self):
        """Verify the watchdog script has valid bash syntax."""
        import subprocess
        result = subprocess.run(
            ["bash", "-n", str(Path(__file__).parent.parent / "config" / "cacheout-watchdog.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_watchdog_validate_pid_rejects_nonexistent(self):
        """validate_pid should reject a PID that doesn't exist."""
        import subprocess
        # Use a high PID that almost certainly doesn't exist
        script = '''
        source <(sed -n '/^validate_pid/,/^}/p' config/cacheout-watchdog.sh)
        BIN="/usr/bin/nonexistent"
        STATE_DIR="/tmp/nonexistent"
        validate_pid 999999
        echo "exit=$?"
        '''
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert "exit=1" in result.stdout

    def test_watchdog_validate_pid_rejects_wrong_binary(self):
        """validate_pid should reject a PID running a different binary."""
        import subprocess
        # Our own PID is running python, not Cacheout
        our_pid = os.getpid()
        script = f'''
        source <(sed -n '/^validate_pid/,/^}}/p' config/cacheout-watchdog.sh)
        BIN="/Applications/Cacheout.app/Contents/MacOS/Cacheout"
        STATE_DIR="/tmp/test"
        validate_pid {our_pid}
        echo "exit=$?"
        '''
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert "exit=1" in result.stdout

    def test_watchdog_requires_state_dir_flag(self):
        """validate_pid should require --state-dir token in the command line."""
        script_path = Path(__file__).parent.parent / "config" / "cacheout-watchdog.sh"
        content = script_path.read_text()
        # The script must use token-boundary matching for --state-dir
        assert '"--state-dir"' in content
        # Must support both --state-dir VALUE and --state-dir=VALUE forms
        assert '--state-dir=$STATE_DIR' in content

    def test_watchdog_token_matching(self):
        """validate_pid must use token matching, not substring matching."""
        script_path = Path(__file__).parent.parent / "config" / "cacheout-watchdog.sh"
        content = script_path.read_text()
        # Must NOT use substring matching patterns like *"$BIN"*
        # Instead should compare tokens exactly: "$token" == "$BIN"
        assert '"$token" == "$BIN"' in content
        assert '"$token" == "--daemon"' in content
        assert '"$token" == "$STATE_DIR"' in content

    def test_watchdog_socket_liveness(self):
        """Watchdog must include socket-based liveness probe."""
        script_path = Path(__file__).parent.parent / "config" / "cacheout-watchdog.sh"
        content = script_path.read_text()
        assert "check_socket_responsive" in content
        assert "SOCK_PATH" in content
        assert "LIVENESS_TIMEOUT" in content
        assert "STARTUP_GRACE" in content

    def test_watchdog_tristate_liveness(self):
        """Watchdog must use tri-state liveness: healthy, missing, unresponsive.
        Unresponsive state must stop the hung daemon before starting a replacement."""
        script_path = Path(__file__).parent.parent / "config" / "cacheout-watchdog.sh"
        content = script_path.read_text()
        assert "check_daemon_state" in content
        assert "DAEMON_STATE" in content
        # Must handle all three states
        assert '"healthy"' in content or "'healthy'" in content
        assert '"missing"' in content or "'missing'" in content
        assert '"unresponsive"' in content or "'unresponsive'" in content
        # Unresponsive path must stop before starting
        assert "stop_daemon_pid" in content

    def test_watchdog_rejects_whitespace_paths(self):
        """Watchdog must reject CACHEOUT_BIN/CACHEOUT_STATE_DIR with whitespace."""
        script_path = Path(__file__).parent.parent / "config" / "cacheout-watchdog.sh"
        content = script_path.read_text()
        assert "[[:space:]]" in content

    def test_watchdog_continue_after_restart_marker(self):
        """After restart.marker triggers start_daemon, the loop must skip the
        health check (continue) to avoid double-starting from stale pidfile."""
        script_path = Path(__file__).parent.parent / "config" / "cacheout-watchdog.sh"
        content = script_path.read_text()
        # Find the while loop body (after "while true; do")
        loop_idx = content.find("while true; do")
        assert loop_idx != -1
        loop_body = content[loop_idx:]
        # The restart marker if-block must contain 'continue' before the
        # first 'fi' closes back at the loop level, preventing fallthrough
        # to the health check
        marker_idx = loop_body.find("if check_restart_marker")
        assert marker_idx != -1
        # Extract up to the "# Tri-state" comment which follows the fi+continue
        tristate_idx = loop_body.find("# Tri-state")
        assert tristate_idx != -1
        marker_block = loop_body[marker_idx:tristate_idx]
        assert "continue" in marker_block, (
            "Restart marker block must 'continue' after start_daemon "
            "to prevent double-start from stale pidfile"
        )

    def test_watchdog_cooldown_constants(self):
        """Verify the script defines correct cooldown constants."""
        script_path = Path(__file__).parent.parent / "config" / "cacheout-watchdog.sh"
        content = script_path.read_text()
        assert "MAX_RESTARTS=3" in content
        assert "COOLDOWN_WINDOW=300" in content
        assert "POLL_INTERVAL=5" in content
