"""Schema-4 data must survive MODE selection and CLI ENVELOPE handling.

Three review threads on PR #1, one theme: schema-4 payloads are dropped not
because the CLI failed to produce them, but because this server took a branch
that never asked for them, or threw the answer away on the way back.

1. MODE selection (`_MODE == "socket"` never builds `_APP_ENGINE`) sent every
   scanner slug and addressed item down the standalone "unsupported target"
   branch — so merely starting the daemon removed all per-item cleaning and
   mislabelled the refusal as "standalone". The daemon's socket vocabulary is
   memory/health only (`stats`, `processes`, `compressor`, `health`,
   `config_status`, `recommendations`, `validate_config` —
   `Headless/StatusSocket.swift`), so there is no daemon scan/clean to route
   to: the app binary is the only engine that speaks schema 4.
   AGENTS.md L138-141 rates disk/scan/clean "Full" in ALL three modes.

2. BRANCH selection (`not params.categories`) bypassed the CLI whenever a
   filter was supplied, so filtering an app scan silently downgraded it to the
   legacy local contract — losing `schema_version` and each row's `state` /
   `exact_bytes` / `estimated_up_to_bytes` / `scan_error` / `grant_hint`. A
   `denied` row's zero means NOT MEASURED; without `state` it reads as
   "nothing there".

3. ENVELOPE handling: a confirmed clean of a single valuables-protected item
   is a TOTAL failure, so the CLI exits 1 with the `CLEAN_FAILED` envelope on
   stderr (PROTOCOL.md exit-code policy; `CLIHandler.swift` `exitWithError`).
   `_run` turned that into an opaque RuntimeError, so the very token the
   acknowledgement retry needs never reached the caller in the COMMON case.
"""

from __future__ import annotations

import asyncio
import json
import stat
from unittest.mock import patch

import pytest

from cacheout_mcp.engine import AppEngine


BA_ITEM_ID = "0d3a9ab9a662fb335a6803cccf0e8a73dd5f1f2a36965334d7f3f5742caeec0e"
BA_ADDRESS = f"build_artifacts:{BA_ITEM_ID}"
ACK_TOKEN = "3b1f0a9d2c4e6b8a0d1f3e5c7a9b1d3f5e7c9a1b3d5f7e9c1a3b5d7f9e1c3a5b"
ACK_ENTRY = f"{BA_ADDRESS}:{ACK_TOKEN}"


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def recorded_argv(argv_log):
    return argv_log.read_text().splitlines() if argv_log.exists() else []


# ── Fixtures ─────────────────────────────────────────────────────────

#: A scan envelope with a MEASURED row, a DENIED row (zero bytes that mean
#: "not measured"), and a per-item scanner finding.
SCAN_ENVELOPE = {
    "schema_version": 4,
    "categories": [
        {
            "slug": "xcode_derived_data",
            "name": "Xcode Derived Data",
            "size_bytes": 15032000000,
            "size_human": "15.03 GB",
            "item_count": 42,
            "exists": True,
            "risk_level": "safe",
            "description": "Build artifacts",
            "rebuild_note": "Xcode rebuilds automatically",
            "state": "measured",
            "exact_bytes": 15000000000,
            "estimated_up_to_bytes": 32000000,
        },
        {
            "slug": "browser_caches",
            "name": "Browser Caches",
            "size_bytes": 0,
            "size_human": "0 bytes",
            "item_count": 0,
            "exists": True,
            "risk_level": "review",
            "description": "Browser cache data",
            "rebuild_note": "Browsers refetch",
            # A denied row: the zero is NOT MEASURED, never "nothing there".
            "state": "denied",
            "exact_bytes": 0,
            "estimated_up_to_bytes": 0,
            "scan_error": "TCC denied read of ~/Library/Containers",
            "grant_hint": "Grant Full Disk Access in System Settings > Privacy",
        },
    ],
    "scanner_items": [
        {
            "scanner_id": "build_artifacts",
            "item_id": BA_ITEM_ID,
            "path": "/Users/dev/rustapp/target",
            "name": "target",
            "state": "measured",
            "exact_bytes": 1200000000,
            "estimated_up_to_bytes": 0,
            "size_bytes": 1200000000,
            "item_count": 40231,
            "risk_level": "review",
            "action": "remove_item",
        }
    ],
    "scanner_errors": [
        {
            "scanner_id": "build_artifacts",
            "kind": "container_refused",
            "detail": "dev root is not a usable container",
            "path": "/Users/dev/Elsewhere",
        }
    ],
}

