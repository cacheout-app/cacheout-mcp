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
from pydantic import ValidationError


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


# ── Unit: app/standalone response contract parity ────────────────────

class TestResponseNormalizers:
    """The app CLI and standalone paths must produce an identical contract."""

    def test_scan_envelope_wraps_bare_app_list(self):
        """App mode returns a bare list; it must be wrapped like standalone."""
        from cacheout_mcp.server import _scan_envelope, _SCAN_ITEM_KEYS
        app_list = [
            {"slug": "a", "name": "A", "size_bytes": 200, "size_human": "200 B",
             "item_count": 2, "risk_level": "safe", "description": "d",
             "rebuild_note": "n", "exists": True},
            {"slug": "b", "name": "B", "size_bytes": 900, "size_human": "900 B",
             "item_count": 9, "risk_level": "review", "description": "d",
             "rebuild_note": "n", "exists": True},
            {"slug": "gone", "name": "Gone", "size_bytes": 0, "exists": False},
        ]
        env = _scan_envelope(app_list, None)
        # Envelope keys match the standalone contract exactly.
        assert set(env) == {"total_cleanable", "total_cleanable_bytes",
                            "category_count", "categories"}
        # Non-existent categories filtered, sorted largest-first.
        assert env["category_count"] == 2
        assert env["categories"][0]["slug"] == "b"
        assert env["total_cleanable_bytes"] == 1100
        # A schema-3 row projects to exactly the canonical key set — the
        # filter input "exists" is consumed, never echoed.
        assert set(env["categories"][0]) == set(_SCAN_ITEM_KEYS)

    def test_scan_envelope_keeps_schema4_category_fields(self):
        """Schema-4 rows are field-for-field schema 3 PLUS measurement state.

        `state`, `exact_bytes` and `estimated_up_to_bytes` (and, when a
        scan was impeded, `scan_error`/`grant_hint`) must survive: without
        them a client cannot tell measured bytes from estimated additional
        space, nor a `denied` row's zero from an empty one.
        """
        from cacheout_mcp.server import _scan_envelope, _SCAN_ITEM_KEYS
        app_list = [
            {"slug": "xcode_derived_data", "name": "Xcode DerivedData",
             "size_bytes": 15032000000, "size_human": "15.03 GB",
             "item_count": 42, "exists": True, "risk_level": "safe",
             "description": "d", "rebuild_note": "n",
             "state": "measured", "exact_bytes": 15000000000,
             "estimated_up_to_bytes": 32000000},
            {"slug": "browser_caches", "name": "Browser Caches",
             "size_bytes": 0, "size_human": "0 B", "item_count": 0,
             "exists": True, "risk_level": "safe", "description": "d",
             "rebuild_note": "n", "state": "denied", "exact_bytes": 0,
             "estimated_up_to_bytes": 0,
             "scan_error": {"kind": "tcc_denied", "message": "Operation not permitted"},
             "grant_hint": "Grant Full Disk Access..."},
        ]
        env = _scan_envelope(app_list, None)
        big, denied = env["categories"]

        # Canonical keys still present, still first, unchanged in meaning.
        assert set(_SCAN_ITEM_KEYS).issubset(big)
        assert list(big)[:len(_SCAN_ITEM_KEYS)] == list(_SCAN_ITEM_KEYS)
        assert env["total_cleanable_bytes"] == 15032000000

        # Additive schema-4 fields survive the projection.
        assert big["state"] == "measured"
        assert big["exact_bytes"] == 15000000000
        assert big["estimated_up_to_bytes"] == 32000000
        assert denied["state"] == "denied"
        assert denied["scan_error"]["kind"] == "tcc_denied"
        assert denied["grant_hint"].startswith("Grant Full Disk Access")

        # "exists" is consumed by the filter, never forwarded.
        assert "exists" not in big

    def test_scan_envelope_forwards_unknown_additive_fields(self):
        """The protocol grows by ADDITION; an allowlist would re-break this."""
        from cacheout_mcp.server import _scan_envelope
        env = _scan_envelope(
            [{"slug": "a", "name": "A", "size_bytes": 1, "exists": True,
              "a_field_from_a_newer_cli": {"nested": True}}],
            None,
        )
        assert env["categories"][0]["a_field_from_a_newer_cli"] == {"nested": True}

    def test_scan_envelope_min_size_filter(self):
        from cacheout_mcp.server import _scan_envelope
        items = [
            {"slug": "small", "name": "S", "size_bytes": 1024, "exists": True},
            {"slug": "big", "name": "B", "size_bytes": 5 * 1024 * 1024, "exists": True},
        ]
        env = _scan_envelope(items, min_size_mb=1.0)
        assert [c["slug"] for c in env["categories"]] == ["big"]

    def test_normalize_clean_dry_run(self):
        """App dry-run shape -> standalone envelope, recovering slug from name."""
        from cacheout_mcp.server import _normalize_clean_result
        from cacheout_mcp.categories import ALL_CATEGORIES
        sample = ALL_CATEGORIES[0]
        app_dry = {
            "dry_run": True,
            "total_would_free": 4096,
            "results": [
                {"slug": sample.slug, "name": sample.name,
                 "bytes_would_free": 4096, "freed_human": "4 KB"},
            ],
        }
        out = _normalize_clean_result(app_dry)
        assert set(out) == {"total_freed", "total_freed_bytes", "dry_run", "results"}
        assert out["dry_run"] is True
        assert out["total_freed_bytes"] == 4096
        row = out["results"][0]
        assert set(row) == {"slug", "name", "bytes_freed", "freed_human", "success", "error"}
        assert row["bytes_freed"] == 4096
        assert row["success"] is True

    def test_normalize_clean_schema4_aggregate_row_uses_identity_fields(self):
        """Schema-4 aggregate rows state their identity — use it, not the name.

        `results[].category` is the retained ADDRESS key (the category slug
        on aggregate rows) and `scanner_id`/`item_id` name the row outright.
        The old code preferred `name` and reverse-looked-up the display
        string, which is a guess against a name table this repo maintains
        separately from the CLI's.
        """
        from cacheout_mcp.server import _normalize_clean_result
        app_real = {
            "schema_version": 4,
            "dry_run": False,
            "total_freed_bytes": 13204889600,
            "total_estimated_up_to_bytes": 32000000,
            "total_freed": "13.2 GB + up to 32 MB more",
            "results": [
                {"category": "xcode_derived_data",
                 # A display name this client's registry does not carry.
                 "name": "Xcode Derived Data",
                 "bytes_freed": 13204889600, "exact_bytes": 13204889600,
                 "estimated_up_to_bytes": 32000000,
                 "freed_human": "13.2 GB + up to 32 MB more", "success": True,
                 "scanner_id": "categories", "item_id": "xcode_derived_data"},
            ],
            "scanner_rollups": [
                {"scanner_id": "categories", "exact_bytes": 13204889600,
                 "estimated_up_to_bytes": 32000000,
                 "bytes_freed": 13236889600, "entry_count": 1},
            ],
        }
        out = _normalize_clean_result(app_real)
        row = out["results"][0]

        assert row["slug"] == "xcode_derived_data"   # was None: name lookup missed
        assert row["name"] == "Xcode Derived Data"
        assert row["scanner_id"] == "categories"
        assert row["item_id"] == "xcode_derived_data"
        assert row["exact_bytes"] == 13204889600
        assert row["estimated_up_to_bytes"] == 32000000
        # Envelope: legacy keys unchanged, schema-4 additions forwarded.
        assert out["total_freed_bytes"] == 13204889600
        assert out["schema_version"] == 4
        assert out["total_estimated_up_to_bytes"] == 32000000
        assert out["scanner_rollups"][0]["scanner_id"] == "categories"

    def test_normalize_clean_schema4_per_item_row_keeps_its_address(self):
        """A per-item row's address is the only handle a caller has on it."""
        from cacheout_mcp.server import _normalize_clean_result
        item_id = "0d3a9ab9a662fb335a6803cccf0e8a73dd5f1f2a36965334d7f3f5742caeec0e"
        address = f"build_artifacts:{item_id}"
        out = _normalize_clean_result({
            "schema_version": 4,
            "dry_run": False,
            "total_freed_bytes": 1200000000,
            "results": [
                {"category": address, "name": "node_modules",
                 "bytes_freed": 1200000000, "exact_bytes": 1200000000,
                 "estimated_up_to_bytes": 0, "freed_human": "1.2 GB",
                 "success": True, "scanner_id": "build_artifacts",
                 "item_id": item_id},
            ],
        })
        row = out["results"][0]

        # Was None (no category is named "node_modules"), stranding the item.
        assert row["slug"] == address
        assert row["scanner_id"] == "build_artifacts"
        assert row["item_id"] == item_id

    def test_normalize_clean_dry_run_plan_row_address(self):
        """Dry-run plan rows carry `slug` already in address form."""
        from cacheout_mcp.server import _normalize_clean_result
        out = _normalize_clean_result({
            "schema_version": 4,
            "dry_run": True,
            "total_would_free": 4096,
            "results": [
                {"slug": "orphaned_caches:abc123", "name": "some-cache",
                 "state": "measured", "action": "remove_item",
                 "bytes_would_free": 4096, "scanner_id": "orphaned_caches",
                 "item_id": "abc123"},
            ],
        })
        row = out["results"][0]
        assert row["slug"] == "orphaned_caches:abc123"
        assert row["bytes_freed"] == 4096
        assert row["state"] == "measured"
        assert row["action"] == "remove_item"

    def test_normalize_clean_real_recovers_slug_from_name(self):
        """App real-clean results carry only `category` (the name) — recover slug."""
        from cacheout_mcp.server import _normalize_clean_result
        from cacheout_mcp.categories import ALL_CATEGORIES
        sample = ALL_CATEGORIES[0]
        app_real = {
            "dry_run": False,
            "total_freed_bytes": 8192,
            "total_freed": "8 KB",
            "results": [
                {"category": sample.name, "bytes_freed": 8192,
                 "freed_human": "8 KB", "success": True},
                {"category": "Mystery Cache", "error": "boom", "success": False},
            ],
        }
        out = _normalize_clean_result(app_real)
        assert out["dry_run"] is False
        ok, bad = out["results"]
        assert ok["slug"] == sample.slug
        assert ok["name"] == sample.name
        assert bad["success"] is False
        assert bad["error"] == "boom"
        assert bad["bytes_freed"] == 0  # error rows have no byte count
        assert bad["slug"] is None      # unknown name -> no slug recoverable


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

    def test_standalone_rejects_scanner_targets_with_an_error_not_a_crash(self):
        """Standalone has no scanners; say so instead of raising KeyError."""
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            result = run_async(cacheout_clear_cache(ClearCacheInput(
                categories=["npm_cache", "build_artifacts:abc123"], dry_run=True
            )))

        data = json.loads(result)
        assert data["unsupported_targets"] == ["build_artifacts:abc123"]
        assert "npm_cache" in data["supported_targets"]
        assert "Cacheout.app" in data["error"]

    def test_app_mode_forwards_scanner_targets_verbatim(self):
        """The advertised addresses must reach the CLI unchanged."""
        item_id = "0d3a9ab9a662fb335a6803cccf0e8a73dd5f1f2a36965334d7f3f5742caeec0e"
        address = f"build_artifacts:{item_id}"
        fake_engine = MagicMock()
        fake_engine.clean = AsyncMock(return_value={
            "schema_version": 4, "dry_run": False,
            "total_freed_bytes": 1200000000,
            "results": [{"category": address, "name": "node_modules",
                         "bytes_freed": 1200000000, "success": True,
                         "scanner_id": "build_artifacts", "item_id": item_id}],
        })
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", fake_engine):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            result = run_async(cacheout_clear_cache(ClearCacheInput(
                categories=[address, "git_worktrees"]
            )))

        fake_engine.clean.assert_awaited_once_with(
            [address, "git_worktrees"], dry_run=False, acknowledgements=[]
        )
        assert json.loads(result)["results"][0]["slug"] == address

    def test_app_mode_forwards_acknowledgements_verbatim(self):
        """The token the refusal printed must reach the CLI unchanged."""
        item_id = "0d3a9ab9a662fb335a6803cccf0e8a73dd5f1f2a36965334d7f3f5742caeec0e"
        address = f"build_artifacts:{item_id}"
        token = "3b1f0a9d2c4e6b8a0d1f3e5c7a9b1d3f5e7c9a1b3d5f7e9c1a3b5d7f9e1c3a5b"
        entry = f"{address}:{token}"
        fake_engine = MagicMock()
        fake_engine.clean = AsyncMock(return_value={
            "schema_version": 4, "dry_run": False,
            "total_freed_bytes": 1200000000,
            "results": [{"category": address, "name": "target",
                         "bytes_freed": 1200000000, "success": True,
                         "scanner_id": "build_artifacts", "item_id": item_id}],
        })
        with patch("cacheout_mcp.server._MODE", "app"), \
             patch("cacheout_mcp.server._APP_ENGINE", fake_engine):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            run_async(cacheout_clear_cache(ClearCacheInput(
                categories=[address], acknowledge_valuables=[entry]
            )))

        fake_engine.clean.assert_awaited_once_with(
            [address], dry_run=False, acknowledgements=[entry]
        )

    def test_standalone_refuses_acknowledgements_instead_of_ignoring_them(self):
        """A destructive authorization must never land where nothing reads it.

        Standalone runs no scanners, so no valuables gate can fire and no
        token exists to honour. Silently dropping the entry and cleaning
        anyway would tell the caller their acknowledgement was applied.
        """
        token = "3b1f0a9d2c4e6b8a0d1f3e5c7a9b1d3f5e7c9a1b3d5f7e9c1a3b5d7f9e1c3a5b"
        entry = f"build_artifacts:abc123:{token}"
        cleaner = AsyncMock()
        with patch("cacheout_mcp.server._MODE", "standalone"), \
             patch("cacheout_mcp.server._APP_ENGINE", None), \
             patch("cacheout_mcp.server.clean_category", cleaner):
            from cacheout_mcp.server import cacheout_clear_cache, ClearCacheInput
            result = run_async(cacheout_clear_cache(ClearCacheInput(
                categories=["npm_cache"], acknowledge_valuables=[entry]
            )))

        data = json.loads(result)
        assert data["unhonoured_acknowledgements"] == [entry]
        assert "Cacheout.app" in data["error"]
        assert "Nothing was cleaned" in data["error"]
        cleaner.assert_not_awaited()


