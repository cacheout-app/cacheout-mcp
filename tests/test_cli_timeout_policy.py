"""Client-side timeout policy tests (cacheout fn-5 decision D18 gate).

PROTOCOL.md "Subprocess Timeout" pins a hard rule on MCP callers: apply NO
client-side timeout to any CONFIRMED `clean` whose targets can execute
`git_worktree_reclaim`. Not a longer timeout — none. Such a clean shells out
to `git worktree remove` on a tree of unbounded size, and an outer kill lands
mid-removal, corrupting the user's git repository state. The CLI's own
budgets (300 s per git invocation at delete time, plus its SIGTERM -> SIGKILL
protocol) are the only correct bound.

The trigger rule is decidable before anything runs, from the invocation's
own argv alone: a CONFIRMED clean keeps the finite budget ONLY when EVERY
target is a bare, known CATEGORY slug. Any `<scanner>:<item-id>` address,
any bare scanner slug, and any unrecognized token get NO timeout. That is a
superset of PROTOCOL.md's four caller-decidable triggers, and — unlike the
preflight-scan cache it replaced — it cannot be changed by whether some
unrelated scan happened to run first (see `TestDecisionIsStateFree`).

These tests cover the decision function directly AND drive it end-to-end
through `AppEngine._run` against a fake CLI binary that stalls, proving a
long-running confirmed worktree clean survives while an ordinary clean past
the same budget is killed.
"""

from __future__ import annotations

import asyncio
import json
import stat

import pytest

from cacheout_mcp import engine as engine_mod
from cacheout_mcp.engine import (
    CLI_TIMEOUT_SECONDS,
    AppEngine,
    resolve_cli_timeout,
    target_may_reclaim_worktrees,
)


WT_ITEM_ID = "8f14e45fceea167a5a36dedd4bea2543a1b2c3d4e5f60718293a4b5c6d7e8f90"
WT_ADDRESS = f"git_worktrees:{WT_ITEM_ID}"

BA_ITEM_ID = "0d3a9ab9a662fb335a6803cccf0e8a73dd5f1f2a36965334d7f3f5742caeec0e"
BA_ADDRESS = f"build_artifacts:{BA_ITEM_ID}"


SCAN_ENVELOPE_WITH_WORKTREES = {
    "schema_version": 4,
    "categories": [],
    "scanner_items": [
        {
            "scanner_id": "git_worktrees",
            "item_id": WT_ITEM_ID,
            "path": "/Users/dev/proj-wt/feature-x",
            "name": "feature-x",
            "state": "measured",
            "size_bytes": 4200000000,
            "risk_level": "review",
            "evidence": "clean checkout; no commits ahead; 190 days stale",
            "action": "git_worktree_reclaim",
        },
        {
            "scanner_id": "build_artifacts",
            "item_id": BA_ITEM_ID,
            "path": "/Users/dev/rustapp/target",
            "name": "target",
            "state": "measured",
            "size_bytes": 1200000000,
            "risk_level": "review",
            "evidence": "target/ beside Cargo.toml; last build 94 days ago",
            "action": "remove_item",
        },
    ],
    "scanner_errors": [],
}