#: Exit 1 + this on stderr: the TOTAL-failure arm of a confirmed clean whose
#: only target was refused by the valuables gate. `details.results` carries
#: the row — including the token the retry must spend.
CLEAN_FAILED_STDERR = {
    "ok": False,
    "error": {
        "code": "CLEAN_FAILED",
        "message": "No requested target could be cleaned",
    },
    "details": {
        "results": [
            {
                "category": BA_ADDRESS,
                "name": "target",
                "bytes_freed": 0,
                "exact_bytes": 0,
                "estimated_up_to_bytes": 0,
                "freed_human": "0 bytes",
                "success": False,
                "scanner_id": "build_artifacts",
                "item_id": BA_ITEM_ID,
                "error": (
                    "/Users/dev/rustapp/target: release artifacts "
                    "(Murmur_0.1.7_aarch64.dmg) are inside this directory at "
                    "delete time and are not covered by an acknowledgement — "
                    "refused, nothing deleted"
                ),
                "valuables": [
                    {
                        "name": "Murmur_0.1.7_aarch64.dmg",
                        "path": "/Users/dev/rustapp/target/release/bundle/"
                                "dmg/Murmur_0.1.7_aarch64.dmg",
                        "allocated_bytes": 44040192,
                        "device": 16777232,
                        "inode": 12345678,
                        "modified_at_ns": 1755057600123456789,
                    }
                ],
                "acknowledgement_token": ACK_TOKEN,
            }
        ]
    },
}

ACKNOWLEDGED_CLEAN_RESULT = {
    "schema_version": 4,
    "dry_run": False,
    "total_freed_bytes": 1200000000,
    "total_freed": "1.2 GB",
    "results": [
        {
            "category": BA_ADDRESS,
            "name": "target",
            "bytes_freed": 1200000000,
            "success": True,
            "scanner_id": "build_artifacts",
            "item_id": BA_ITEM_ID,
        }
    ],
}


def make_cli(tmp_path, *, stdout_payload=None, stderr_payload=None, exit_code=0):
    """A fake Cacheout binary with a fixed stdout/stderr/exit-code triple."""
    argv_log = tmp_path / "argv.json"
    script_lines = ["#!/bin/sh", f'printf \'%s\\n\' "$@" > "{argv_log}"']
    if stdout_payload is not None:
        out_file = tmp_path / "stdout.json"
        out_file.write_text(
            stdout_payload if isinstance(stdout_payload, str)
            else json.dumps(stdout_payload)
        )
        script_lines.append(f'cat "{out_file}"')
    if stderr_payload is not None:
        err_file = tmp_path / "stderr.json"
        err_file.write_text(
            stderr_payload if isinstance(stderr_payload, str)
            else json.dumps(stderr_payload)
        )
        script_lines.append(f'cat "{err_file}" >&2')
    script_lines.append(f"exit {exit_code}")
    script = tmp_path / "fake-cacheout"
    script.write_text("\n".join(script_lines) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), argv_log


def make_valuables_cli(tmp_path):
    """Models the REAL gate: total refusal exits 1 with CLEAN_FAILED on
    stderr; the acknowledged retry exits 0 with the payload on stdout."""
    argv_log = tmp_path / "argv.json"
    refused_file = tmp_path / "refused.json"
    acked_file = tmp_path / "acked.json"
    refused_file.write_text(json.dumps(CLEAN_FAILED_STDERR))
    acked_file.write_text(json.dumps(ACKNOWLEDGED_CLEAN_RESULT))
    script = tmp_path / "fake-cacheout"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$@" > "{argv_log}"\n'
        'for a in "$@"; do\n'
        '  if [ "$a" = "--acknowledge-valuables" ]; then\n'
        f'    cat "{acked_file}"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f'cat "{refused_file}" >&2\n'
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), argv_log


