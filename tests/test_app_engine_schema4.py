"""AppEngine schema-4 consumer-compatibility tests (cacheout fn-2.6 gate).

The cacheout CLI's schema_version 3 -> 4 bump changed `scan` output from a
top-level array to the envelope `{schema_version, categories, scanner_items,
scanner_errors}`, added the clean target address grammar, and put additive
`scanner_id`/`item_id` identity fields plus a top-level `schema_version` on
every clean/smart-clean payload (PROTOCOL.md).

Every OTHER test in this suite patches `cacheout_mcp.server._APP_ENGINE` to
None — an unmodified green run proves nothing about CLI JSON parsing. These
tests are the executable consumer gate: they feed schema-4 scan, clean, and
smart-clean fixtures through the UPDATED `AppEngine.scan_all()`,
`AppEngine.clean()`, and `AppEngine.smart_clean()` against a fake CLI binary
(a script that records argv and prints the fixture payload), asserting both
the parsed shapes and the destructive-invocation `--confirm` contract.
"""

from __future__ import annotations

import asyncio
import json
import stat

import pytest

from cacheout_mcp.engine import AppEngine


# The per-item scanner slug the CLI registers (PROTOCOL.md schema 4). The
# unreleased per-project slug was renamed to build_artifacts before any
# release shipped — no alias exists, so the old slug is an unknown target.
BA_ITEM_ID = "0d3a9ab9a662fb335a6803cccf0e8a73dd5f1f2a36965334d7f3f5742caeec0e"
BA_ADDRESS = f"build_artifacts:{BA_ITEM_ID}"


SCHEMA4_SCAN_ENVELOPE = {
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
            "description": "Build artifacts regenerated on next build",
            "rebuild_note": "Xcode rebuilds automatically",
            "state": "measured",
            "exact_bytes": 15000000000,
            "estimated_up_to_bytes": 32000000,
        }
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
            "evidence": "target/ beside Cargo.toml; last build 94 days ago",
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

SCHEMA4_CLEAN_RESULT = {
    "schema_version": 4,
    "dry_run": False,
    "total_freed_bytes": 14404889600,
    "total_estimated_up_to_bytes": 32000000,
    "total_freed": "14.4 GB + up to 32 MB more",
    "results": [
        {
            "category": "xcode_derived_data",
            "name": "Xcode Derived Data",
            "bytes_freed": 13204889600,
            "exact_bytes": 13204889600,
            "estimated_up_to_bytes": 32000000,
            "freed_human": "13.2 GB + up to 32 MB more",
            "success": True,
            "scanner_id": "categories",
            "item_id": "xcode_derived_data",
        },
        {
            "category": BA_ADDRESS,
            "name": "target",
            "bytes_freed": 1200000000,
            "exact_bytes": 1200000000,
            "estimated_up_to_bytes": 0,
            "freed_human": "1.2 GB",
            "success": True,
            "scanner_id": "build_artifacts",
            "item_id": BA_ITEM_ID,
        },
    ],
    "scanner_rollups": [
        {
            "scanner_id": "categories",
            "exact_bytes": 13204889600,
            "estimated_up_to_bytes": 32000000,
            "bytes_freed": 13236889600,
            "entry_count": 1,
        },
        {
            "scanner_id": "build_artifacts",
            "exact_bytes": 1200000000,
            "estimated_up_to_bytes": 0,
            "bytes_freed": 1200000000,
            "entry_count": 1,
        },
    ],
}

SCHEMA4_SMART_CLEAN_RESULT = {
    "schema_version": 4,
    "target_gb": 10.0,
    "target_met": True,
    "total_freed_bytes": 13204889600,
    "total_estimated_up_to_bytes": 0,
    "total_freed": "13.2 GB",
    "dry_run": False,
    "cleaned": [
        {
            "slug": "xcode_derived_data",
            "name": "Xcode Derived Data",
            "bytes_freed": 13204889600,
            "exact_bytes": 13204889600,
            "estimated_up_to_bytes": 0,
            "freed_human": "13.2 GB",
            "success": True,
            "scanner_id": "categories",
            "item_id": "xcode_derived_data",
        }
    ],
}