CLEAN_RESULT = {
    "schema_version": 4,
    "dry_run": False,
    "total_freed_bytes": 4200000000,
    "results": [
        {
            "category": WT_ADDRESS,
            "name": "feature-x",
            "bytes_freed": 4200000000,
            "success": True,
            "scanner_id": "git_worktrees",
            "item_id": WT_ITEM_ID,
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


def make_scripted_cli(tmp_path, scan_payload, clean_payload, clean_delay="0"):
    """A fake Cacheout binary that dispatches on the subcommand.

    Records argv, answers `scan` immediately, and can stall a `clean` for
    `clean_delay` seconds to model a long-running `git worktree remove`
    without running git for minutes. argv arrives as
    `--cli <subcommand> ... --format json`, so the subcommand is "$2".
    """
    argv_log = tmp_path / "argv.json"
    scan_file = tmp_path / "scan.json"
    clean_file = tmp_path / "clean.json"
    scan_file.write_text(json.dumps(scan_payload))
    clean_file.write_text(json.dumps(clean_payload))
    script = tmp_path / "fake-cacheout"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$@" > "{argv_log}"\n'
        'if [ "$2" = "scan" ]; then\n'
        f'  cat "{scan_file}"\n'
        "else\n"
        f"  sleep {clean_delay}\n"
        f'  cat "{clean_file}"\n'
        "fi\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), argv_log


class TestTargetMayReclaimWorktrees:
    """The per-target trigger rule, decidable before anything runs."""

    def test_bare_scanner_slug_is_composite(self):
        assert target_may_reclaim_worktrees("git_worktrees") is True

    def test_addressed_item_is_composite(self):
        assert target_may_reclaim_worktrees(WT_ADDRESS) is True

    def test_known_category_slug_is_not_composite(self):
        assert target_may_reclaim_worktrees("xcode_derived_data") is False

    def test_any_per_item_scanner_slug_is_composite(self):
        # `git_worktree_reclaim` is reserved for per-item scanners but is
        # NOT private to the `git_worktrees` slug (cacheout's validator
        # forbids only the AGGREGATE `categories` scanner from emitting
        # it), so no per-item scanner is resolvable as git-incapable.
        assert target_may_reclaim_worktrees("build_artifacts") is True
        assert target_may_reclaim_worktrees("orphaned_caches") is True

    def test_unknown_bare_token_is_composite(self):
        # Could be a composite scanner slug from a newer CLI. Conservative.
        assert target_may_reclaim_worktrees("some_future_scanner") is True

    def test_unknown_scanner_address_is_composite(self):
        assert target_may_reclaim_worktrees("some_future_scanner:abc123") is True

    def test_every_addressed_item_is_composite(self):
        # An item id is opaque: its row's `action` is the scan's to
        # declare, never ours to infer from the scanner slug. PROTOCOL's
        # "unknown or uncached item id ... treat it as composite".
        assert target_may_reclaim_worktrees(BA_ADDRESS) is True
        assert target_may_reclaim_worktrees("build_artifacts:never-seen") is True
        assert target_may_reclaim_worktrees("orphaned_caches:abc") is True

    def test_only_a_bare_known_category_slug_is_resolvable(self):
        # The single positive case, and it is a static-registry lookup.
        assert target_may_reclaim_worktrees("npm_cache") is False
        # ...not the same slug in addressed form.
        assert target_may_reclaim_worktrees("npm_cache:anything") is True

    def test_the_decision_takes_no_input_but_the_target(self):
        # Regression guard for the fail-open this replaced: the signature
        # must not grow a mutable-state parameter again.
        import inspect

        params = list(inspect.signature(target_may_reclaim_worktrees).parameters)
        assert params == ["target"]


class TestResolveCliTimeout:
    """The whole-invocation decision: a number, or None meaning NO timeout."""

    def test_confirmed_clean_on_bare_worktree_slug_has_no_timeout(self):
        args = ("clean", "git_worktrees", "--confirm")
        assert resolve_cli_timeout(args) is None

    def test_confirmed_clean_on_worktree_address_has_no_timeout(self):
        args = ("clean", WT_ADDRESS, "--confirm")
        assert resolve_cli_timeout(args) is None

    def test_one_composite_target_in_a_mixed_selection_disarms_the_timeout(self):
        args = ("clean", "xcode_derived_data", BA_ADDRESS, WT_ADDRESS, "--confirm")
        assert resolve_cli_timeout(args) is None

    def test_confirmed_clean_on_unknown_target_has_no_timeout(self):
        assert resolve_cli_timeout(("clean", "mystery_slug", "--confirm")) is None
        assert resolve_cli_timeout(("clean", "mystery:abc", "--confirm")) is None

    def test_targetless_confirmed_clean_has_no_timeout(self):
        # Nothing to resolve => cannot tell => conservative branch.
        assert resolve_cli_timeout(("clean", "--confirm")) is None

    def test_addressed_item_clean_has_no_timeout(self):
        # The reviewer's fail-open, at the decision level: this must be
        # None with NO preflight scan anywhere in the picture.
        assert resolve_cli_timeout(("clean", BA_ADDRESS, "--confirm")) is None

    def test_confirmed_category_clean_keeps_the_timeout(self):
        args = ("clean", "xcode_derived_data", "npm_cache", "--confirm")
        assert resolve_cli_timeout(args) == CLI_TIMEOUT_SECONDS

    def test_confirmed_other_scanner_clean_has_no_timeout(self):
        # A bare scanner slug selects every item that scanner found — and
        # any per-item scanner may emit `git_worktree_reclaim`.
        assert resolve_cli_timeout(("clean", "build_artifacts", "--confirm")) is None
        assert resolve_cli_timeout(("clean", "orphaned_caches", "--confirm")) is None

    def test_dry_run_worktree_clean_keeps_the_timeout(self):
        # Only a CONFIRMED clean reaches delete time and invokes git.
        args = ("clean", WT_ADDRESS, "--dry-run")
        assert resolve_cli_timeout(args) == CLI_TIMEOUT_SECONDS

    def test_read_only_and_aggregate_commands_keep_the_timeout(self):
        for args in (
            ("scan",),
            ("disk-info",),
            ("memory-stats",),
            ("recommendations",),
            ("smart-clean", "10.0", "--confirm"),
            ("smart-clean", "10.0", "--dry-run"),
        ):
            assert resolve_cli_timeout(args) == CLI_TIMEOUT_SECONDS, args

    def test_flag_value_is_never_mistaken_for_a_target(self):
        # Targets are the leading positionals; a flag's VALUE is not one.
        args = (
            "clean", "xcode_derived_data",
            "--acknowledge-valuables", f"{BA_ADDRESS}:tok",
            "--confirm",
        )
        assert resolve_cli_timeout(args) == CLI_TIMEOUT_SECONDS

    def test_empty_argv_keeps_the_timeout(self):
        assert resolve_cli_timeout(()) == CLI_TIMEOUT_SECONDS


class TestAppEngineHonoursThePolicy:
    """`AppEngine._run` applies the decision to a real subprocess."""

    def test_long_running_confirmed_worktree_clean_is_not_killed(
        self, tmp_path, monkeypatch
    ):
        # Budget shrunk to 0.15 s and the fake CLI stalls 5x that. A
        # timeout-applying caller would SIGKILL it mid-"removal"; the
        # no-timeout rule must let it finish and return its payload.
        monkeypatch.setattr(engine_mod, "CLI_TIMEOUT_SECONDS", 0.15)
        binary, argv_log = make_scripted_cli(
            tmp_path, SCAN_ENVELOPE_WITH_WORKTREES, CLEAN_RESULT, clean_delay="0.75"
        )
        engine = AppEngine(binary)

        data = run_async(engine.clean([WT_ADDRESS]))

        assert data["schema_version"] == 4
        assert data["results"][0]["scanner_id"] == "git_worktrees"
        assert data["results"][0]["success"] is True
        argv = argv_log.read_text().splitlines()
        assert WT_ADDRESS in argv
        assert "--confirm" in argv

    def test_argv_shape_for_an_addressed_clean(self, tmp_path):
        """Targets are leading positionals and `--format json` stays LAST.

        This client ALWAYS appends `--format json`, so every target and flag
        has to sit before it — and no valued flag may end up in trailing
        position, where this CLI has previously parsed one as ABSENT.
        """
        binary, argv_log = make_scripted_cli(
            tmp_path, SCAN_ENVELOPE_WITH_WORKTREES, CLEAN_RESULT
        )

        run_async(AppEngine(binary).clean([BA_ADDRESS, "npm_cache"]))

        argv = argv_log.read_text().splitlines()
        assert argv == [
            "--cli", "clean", BA_ADDRESS, "npm_cache",
            "--confirm", "--format", "json",
        ]

    def test_long_running_bare_worktree_slug_clean_is_not_killed(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(engine_mod, "CLI_TIMEOUT_SECONDS", 0.15)
        binary, _ = make_scripted_cli(
            tmp_path, SCAN_ENVELOPE_WITH_WORKTREES, CLEAN_RESULT, clean_delay="0.75"
        )
        engine = AppEngine(binary)

        data = run_async(engine.clean(["git_worktrees"]))

        assert data["total_freed_bytes"] == 4200000000

    def test_long_running_unknown_target_clean_is_not_killed(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(engine_mod, "CLI_TIMEOUT_SECONDS", 0.15)
        binary, _ = make_scripted_cli(
            tmp_path, SCAN_ENVELOPE_WITH_WORKTREES, CLEAN_RESULT, clean_delay="0.75"
        )
        engine = AppEngine(binary)

        data = run_async(engine.clean(["scanner_from_the_future:abc"]))

        assert data["schema_version"] == 4

    def test_ordinary_category_clean_is_still_killed_at_the_budget(
        self, tmp_path, monkeypatch
    ):
        # Same stalling binary, ordinary target: the existing timeout
        # survives this change and still fires.
        monkeypatch.setattr(engine_mod, "CLI_TIMEOUT_SECONDS", 0.15)
        binary, _ = make_scripted_cli(
            tmp_path, SCAN_ENVELOPE_WITH_WORKTREES, CLEAN_RESULT, clean_delay="0.75"
        )
        engine = AppEngine(binary)

        with pytest.raises(RuntimeError, match="timed out after 0.15s"):
            run_async(engine.clean(["xcode_derived_data"]))

    def test_dry_run_worktree_clean_is_still_killed_at_the_budget(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(engine_mod, "CLI_TIMEOUT_SECONDS", 0.15)
        binary, _ = make_scripted_cli(
            tmp_path, SCAN_ENVELOPE_WITH_WORKTREES, CLEAN_RESULT, clean_delay="0.75"
        )
        engine = AppEngine(binary)

        with pytest.raises(RuntimeError, match="timed out"):
            run_async(engine.clean([WT_ADDRESS], dry_run=True))

    def test_default_budget_is_unchanged(self):
        assert CLI_TIMEOUT_SECONDS == 120.0


class TestDecisionIsStateFree:
    """No intervening scan can change how a later clean is bounded.

    The fail-open this replaced: `scan_all()` cached each row's `action`
    and REPLACED that cache every scan, so an item dropped from a later
    scan (a concurrent scan, a transient scanner error, a
    `malformed_outcome` that excludes a whole scanner's outcome) silently
    reverted to the finite budget — and a confirmed clean of that address
    could then be SIGKILLed mid-`git worktree remove`.
    """

    def test_engine_keeps_no_per_scan_state(self, tmp_path):
        binary, _ = make_scripted_cli(
            tmp_path, SCAN_ENVELOPE_WITH_WORKTREES, CLEAN_RESULT
        )
        engine = AppEngine(binary)

        run_async(engine.scan_all())

        # Only the binary path. Nothing scan-derived is retained, so the
        # memory bound is zero and there is no cache to lose.
        assert vars(engine) == {"binary": binary}

    def test_scan_all_still_returns_the_envelope(self, tmp_path):
        binary, _ = make_scripted_cli(
            tmp_path, SCAN_ENVELOPE_WITH_WORKTREES, CLEAN_RESULT
        )
        engine = AppEngine(binary)

        data = run_async(engine.scan_all())

        assert data["schema_version"] == 4
        assert [r["scanner_id"] for r in data["scanner_items"]] == [
            "git_worktrees", "build_artifacts",
        ]

    def test_legacy_schema3_array_still_wraps(self, tmp_path):
        binary, _ = make_scripted_cli(tmp_path, [], CLEAN_RESULT)
        engine = AppEngine(binary)

        data = run_async(engine.scan_all())

        assert data == {
            "schema_version": 3,
            "categories": [],
            "scanner_items": [],
            "scanner_errors": [],
        }

    def test_an_intervening_scan_that_drops_an_item_cannot_arm_a_timeout(
        self, tmp_path, monkeypatch
    ):
        """THE regression test for the reviewer's fail-open.

        Scan #1 advertises `build_artifacts:<id>` carrying
        `"action": "git_worktree_reclaim"`. Scan #2 omits it. The confirmed
        clean of that same address must still run unbounded — under the old
        cache it was demoted to the finite budget and killed mid-removal.
        """
        composite_ba = dict(
            SCAN_ENVELOPE_WITH_WORKTREES["scanner_items"][1],
            action="git_worktree_reclaim",
        )
        first = dict(SCAN_ENVELOPE_WITH_WORKTREES, scanner_items=[composite_ba])
        binary, _ = make_scripted_cli(
            tmp_path, first, CLEAN_RESULT, clean_delay="0.75"
        )
        engine = AppEngine(binary)
        run_async(engine.scan_all())

        # Scan #2 — same engine, same process — no longer reports the item
        # (the fake CLI reads its answer from scan.json on every call).
        dropped = dict(SCAN_ENVELOPE_WITH_WORKTREES, scanner_items=[])
        (tmp_path / "scan.json").write_text(json.dumps(dropped))
        assert run_async(engine.scan_all())["scanner_items"] == []

        # Shrink the budget only now: the scans above are ordinary
        # invocations and keep it, so only the clean's decision is under
        # test. The fake CLI stalls 5x the budget.
        monkeypatch.setattr(engine_mod, "CLI_TIMEOUT_SECONDS", 0.15)
        data = run_async(engine.clean([BA_ADDRESS]))

        assert data["schema_version"] == 4

    def test_a_fresh_engine_decides_identically(self, tmp_path, monkeypatch):
        """No preflight scan at all — same outcome as after one."""
        monkeypatch.setattr(engine_mod, "CLI_TIMEOUT_SECONDS", 0.15)
        binary, _ = make_scripted_cli(
            tmp_path, SCAN_ENVELOPE_WITH_WORKTREES, CLEAN_RESULT, clean_delay="0.75"
        )

        data = run_async(AppEngine(binary).clean([BA_ADDRESS]))

        assert data["schema_version"] == 4