# ── Thread 1: socket mode keeps schema-4 disk/scan/clean ─────────────

class TestSocketModeKeepsSchema4Cleaning:
    """AGENTS.md L138-141: disk/scan/clean are "Full" in socket mode too."""

    def test_socket_mode_cleans_an_addressed_item_via_the_app_binary(self, tmp_path):
        """The defect: starting the daemon removed per-item cleaning entirely."""
        binary, argv_log = make_cli(
            tmp_path, stdout_payload=ACKNOWLEDGED_CLEAN_RESULT
        )
        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value=binary):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            data = json.loads(run_async(cacheout_clear_cache(
                ClearCacheInput(categories=[BA_ADDRESS])
            )))

        assert "error" not in data, (
            "socket mode must not refuse a scanner target as unsupported"
        )
        assert data["results"][0]["slug"] == BA_ADDRESS
        assert data["results"][0]["success"] is True
        assert BA_ADDRESS in recorded_argv(argv_log)

    def test_socket_mode_scan_returns_the_schema4_envelope(self, tmp_path):
        binary, _ = make_cli(tmp_path, stdout_payload=SCAN_ENVELOPE)
        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value=binary):
            from cacheout_mcp.server import cacheout_scan_caches, ScanCachesInput
            data = json.loads(run_async(cacheout_scan_caches(ScanCachesInput())))

        assert data["schema_version"] == 4
        assert data["scanner_items"][0]["item_id"] == BA_ITEM_ID

    def test_socket_mode_acknowledgement_retry_is_honoured(self, tmp_path):
        """Acknowledgement retries were refused outright in socket mode."""
        binary, argv_log = make_valuables_cli(tmp_path)
        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value=binary):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            data = json.loads(run_async(cacheout_clear_cache(ClearCacheInput(
                categories=[BA_ADDRESS], acknowledge_valuables=[ACK_ENTRY]
            ))))

        assert data["results"][0]["success"] is True
        assert "--acknowledge-valuables" in recorded_argv(argv_log)

    def test_socket_mode_disk_usage_uses_the_app_binary(self, tmp_path):
        binary, argv_log = make_cli(
            tmp_path, stdout_payload={"total": "500 GB", "free_gb": 23.4}
        )
        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value=binary):
            from cacheout_mcp.server import cacheout_get_disk_usage, GetDiskUsageInput
            data = json.loads(run_async(
                cacheout_get_disk_usage(GetDiskUsageInput())
            ))

        assert data["free_gb"] == 23.4
        assert "disk-info" in recorded_argv(argv_log)

    def test_socket_without_the_app_still_refuses_but_says_why_honestly(self):
        """No app installed: the refusal is real, and must not be mislabelled.

        The error named "standalone" even when Cacheout.app WAS installed.
        With no binary the refusal is correct — but it must name the actual
        reason (the app is not installed) rather than a mode the server is
        not in.
        """
        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value=None):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            data = json.loads(run_async(cacheout_clear_cache(
                ClearCacheInput(categories=[BA_ADDRESS])
            )))

        assert "error" in data
        assert data["unsupported_targets"] == [BA_ADDRESS]
        # The mode reported must be the mode the server is actually in.
        assert data["mode"] == "socket"
        assert "Cacheout.app" in data["error"]

    def test_app_mode_is_untouched_by_the_socket_fallback(self, tmp_path):
        """A configured _APP_ENGINE is used as-is; no re-resolution."""
        binary, argv_log = make_cli(
            tmp_path, stdout_payload=ACKNOWLEDGED_CLEAN_RESULT
        )
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)), \
             patch("cacheout_mcp.server._find_cacheout_binary",
                   side_effect=AssertionError("must not re-resolve in app mode")):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            data = json.loads(run_async(cacheout_clear_cache(
                ClearCacheInput(categories=[BA_ADDRESS])
            )))

        assert data["results"][0]["success"] is True

    def test_standalone_mode_never_resolves_a_binary(self):
        """Standalone is a deliberate contract, not a missing-app accident."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary",
                   side_effect=AssertionError("must not resolve in standalone")):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            data = json.loads(run_async(cacheout_clear_cache(
                ClearCacheInput(categories=[BA_ADDRESS])
            )))

        assert data["mode"] == "standalone"
        assert data["unsupported_targets"] == [BA_ADDRESS]


# ── Thread 2: filtered app scans keep schema-4 ───────────────────────

class TestFilteredAppScanPreservesSchema4:
    """Filtering must narrow the rows, never change the contract."""

    def test_filtered_scan_keeps_schema_version_and_measurement_fields(self, tmp_path):
        binary, argv_log = make_cli(tmp_path, stdout_payload=SCAN_ENVELOPE)
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)):
            from cacheout_mcp.server import cacheout_scan_caches, ScanCachesInput
            data = json.loads(run_async(cacheout_scan_caches(
                ScanCachesInput(categories=["xcode_derived_data"])
            )))

        # The CLI was actually consulted rather than bypassed.
        assert "scan" in recorded_argv(argv_log)
        assert data["schema_version"] == 4
        assert data["category_count"] == 1
        row = data["categories"][0]
        assert row["slug"] == "xcode_derived_data"
        assert row["state"] == "measured"
        assert row["exact_bytes"] == 15000000000
        assert row["estimated_up_to_bytes"] == 32000000

    def test_filtered_scan_preserves_a_denied_rows_state_and_grant_hint(self, tmp_path):
        """The sharp end: a denied zero must never read as an empty result."""
        binary, _ = make_cli(tmp_path, stdout_payload=SCAN_ENVELOPE)
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)):
            from cacheout_mcp.server import cacheout_scan_caches, ScanCachesInput
            data = json.loads(run_async(cacheout_scan_caches(
                ScanCachesInput(categories=["browser_caches"])
            )))

        assert data["schema_version"] == 4
        row = data["categories"][0]
        assert row["slug"] == "browser_caches"
        assert row["state"] == "denied"
        assert row["scan_error"] == "TCC denied read of ~/Library/Containers"
        assert "Full Disk Access" in row["grant_hint"]

    def test_filter_narrows_the_same_rows_the_unfiltered_scan_returned(self, tmp_path):
        """Filtering is a projection of the full scan, not a different scan."""
        binary, _ = make_cli(tmp_path, stdout_payload=SCAN_ENVELOPE)
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)):
            from cacheout_mcp.server import cacheout_scan_caches, ScanCachesInput
            full = json.loads(run_async(cacheout_scan_caches(ScanCachesInput())))
            filtered = json.loads(run_async(cacheout_scan_caches(
                ScanCachesInput(categories=["xcode_derived_data"])
            )))

        expected = [c for c in full["categories"] if c["slug"] == "xcode_derived_data"]
        assert filtered["categories"] == expected

    def test_filter_composes_with_min_size_mb(self, tmp_path):
        binary, _ = make_cli(tmp_path, stdout_payload=SCAN_ENVELOPE)
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)):
            from cacheout_mcp.server import cacheout_scan_caches, ScanCachesInput
            data = json.loads(run_async(cacheout_scan_caches(ScanCachesInput(
                categories=["xcode_derived_data", "browser_caches"],
                min_size_mb=1024,
            ))))

        assert [c["slug"] for c in data["categories"]] == ["xcode_derived_data"]

    def test_filtering_by_scanner_slug_selects_its_items(self, tmp_path):
        """Scanner slugs are addressable targets, so they are filterable."""
        binary, _ = make_cli(tmp_path, stdout_payload=SCAN_ENVELOPE)
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)):
            from cacheout_mcp.server import cacheout_scan_caches, ScanCachesInput
            data = json.loads(run_async(cacheout_scan_caches(
                ScanCachesInput(categories=["build_artifacts"])
            )))

        assert data["categories"] == []
        assert data["scanner_items"][0]["item_id"] == BA_ITEM_ID
        assert data["scanner_errors"][0]["scanner_id"] == "build_artifacts"

    def test_category_only_filter_does_not_carry_unrelated_scanner_items(self, tmp_path):
        binary, _ = make_cli(tmp_path, stdout_payload=SCAN_ENVELOPE)
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)):
            from cacheout_mcp.server import cacheout_scan_caches, ScanCachesInput
            data = json.loads(run_async(cacheout_scan_caches(
                ScanCachesInput(categories=["xcode_derived_data"])
            )))

        assert data["scanner_items"] == []
        assert data["scanner_errors"] == []

    def test_standalone_filtered_scan_is_unchanged(self):
        """No app: the legacy local contract still applies verbatim."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value=None):
            from cacheout_mcp.server import cacheout_scan_caches, ScanCachesInput
            data = json.loads(run_async(cacheout_scan_caches(
                ScanCachesInput(categories=["xcode_derived_data"])
            )))

        assert "schema_version" not in data
        assert "categories" in data