class TestClearCacheAcknowledgementGrammar:
    """`ClearCacheInput` validates the CLI's frozen acknowledgement entry.

    `--acknowledge-valuables <scanner>:<item-id>:<token>` is destructive
    AUTHORIZATION input, so every locally decidable rule fails closed here
    rather than at delete time. The charset is load-bearing for the same
    reason the target charset is: the entry becomes a CLI FLAG VALUE, and
    one able to begin with `-` could be read as a flag.
    """

    TOKEN = "3b1f0a9d2c4e6b8a0d1f3e5c7a9b1d3f5e7c9a1b3d5f7e9c1a3b5d7f9e1c3a5b"
    ITEM = "0d3a9ab9a662fb335a6803cccf0e8a73dd5f1f2a36965334d7f3f5742caeec0e"

    @classmethod
    def _accepts(cls, *entries):
        from cacheout_mcp.server import ClearCacheInput
        return ClearCacheInput(
            categories=[f"build_artifacts:{cls.ITEM}"],
            acknowledge_valuables=list(entries),
        ).acknowledge_valuables

    @classmethod
    def _rejects(cls, *entries):
        from cacheout_mcp.server import ClearCacheInput
        with pytest.raises(ValidationError) as exc:
            ClearCacheInput(
                categories=[f"build_artifacts:{cls.ITEM}"],
                acknowledge_valuables=list(entries),
            )
        return str(exc.value)

    def test_absent_by_default(self):
        from cacheout_mcp.server import ClearCacheInput
        assert ClearCacheInput(categories=["npm_cache"]).acknowledge_valuables is None

    def test_well_formed_entry_accepted(self):
        entry = f"build_artifacts:{self.ITEM}:{self.TOKEN}"
        assert self._accepts(entry) == [entry]

    def test_one_entry_per_item_accepted(self):
        first = f"build_artifacts:{self.ITEM}:{self.TOKEN}"
        second = f"orphaned_caches:{self.ITEM}:{self.TOKEN}"
        assert self._accepts(first, second) == [first, second]

    def test_row_slug_without_a_token_rejected(self):
        # The likeliest caller mistake: passing the refused row's address
        # and forgetting to join the token onto it.
        message = self._rejects(f"build_artifacts:{self.ITEM}")
        assert "not a valid valuables acknowledgement" in message

    def test_extra_field_rejected(self):
        message = self._rejects(f"build_artifacts:{self.ITEM}:{self.TOKEN}:extra")
        assert "not a valid valuables acknowledgement" in message

    def test_malformed_tokens_rejected(self):
        for token in (
            self.TOKEN[:63],            # truncated
            self.TOKEN + "a",           # too long
            self.TOKEN.upper(),         # uppercase never matches byte-for-byte
            "z" * 64,                   # not hex
            "",                         # empty
            "not-a-token",
        ):
            message = self._rejects(f"build_artifacts:{self.ITEM}:{token}")
            assert "64 lowercase hex characters" in message, token

    def test_argv_hostile_entries_rejected(self):
        # The entry is a CLI flag VALUE. Anything that could parse as a
        # flag, or smuggle a second argument, must be unrepresentable —
        # `_rejects` itself is the assertion that it never validates.
        for hostile in (
            f"--dry-run:{self.ITEM}:{self.TOKEN}",
            f"-x:{self.ITEM}:{self.TOKEN}",
            f"build_artifacts:-flag:{self.TOKEN}",
            f"build_artifacts::{self.TOKEN}",          # no item id
            f"build_artifacts:{self.ITEM} --confirm:{self.TOKEN}",
            f"BUILD_ARTIFACTS:{self.ITEM}:{self.TOKEN}",
            f"build_artifacts:{self.ITEM}\n--confirm:{self.TOKEN}",
            "",
        ):
            message = self._rejects(hostile)
            assert (
                "does not address an item" in message
                or "not a valid valuables acknowledgement" in message
            ), hostile

    def test_aggregate_scanner_cannot_be_acknowledged(self):
        # The aggregate scanner is refused as a target in every form, so it
        # can never be the item half of an acknowledgement either.
        assert "aggregate scanner" in self._rejects(
            f"categories:npm_cache:{self.TOKEN}"
        )

    def test_duplicate_item_rejected(self):
        entry = f"build_artifacts:{self.ITEM}:{self.TOKEN}"
        other_token = "a" * 64
        message = self._rejects(entry, f"build_artifacts:{self.ITEM}:{other_token}")
        assert "one acknowledgement per item" in message
        assert f"build_artifacts:{self.ITEM}" in message

    def test_every_invalid_entry_is_named(self):
        message = self._rejects(
            f"build_artifacts:{self.ITEM}:{self.TOKEN}", "nope", "also-nope"
        )
        assert "'nope'" in message and "'also-nope'" in message