def run_async(coro):
    """Run an async coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_fake_cli(tmp_path, payload):
    """A fake Cacheout binary: records its argv, prints the fixture JSON."""
    argv_log = tmp_path / "argv.json"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload))
    script = tmp_path / "fake-cacheout"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$@" > "{argv_log}"\n'
        f'cat "{payload_file}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), argv_log


def recorded_argv(argv_log):
    return argv_log.read_text().splitlines()


class TestAppEngineSchema4Scan:
    """`AppEngine.scan_all()` parses the schema-4 scan envelope."""

    def test_scan_all_parses_schema4_envelope(self, tmp_path):
        binary, argv_log = make_fake_cli(tmp_path, SCHEMA4_SCAN_ENVELOPE)
        engine = AppEngine(binary)

        data = run_async(engine.scan_all())

        assert data["schema_version"] == 4
        # Category rows survive field-for-field inside the envelope.
        assert data["categories"][0]["slug"] == "xcode_derived_data"
        assert data["categories"][0]["exact_bytes"] == 15000000000
        assert data["categories"][0]["state"] == "measured"
        # Per-item scanner rows: identity siblings + opaque item id.
        item = data["scanner_items"][0]
        assert item["scanner_id"] == "build_artifacts"
        assert item["item_id"] == BA_ITEM_ID
        assert len(item["item_id"]) == 64
        assert item["risk_level"] == "review"
        assert item["action"] == "remove_item"
        # Scanner errors ride with their classified kind and path.
        assert data["scanner_errors"][0]["kind"] == "container_refused"
        # scan is read-only: no --confirm, no --dry-run.
        argv = recorded_argv(argv_log)
        assert "scan" in argv
        assert "--confirm" not in argv
        assert "--dry-run" not in argv

    def test_scan_all_wraps_legacy_schema3_array(self, tmp_path):
        legacy_rows = SCHEMA4_SCAN_ENVELOPE["categories"]
        binary, _ = make_fake_cli(tmp_path, legacy_rows)
        engine = AppEngine(binary)

        data = run_async(engine.scan_all())

        assert data["schema_version"] == 3
        assert data["categories"] == legacy_rows
        assert data["scanner_items"] == []
        assert data["scanner_errors"] == []


class TestAppEngineSchema4Clean:
    """`AppEngine.clean()` parses schema-4 results and passes --confirm."""

    def test_clean_parses_schema4_result_and_confirms(self, tmp_path):
        binary, argv_log = make_fake_cli(tmp_path, SCHEMA4_CLEAN_RESULT)
        engine = AppEngine(binary)

        data = run_async(engine.clean(["xcode_derived_data", BA_ADDRESS]))

        assert data["schema_version"] == 4
        assert data["total_freed_bytes"] == 14404889600
        assert data["total_freed"] == "14.4 GB + up to 32 MB more"
        rows = data["results"]
        # Aggregate row: bare slug + identity siblings.
        assert rows[0]["category"] == "xcode_derived_data"
        assert rows[0]["scanner_id"] == "categories"
        assert rows[0]["bytes_freed"] == 13204889600
        assert rows[0]["freed_human"] == "13.2 GB + up to 32 MB more"
        # Per-item row: composite address, reproducible from the siblings —
        # consumers never parse the composite.
        assert rows[1]["category"] == BA_ADDRESS
        assert rows[1]["category"] == f"{rows[1]['scanner_id']}:{rows[1]['item_id']}"
        assert rows[1]["success"] is True
        # Additive rollups are readable.
        assert data["scanner_rollups"][1]["scanner_id"] == "build_artifacts"

        # Destructive invocation carries --confirm (schema >= 3 contract),
        # never --dry-run, and passes the targets through verbatim.
        argv = recorded_argv(argv_log)
        assert "clean" in argv
        assert BA_ADDRESS in argv
        assert "--confirm" in argv
        assert "--dry-run" not in argv

    def test_clean_dry_run_passes_dry_run_not_confirm(self, tmp_path):
        dry_payload = {
            "schema_version": 4,
            "dry_run": True,
            "total_would_free": 13204889600,
            "total_estimated_up_to_bytes": 0,
            "results": [],
        }
        binary, argv_log = make_fake_cli(tmp_path, dry_payload)
        engine = AppEngine(binary)

        data = run_async(engine.clean(["xcode_derived_data"], dry_run=True))

        assert data["schema_version"] == 4
        assert data["dry_run"] is True
        argv = recorded_argv(argv_log)
        assert "--dry-run" in argv
        assert "--confirm" not in argv


class TestAppEngineSchema4SmartClean:
    """`AppEngine.smart_clean()` parses schema-4 results and confirms."""

    def test_smart_clean_parses_schema4_result_and_confirms(self, tmp_path):
        binary, argv_log = make_fake_cli(tmp_path, SCHEMA4_SMART_CLEAN_RESULT)
        engine = AppEngine(binary)

        data = run_async(engine.smart_clean(10.0))

        assert data["schema_version"] == 4
        assert data["target_met"] is True
        assert data["total_freed_bytes"] == 13204889600
        row = data["cleaned"][0]
        # Smart-clean rows keep the as-built `slug` key (asymmetric with
        # clean's `category` — preserved, not "fixed") plus identity fields.
        assert row["slug"] == "xcode_derived_data"
        assert row["scanner_id"] == "categories"
        assert row["item_id"] == "xcode_derived_data"
        assert row["success"] is True

        argv = recorded_argv(argv_log)
        assert "smart-clean" in argv
        assert "10.0" in argv
        assert "--confirm" in argv
        assert "--dry-run" not in argv

    def test_smart_clean_dry_run_passes_dry_run_not_confirm(self, tmp_path):
        dry_payload = dict(SCHEMA4_SMART_CLEAN_RESULT, dry_run=True)
        binary, argv_log = make_fake_cli(tmp_path, dry_payload)
        engine = AppEngine(binary)

        data = run_async(engine.smart_clean(10.0, dry_run=True))

        assert data["schema_version"] == 4
        assert data["dry_run"] is True
        argv = recorded_argv(argv_log)
        assert "--dry-run" in argv
        assert "--confirm" not in argv


# ── Valuables acknowledgement (PROTOCOL.md schema 4, R17) ────────────
#
# `clean --confirm` REFUSES an item that discloses release artifacts and
# hands back a per-item `acknowledgement_token`. The retry carries it as
# `--acknowledge-valuables <scanner>:<item-id>:<token>` — REPEATABLE and
# item-bound, because the token is a SHA-256 whose preimage begins with
# `scannerID NUL itemID NUL` and so authorizes exactly one item.

ACK_TOKEN = "3b1f0a9d2c4e6b8a0d1f3e5c7a9b1d3f5e7c9a1b3d5f7e9c1a3b5d7f9e1c3a5b"
ACK_ENTRY = f"{BA_ADDRESS}:{ACK_TOKEN}"

#: The exit-0 PARTIAL arm: the category target was cleaned, the
#: valuable-bearing item was refused and deleted NOTHING, and the refusal
#: rides the ordinary result-row shape carrying `valuables` + the token.
REFUSED_CLEAN_RESULT = {
    "schema_version": 4,
    "dry_run": False,
    "total_freed_bytes": 13204889600,
    "total_freed": "13.2 GB",
    "results": [
        {
            "category": "xcode_derived_data",
            "name": "Xcode Derived Data",
            "bytes_freed": 13204889600,
            "success": True,
            "scanner_id": "categories",
            "item_id": "xcode_derived_data",
        },
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
                    "path": "/Users/dev/rustapp/target/release/bundle/dmg/"
                            "Murmur_0.1.7_aarch64.dmg",
                    "allocated_bytes": 44040192,
                    "device": 16777232,
                    "inode": 12345678,
                    "modified_at_ns": 1755057600123456789,
                }
            ],
            "acknowledgement_token": ACK_TOKEN,
        },
    ],
}

#: The same clean once the entry is present: the item is deleted.
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


def make_valuables_cli(tmp_path, refused, acknowledged):
    """A fake schema-4 CLI that models the valuables gate at argv level.

    Refuses (exit 0, per-item error row with the token) unless argv carries
    an `--acknowledge-valuables` occurrence, in which case it deletes. It
    records argv on every run, so the shape the retry actually sent is
    observable — not merely the fact that the retry "worked".
    """
    argv_log = tmp_path / "argv.json"
    refused_file = tmp_path / "refused.json"
    acknowledged_file = tmp_path / "acknowledged.json"
    refused_file.write_text(json.dumps(refused))
    acknowledged_file.write_text(json.dumps(acknowledged))
    script = tmp_path / "fake-cacheout"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$@" > "{argv_log}"\n'
        'for a in "$@"; do\n'
        '  if [ "$a" = "--acknowledge-valuables" ]; then\n'
        f'    cat "{acknowledged_file}"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f'cat "{refused_file}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), argv_log


class TestValuablesAcknowledgementArgv:
    """`AppEngine.clean()` forwards acknowledgements as the CLI's flag."""

    def test_entry_rides_between_confirm_and_the_trailing_format_json(
        self, tmp_path
    ):
        """The whole argv shape, pinned.

        Two hazards this client has already been bitten by live here: this
        client ALWAYS appends `--format json`, and a valued flag left in
        TRAILING position has previously parsed as ABSENT — which for an
        acknowledgement means an UNACKNOWLEDGED clean the caller believes
        they authorized. Targets stay leading positionals, each entry
        immediately follows its own flag occurrence, and `--format json`
        stays last.
        """
        binary, argv_log = make_fake_cli(tmp_path, ACKNOWLEDGED_CLEAN_RESULT)

        run_async(AppEngine(binary).clean(
            [BA_ADDRESS], acknowledgements=[ACK_ENTRY]
        ))

        argv = recorded_argv(argv_log)
        assert argv == [
            "--cli", "clean", BA_ADDRESS,
            "--confirm", "--acknowledge-valuables", ACK_ENTRY,
            "--format", "json",
        ]
        # Explicitly: the valued flag is never last, and its value is the
        # very next token — not `--format`, which would swallow it.
        assert argv[-2:] == ["--format", "json"]
        assert argv[argv.index("--acknowledge-valuables") + 1] == ACK_ENTRY

    def test_each_item_gets_its_own_flag_occurrence(self, tmp_path):
        """One entry per item — the token is item-bound, never per-run."""
        other_address = f"orphaned_caches:{BA_ITEM_ID}"
        other_entry = f"{other_address}:{'a' * 64}"
        binary, argv_log = make_fake_cli(tmp_path, ACKNOWLEDGED_CLEAN_RESULT)

        run_async(AppEngine(binary).clean(
            [BA_ADDRESS, other_address],
            acknowledgements=[ACK_ENTRY, other_entry],
        ))

        assert recorded_argv(argv_log) == [
            "--cli", "clean", BA_ADDRESS, other_address, "--confirm",
            "--acknowledge-valuables", ACK_ENTRY,
            "--acknowledge-valuables", other_entry,
            "--format", "json",
        ]

    def test_dry_run_forwards_entries_too(self, tmp_path):
        """The CLI validates entry FORM on every path, matches only on confirm."""
        binary, argv_log = make_fake_cli(
            tmp_path, dict(ACKNOWLEDGED_CLEAN_RESULT, dry_run=True)
        )

        run_async(AppEngine(binary).clean(
            [BA_ADDRESS], dry_run=True, acknowledgements=[ACK_ENTRY]
        ))

        assert recorded_argv(argv_log) == [
            "--cli", "clean", BA_ADDRESS,
            "--dry-run", "--acknowledge-valuables", ACK_ENTRY,
            "--format", "json",
        ]

    def test_no_acknowledgement_leaves_the_invocation_untouched(self, tmp_path):
        """The default path must not grow a flag — an absent flag means
        UNACKNOWLEDGED, which is the CLI's refusing direction."""
        binary, argv_log = make_fake_cli(tmp_path, SCHEMA4_CLEAN_RESULT)

        run_async(AppEngine(binary).clean([BA_ADDRESS]))

        assert recorded_argv(argv_log) == [
            "--cli", "clean", BA_ADDRESS, "--confirm", "--format", "json",
        ]