# ── Thread 3: structured CLEAN_FAILED envelopes come back as data ────

class TestCleanFailedEnvelopeIsReturned:
    """A total refusal is a RESULT, not an opaque error."""

    def test_single_item_refusal_returns_the_token(self, tmp_path):
        binary, _ = make_valuables_cli(tmp_path)
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            data = json.loads(run_async(cacheout_clear_cache(
                ClearCacheInput(categories=[BA_ADDRESS])
            )))

        row = data["results"][0]
        assert row["slug"] == BA_ADDRESS
        assert row["success"] is False
        assert row["bytes_freed"] == 0
        assert row["valuables"][0]["name"] == "Murmur_0.1.7_aarch64.dmg"
        assert row["acknowledgement_token"] == ACK_TOKEN
        # Nothing was freed, and the envelope says so rather than implying it.
        assert data["total_freed_bytes"] == 0
        assert data["dry_run"] is False
        assert data["clean_failed"] is True
        # A dedicated key, not `error`: standalone's envelope-level `error`
        # is a plain string, and one key with two types is exactly the
        # contract drift this repo keeps getting bitten by.
        assert data["clean_failed_error"]["code"] == "CLEAN_FAILED"

    def test_the_whole_refuse_then_acknowledge_loop_closes(self, tmp_path):
        """The gap 53e5210 left: the common single-item case is now spendable."""
        binary, argv_log = make_valuables_cli(tmp_path)
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            refused = json.loads(run_async(cacheout_clear_cache(
                ClearCacheInput(categories=[BA_ADDRESS])
            )))
            row = refused["results"][0]
            entry = f"{row['slug']}:{row['acknowledgement_token']}"
            retried = json.loads(run_async(cacheout_clear_cache(ClearCacheInput(
                categories=[BA_ADDRESS], acknowledge_valuables=[entry]
            ))))

        assert retried["results"][0]["success"] is True
        assert retried["total_freed_bytes"] == 1200000000
        assert recorded_argv(argv_log) == [
            "--cli", "clean", BA_ADDRESS, "--confirm",
            "--acknowledge-valuables", entry, "--format", "json",
        ]

    def test_smart_clean_total_failure_is_also_structured(self, tmp_path):
        payload = {
            "ok": False,
            "error": {"code": "CLEAN_FAILED",
                      "message": "No eligible category could be cleaned"},
            "details": {
                "cleaned": [
                    {"slug": "xcode_derived_data", "name": "Xcode Derived Data",
                     "bytes_freed": 0, "success": False, "error": "denied"}
                ],
                "target_gb": 10.0,
            },
        }
        binary, _ = make_cli(tmp_path, stderr_payload=payload, exit_code=1)
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)):
            from cacheout_mcp.server import cacheout_smart_clean, SmartCleanInput
            data = json.loads(run_async(cacheout_smart_clean(
                SmartCleanInput(target_gb=10.0)
            )))

        assert data["clean_failed"] is True
        assert data["cleaned"][0]["success"] is False
        assert data["target_gb"] == 10.0