class TestClearCacheTargetGrammar:
    """`ClearCacheInput` validates schema 4's target grammar (PROTOCOL.md).

    It used to reject everything absent from `CATEGORY_MAP`, so none of the
    scanner findings `cacheout_scan_caches` advertises could be cleaned.
    """

    @staticmethod
    def _accepts(*targets):
        from cacheout_mcp.server import ClearCacheInput
        return ClearCacheInput(categories=list(targets)).categories

    @staticmethod
    def _rejects(*targets):
        from cacheout_mcp.server import ClearCacheInput
        with pytest.raises(ValidationError) as exc:
            ClearCacheInput(categories=list(targets))
        return str(exc.value)

    def test_category_slugs_still_accepted(self):
        assert self._accepts("npm_cache", "xcode_derived_data") == [
            "npm_cache", "xcode_derived_data",
        ]

    def test_scanner_slugs_accepted(self):
        for slug in ("build_artifacts", "orphaned_caches", "git_worktrees"):
            assert self._accepts(slug) == [slug]

    def test_scanner_item_addresses_accepted(self):
        item_id = "0d3a9ab9a662fb335a6803cccf0e8a73dd5f1f2a36965334d7f3f5742caeec0e"
        for address in (
            f"build_artifacts:{item_id}",
            f"git_worktrees:{item_id}",
            # A scanner slug this build has never heard of: the id was
            # echoed from a scan, so the CLI polices its own namespace.
            f"scanner_from_the_future:{item_id}",
        ):
            assert self._accepts(address) == [address]

    def test_aggregate_scanner_id_is_refused_in_every_form(self):
        assert "aggregate scanner" in self._rejects("categories")
        assert "aggregate scanner" in self._rejects("categories:npm_cache")

    def test_unknown_bare_token_still_rejected(self):
        # A typo must not become a CLI round-trip.
        assert "Unknown target" in self._rejects("npm_cach")

    def test_argv_hostile_tokens_rejected(self):
        # These become leading POSITIONALS in the CLI argv. A token that
        # could parse as a flag would rewrite the invocation's meaning.
        for hostile in (
            "--confirm", "-x", "--format", "", " ", "npm cache",
            "npm_cache;rm -rf /", "../etc", "NPM_CACHE", "build_artifacts:",
            "build_artifacts:-flag", "a:-b:c", "npm_cache\nbuild_artifacts",
        ):
            assert "not a valid clean target" in self._rejects(hostile), hostile

    def test_every_invalid_target_is_named(self):
        message = self._rejects("npm_cache", "npm_cach", "categories")
        assert "npm_cach" in message and "categories" in message


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