class TestValuablesRefusalRetryEndToEnd:
    """The whole loop through the real tool: refused -> token -> retry.

    `_clean_row` started forwarding `valuables`/`acknowledgement_token` so a
    caller could retry, but `ClearCacheInput` had no field to carry one back
    and the clean always rebuilt the same argv — the token was visible and
    unspendable. These drive `cacheout_clear_cache` itself against a fake
    schema-4 CLI, so a regression on either half fails here.
    """

    def test_refusal_surfaces_the_token_and_the_retry_spends_it(self, tmp_path):
        from unittest.mock import patch

        binary, argv_log = make_valuables_cli(
            tmp_path, REFUSED_CLEAN_RESULT, ACKNOWLEDGED_CLEAN_RESULT
        )
        engine = AppEngine(binary)

        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", engine):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput

            refused = json.loads(run_async(cacheout_clear_cache(ClearCacheInput(
                categories=["xcode_derived_data", BA_ADDRESS]
            ))))

            # The refusal reaches the caller as DATA, on the ordinary row
            # shape: nothing deleted for that item, what is inside it, and
            # the token to spend.
            row = next(r for r in refused["results"] if r["slug"] == BA_ADDRESS)
            assert row["success"] is False
            assert row["bytes_freed"] == 0
            assert row["valuables"][0]["name"] == "Murmur_0.1.7_aarch64.dmg"
            assert row["acknowledgement_token"] == ACK_TOKEN
            # The first attempt was genuinely unacknowledged.
            assert "--acknowledge-valuables" not in recorded_argv(argv_log)

            # The caller composes the entry from that SAME row — the row's
            # address plus its token, never a constructed or cached one.
            entry = f"{row['slug']}:{row['acknowledgement_token']}"
            retried = json.loads(run_async(cacheout_clear_cache(ClearCacheInput(
                categories=[BA_ADDRESS], acknowledge_valuables=[entry]
            ))))

        assert retried["results"][0]["slug"] == BA_ADDRESS
        assert retried["results"][0]["success"] is True
        assert retried["total_freed_bytes"] == 1200000000
        assert recorded_argv(argv_log) == [
            "--cli", "clean", BA_ADDRESS, "--confirm",
            "--acknowledge-valuables", entry, "--format", "json",
        ]

    def test_a_malformed_token_never_reaches_the_cli(self, tmp_path):
        """Fail-safe direction: a bad entry is refused before anything runs.

        The CLI would refuse it too (INVALID_ARGUMENTS, pre-flight, nothing
        deleted) — but a clean must never be SPAWNED on authorization input
        this client can already tell is unspendable.
        """
        from unittest.mock import patch
        from pydantic import ValidationError

        binary, argv_log = make_valuables_cli(
            tmp_path, REFUSED_CLEAN_RESULT, ACKNOWLEDGED_CLEAN_RESULT
        )

        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", AppEngine(binary)):
            from cacheout_mcp.server import ClearCacheInput
            with pytest.raises(ValidationError):
                ClearCacheInput(
                    categories=[BA_ADDRESS],
                    acknowledge_valuables=[f"{BA_ADDRESS}:{ACK_TOKEN[:20]}"],
                )

        assert not argv_log.exists()