class TestGenuineFailuresStillRaise:
    """The whitelist must never let a real failure pass as a result."""

    def _clean(self, binary):
        engine = AppEngine(binary)
        return run_async(engine.clean([BA_ADDRESS]))

    def test_non_json_stderr_still_raises(self, tmp_path):
        binary, _ = make_cli(
            tmp_path, stderr_payload="Segmentation fault: 11", exit_code=139
        )
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            self._clean(binary)

    def test_empty_stderr_still_raises(self, tmp_path):
        binary, _ = make_cli(tmp_path, exit_code=1)
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            self._clean(binary)

    def test_confirmation_required_still_raises(self, tmp_path):
        payload = {
            "ok": False,
            "error": {"code": "CONFIRMATION_REQUIRED", "message": "needs --confirm"},
            "details": {"plan": [], "command": "clean"},
        }
        binary, _ = make_cli(tmp_path, stderr_payload=payload, exit_code=1)
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            self._clean(binary)

    def test_invalid_arguments_still_raises(self, tmp_path):
        payload = {
            "ok": False,
            "error": {"code": "INVALID_ARGUMENTS", "message": "unknown target"},
        }
        binary, _ = make_cli(tmp_path, stderr_payload=payload, exit_code=1)
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            self._clean(binary)

    def test_root_refused_still_raises(self, tmp_path):
        payload = {
            "ok": False,
            "error": {"code": "ROOT_REFUSED", "message": "refuses to run as root"},
        }
        binary, _ = make_cli(tmp_path, stderr_payload=payload, exit_code=1)
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            self._clean(binary)

    def test_missing_argument_still_raises(self, tmp_path):
        payload = {
            "ok": False,
            "error": {"code": "MISSING_ARGUMENT", "message": "no targets given"},
        }
        binary, _ = make_cli(tmp_path, stderr_payload=payload, exit_code=1)
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            self._clean(binary)

    def test_missing_binary_still_raises(self, tmp_path):
        engine = AppEngine(str(tmp_path / "does-not-exist"))
        with pytest.raises((FileNotFoundError, OSError, RuntimeError)):
            run_async(engine.clean([BA_ADDRESS]))

    def test_clean_failed_on_a_non_clean_command_still_raises(self, tmp_path):
        """Only clean/smart-clean can produce a clean verdict."""
        binary, _ = make_cli(
            tmp_path, stderr_payload=CLEAN_FAILED_STDERR, exit_code=1
        )
        engine = AppEngine(binary)
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            run_async(engine.scan_all())

    def test_clean_failed_without_details_still_raises(self, tmp_path):
        payload = {
            "ok": False,
            "error": {"code": "CLEAN_FAILED", "message": "no details"},
        }
        binary, _ = make_cli(tmp_path, stderr_payload=payload, exit_code=1)
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            self._clean(binary)

    def test_clean_failed_with_non_list_results_still_raises(self, tmp_path):
        payload = {
            "ok": False,
            "error": {"code": "CLEAN_FAILED", "message": "bad shape"},
            "details": {"results": "not-a-list"},
        }
        binary, _ = make_cli(tmp_path, stderr_payload=payload, exit_code=1)
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            self._clean(binary)

    def test_ok_true_envelope_on_nonzero_exit_still_raises(self, tmp_path):
        """A self-contradicting payload is not a result to trust."""
        payload = {
            "ok": True,
            "error": {"code": "CLEAN_FAILED", "message": "contradiction"},
            "details": {"results": []},
        }
        binary, _ = make_cli(tmp_path, stderr_payload=payload, exit_code=1)
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            self._clean(binary)

    def test_json_array_stderr_still_raises(self, tmp_path):
        binary, _ = make_cli(tmp_path, stderr_payload="[1, 2, 3]", exit_code=1)
        with pytest.raises(RuntimeError, match="Cacheout CLI error"):
            self._clean(binary)


class TestTimeoutPolicyUnchanged:
    """`resolve_cli_timeout` stays a pure function of argv (cacheout 0b50b62)."""

    def test_socket_routing_builds_the_same_argv_app_mode_does(self, tmp_path):
        from cacheout_mcp.engine import resolve_cli_timeout

        binary, argv_log = make_cli(
            tmp_path, stdout_payload=ACKNOWLEDGED_CLEAN_RESULT
        )
        with patch("cacheout_mcp.server._MODE", "socket"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server._find_cacheout_binary", return_value=binary):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            run_async(cacheout_clear_cache(
                ClearCacheInput(categories=["git_worktrees"])
            ))

        argv = recorded_argv(argv_log)
        assert argv[:4] == ["--cli", "clean", "git_worktrees", "--confirm"]
        # D18: a confirmed clean that may reclaim worktrees runs untimed,
        # decided from this argv tail alone.
        assert resolve_cli_timeout(tuple(argv[1:])) is None
