"""
Cache scanning and cleaning engine.

Two execution modes:
  1. STANDALONE — performs all operations directly via Python (default)
  2. APP — delegates to the Cacheout CLI binary if available

The mode is auto-detected at startup and can be overridden via
CACHEOUT_MODE=standalone|app or CACHEOUT_BIN=/path/to/Cacheout
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket as socket_mod
import sys
import time
from dataclasses import dataclass
from itertools import takewhile
from pathlib import Path
from typing import Any, Optional, Sequence

from .categories import (
    ALL_CATEGORIES,
    CATEGORY_MAP,
    CacheCategory,
    RiskLevel,
)


# ── Configuration ────────────────────────────────────────────────────

# Known locations for the Cacheout binary
_CACHEOUT_SEARCH_PATHS = [
    "/Applications/Cacheout.app/Contents/MacOS/Cacheout",
    "/usr/local/bin/cacheout",
    str(Path.home() / "Applications" / "Cacheout.app" / "Contents" / "MacOS" / "Cacheout"),
]


def _find_cacheout_binary() -> Optional[str]:
    """Locate the Cacheout binary, if installed."""
    # Explicit override
    env_bin = os.environ.get("CACHEOUT_BIN")
    if env_bin and Path(env_bin).is_file():
        return env_bin

    # Search known paths
    for p in _CACHEOUT_SEARCH_PATHS:
        if Path(p).is_file():
            return p

    # Try PATH
    result = shutil.which("cacheout")
    if result:
        return result

    return None


def _get_state_dir() -> str:
    """Return the daemon state directory path from env or default."""
    return os.environ.get("CACHEOUT_STATE_DIR", os.path.join(Path.home(), ".cacheout"))


def _get_socket_path() -> str:
    """Return the daemon Unix socket path."""
    return os.path.join(_get_state_dir(), "status.sock")


def _socket_connectable(path: str, timeout: float = 2.0) -> bool:
    """Test whether the daemon Unix socket is connectable."""
    try:
        s = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)
        s.close()
        return True
    except (OSError, socket_mod.error):
        return False


async def _socket_command(cmd: str, params: Optional[dict[str, Any]] = None,
                          timeout: float = 2.0) -> Optional[dict]:
    """Send a command to the daemon socket and return parsed response.

    Returns None on any connection/parse failure (allows CLI fallback).
    """
    sock_path = _get_socket_path()
    try:
        s = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock_path)

        payload: dict[str, Any] = {"cmd": cmd}
        if params:
            payload.update(params)
        msg = json.dumps(payload) + "\n"
        s.sendall(msg.encode("utf-8"))

        # Read response (up to 64KB)
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        s.close()

        data = b"".join(chunks).decode("utf-8").strip()
        if not data:
            return None
        envelope = json.loads(data)
        if envelope.get("ok"):
            return envelope.get("data")
        return None
    except (OSError, socket_mod.error, json.JSONDecodeError, UnicodeDecodeError):
        return None


async def socket_recommendations(timeout: float = 5.0) -> Optional[dict]:
    """Send 'recommendations' command to daemon socket.

    Returns parsed data dict with 'recommendations' array and '_meta',
    or None on failure (allows fallback to CLI/standalone).
    """
    return await _socket_command("recommendations", timeout=timeout)


async def detect_mode() -> str:
    """Determine execution mode: 'socket', 'app', or 'standalone'.

    Priority: socket > app > standalone.
    Socket mode is used when the daemon's Unix socket is connectable.
    """
    forced = os.environ.get("CACHEOUT_MODE", "").lower()
    if forced in ("app", "standalone", "socket"):
        return forced

    # Check socket first (highest priority)
    sock_path = _get_socket_path()
    if _socket_connectable(sock_path):
        return "socket"

    binary = _find_cacheout_binary()
    if binary:
        # Verify the binary supports --cli mode
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "--cli", "version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                return "app"
        except asyncio.TimeoutError:
            # Reap the hung child to avoid leaking a stray subprocess
            if proc is not None:
                try:
                    proc.terminate()
                    await proc.wait()
                except (OSError, ProcessLookupError):
                    pass
        except (FileNotFoundError, OSError):
            pass

    return "standalone"


# ── Data Classes ─────────────────────────────────────────────────────

@dataclass
class ScanResult:
    slug: str
    name: str
    size_bytes: int
    item_count: int
    exists: bool
    risk_level: str
    description: str
    rebuild_note: str
    clean_priority: int

    @property
    def size_human(self) -> str:
        return _human_bytes(self.size_bytes)


@dataclass
class DiskInfo:
    total_bytes: int
    free_bytes: int
    used_bytes: int

    @property
    def free_gb(self) -> float:
        return self.free_bytes / (1024 ** 3)

    @property
    def used_pct(self) -> float:
        return (self.used_bytes / self.total_bytes * 100) if self.total_bytes > 0 else 0

    def to_dict(self) -> dict:
        # Key set kept in parity with the app CLI's `disk-info` output so the
        # cacheout_get_disk_usage contract is identical in standalone and app modes.
        return {
            "total": _human_bytes(self.total_bytes),
            "total_bytes": self.total_bytes,
            "free": _human_bytes(self.free_bytes),
            "free_bytes": self.free_bytes,
            "used": _human_bytes(self.used_bytes),
            "used_bytes": self.used_bytes,
            "free_gb": round(self.free_gb, 2),
            "used_percent": round(self.used_pct, 1),
        }


@dataclass
class CleanResult:
    category: str
    slug: str
    bytes_freed: int
    success: bool
    error: Optional[str] = None


# ── Standalone Engine ────────────────────────────────────────────────

def get_disk_info() -> DiskInfo:
    """Get current disk usage for the boot volume."""
    stat = os.statvfs("/")
    total = stat.f_frsize * stat.f_blocks
    free = stat.f_frsize * stat.f_bavail
    return DiskInfo(total_bytes=total, free_bytes=free, used_bytes=total - free)


def _dir_size(path: Path) -> tuple[int, int]:
    """Return (total_bytes, file_count) for a directory tree."""
    total = 0
    count = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file() and not entry.is_symlink():
                try:
                    total += entry.stat().st_size
                    count += 1
                except OSError:
                    continue
    except PermissionError:
        pass
    return total, count


def scan_category(cat: CacheCategory) -> ScanResult:
    """Scan a single cache category."""
    total_size = 0
    total_items = 0
    exists = False

    for p in cat.resolved_paths:
        exists = True
        size, count = _dir_size(p)
        total_size += size
        total_items += count

    return ScanResult(
        slug=cat.slug,
        name=cat.name,
        size_bytes=total_size,
        item_count=total_items,
        exists=exists,
        risk_level=cat.risk_level.value,
        description=cat.description,
        rebuild_note=cat.rebuild_note,
        clean_priority=cat.clean_priority,
    )


def scan_all() -> list[ScanResult]:
    """Scan all cache categories. Returns sorted by size descending."""
    results = [scan_category(cat) for cat in ALL_CATEGORIES]
    return sorted(results, key=lambda r: r.size_bytes, reverse=True)


async def clean_category(cat: CacheCategory, dry_run: bool = False) -> CleanResult:
    """Clean a single cache category. Returns bytes freed."""
    # If category has a custom clean command, use it
    if cat.clean_command:
        return await _clean_via_command(cat, dry_run)

    paths = cat.resolved_paths
    if not paths:
        return CleanResult(
            category=cat.name, slug=cat.slug, bytes_freed=0,
            success=True, error=None,
        )

    total_freed = 0
    errors = []

    for dir_path in paths:
        try:
            # Calculate size before cleaning
            size_before, _ = _dir_size(dir_path)

            if not dry_run:
                # Remove contents but keep the directory itself
                for item in dir_path.iterdir():
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        try:
                            item.unlink()
                        except OSError:
                            pass

            total_freed += size_before
        except PermissionError as e:
            errors.append(f"Permission denied: {dir_path}")
        except Exception as e:
            errors.append(f"{dir_path}: {e}")

    if not dry_run:
        _log_cleanup(cat.name, total_freed)

    return CleanResult(
        category=cat.name,
        slug=cat.slug,
        bytes_freed=total_freed,
        success=len(errors) == 0,
        error="; ".join(errors) if errors else None,
    )


async def _clean_via_command(cat: CacheCategory, dry_run: bool = False) -> CleanResult:
    """Clean a category using its custom shell command."""
    # Measure size before
    total_before = 0
    for p in cat.resolved_paths:
        size, _ = _dir_size(p)
        total_before += size

    if dry_run:
        return CleanResult(
            category=cat.name, slug=cat.slug,
            bytes_freed=total_before, success=True, error=None,
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", cat.clean_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin",
                "HOME": str(Path.home()),
            },
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=30,
        )
        stderr = stderr_bytes.decode() if stderr_bytes else ""
        success = proc.returncode == 0
        error = stderr.strip() if not success else None
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        success = False
        error = "Clean command timed out after 30s"
    except Exception as e:
        success = False
        error = str(e)

    if success:
        _log_cleanup(cat.name, total_before)

    return CleanResult(
        category=cat.name, slug=cat.slug,
        bytes_freed=total_before if success else 0,
        success=success, error=error,
    )


async def smart_clean(target_gb: float, dry_run: bool = False, include_caution: bool = False) -> dict:
    """
    Intelligently free disk space by cleaning caches in priority order.

    Stops once target_gb of space has been freed (or all safe categories exhausted).
    Only cleans categories with risk_level == SAFE unless target requires more.
    """
    results = scan_all()
    disk_before = get_disk_info()

    # Sort by clean_priority (lowest first = safest)
    cleanable = sorted(
        [r for r in results if r.exists and r.size_bytes > 0],
        key=lambda r: r.clean_priority,
    )

    target_bytes = int(target_gb * (1024 ** 3))
    freed_so_far = 0
    cleaned = []
    skipped = []

    for result in cleanable:
        if freed_so_far >= target_bytes:
            break

        cat = CATEGORY_MAP.get(result.slug)
        if not cat:
            continue

        # Skip CAUTION categories entirely unless include_caution is set,
        # and even then only allow them once 80% of target is met.
        if cat.risk_level == RiskLevel.CAUTION and (not include_caution or freed_so_far < target_bytes * 0.8):
            skipped.append({
                "slug": result.slug,
                "name": result.name,
                "size": result.size_human,
                "reason": "caution-level risk, skipped"
                    + (" (include_caution=false)" if not include_caution else " (not desperate enough yet)"),
            })
            continue

        clean_result = await clean_category(cat, dry_run=dry_run)
        freed_so_far += clean_result.bytes_freed
        cleaned.append({
            "slug": result.slug,
            "name": result.name,
            "bytes_freed": clean_result.bytes_freed,
            "freed_human": _human_bytes(clean_result.bytes_freed),
            "success": clean_result.success,
            "error": clean_result.error,
        })

    disk_after = get_disk_info()

    return {
        "target_gb": target_gb,
        "target_met": freed_so_far >= target_bytes,
        "total_freed_bytes": freed_so_far,
        "total_freed_human": _human_bytes(freed_so_far),
        "dry_run": dry_run,
        "cleaned": cleaned,
        "skipped": skipped,
        "disk_before": disk_before.to_dict(),
        "disk_after": disk_after.to_dict() if not dry_run else disk_before.to_dict(),
    }


# ── Clean Target Address Grammar ────────────────────────────────────
#
# PROTOCOL.md "Target address grammar (schema 4 — permanent contract)".
# A positional `clean` target token is ONE of:
#
#   <category-slug>              one category aggregate (schema 3)
#   <scanner-slug>               ALL items of that per-item scanner
#   <scanner-slug>:<item-id>     one item, by the opaque id `scan` printed
#
# Category and scanner slugs match `[a-z0-9_]+` (no colon), so the FIRST
# `:` splits the two parts unambiguously, and the two namespaces are
# collision-free by registration.

#: Slug charset for the leading token, plus the optional opaque item id.
#: Item ids are cacheout's frozen 64-char lowercase hex, but PROTOCOL says
#: consumers NEVER parse or derive them — so the id part is validated only
#: as "CLI-safe", not for shape. The anchored slug charset is also what
#: keeps a target from ever starting with `-`: these tokens become leading
#: positionals in the argv `AppEngine._run` builds, and a token that parsed
#: as a FLAG there could rewrite the invocation's meaning.
_CLEAN_TARGET_RE = re.compile(r"^[a-z0-9_]+(?::[A-Za-z0-9][A-Za-z0-9._-]*)?$")

#: The frozen AGGREGATE scanner id. Not a valid target token in any form —
#: `categories` and `categories:<x>` are both refused with
#: INVALID_ARGUMENTS, because a scanner-wide token over every category
#: would be a mass-clean footgun. Aggregates are addressed by slug only.
AGGREGATE_SCANNER_ID = "categories"

#: Per-item scanner slugs whose BARE form ("clean everything this scanner
#: found") this build recognizes. Deliberately not a gate on the ADDRESSED
#: form: `<scanner>:<item-id>` targets are echoed straight back from a
#: scan, so a scanner introduced by a newer CLI stays fully usable without
#: a client update. Only the bare mass-clean of an unknown scanner is
#: refused, which is the safe direction to fail.
ITEM_SCANNER_SLUGS = frozenset({
    "build_artifacts", "orphaned_caches", "git_worktrees",
})


def clean_target_error(target: str) -> str:
    """Why ``target`` is not a valid schema-4 clean target, or ``""``."""
    if not target or not _CLEAN_TARGET_RE.match(target):
        return (
            f"{target!r} is not a valid clean target. Expected a category "
            "slug, a scanner slug, or a '<scanner>:<item-id>' address "
            "echoed from a scan (slugs are lowercase [a-z0-9_])"
        )

    scanner, sep, _item_id = target.partition(":")

    if scanner == AGGREGATE_SCANNER_ID:
        return (
            f"{target!r} is not addressable: the aggregate scanner "
            f"{AGGREGATE_SCANNER_ID!r} is refused in every form. Name the "
            "category slugs you want instead"
        )

    # An addressed item: the id came from a scan, so the scanner namespace
    # is the CLI's to police, not ours.
    if sep:
        return ""

    if target in CATEGORY_MAP or target in ITEM_SCANNER_SLUGS:
        return ""

    return (
        f"Unknown target {target!r}. Valid category slugs: "
        f"{', '.join(CATEGORY_MAP)}. Valid scanner slugs: "
        f"{', '.join(sorted(ITEM_SCANNER_SLUGS))} (or address one of their "
        "items as '<scanner>:<item-id>')"
    )


# ── Valuables Acknowledgement Entries ───────────────────────────────
#
# PROTOCOL.md "Valuables acknowledgement contract (schema 4)".
#
# `clean --confirm` REFUSES to delete an item that discloses release
# artifacts (a .dmg/.pkg/.ipa/.app/.xcarchive/.dSYM inside a build
# directory) unless the caller acknowledges that item by token:
#
#     --acknowledge-valuables <scanner-slug>:<item-id>:<token>
#
# REPEATABLE — one entry per item, because the token is a SHA-256 whose
# preimage BEGINS with `scannerID NUL itemID NUL`. It is therefore bound
# to one item and can never authorize another, so a multi-target clean
# needs one entry for each refused item, never a single per-run token.
# `clean` is the only command that accepts the flag.
#
# What this client validates is only what a client can decide locally:
# entry SHAPE, the address grammar, the token spelling, and one entry per
# item. Everything else is the CLI's, and is fail-safe there — the token
# is recomputed from a fresh delete-time inspection and a stale one simply
# refuses again with a new token; an entry naming an item outside the
# clean's resolved selection is INVALID_ARGUMENTS pre-flight, nothing
# deleted. This client never derives, caches or replays a token: an
# acknowledgement is the user's explicit consent to destroy something the
# scanner flagged, so it can only ever arrive as an explicit input.

#: The token's frozen spelling: the FULL lowercase-hex SHA-256, 64 chars,
#: never a prefix and never uppercase. The CLI compares it byte-for-byte
#: against a freshly derived one, so a spelling that could never match is
#: rejected here rather than at delete time.
_ACK_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

#: Field count of a well-formed entry — exactly two colons. Slug, item id
#: and token are all colon-free by construction, so the colon-joined form
#: parses unambiguously.
_ACK_ENTRY_FIELDS = 3


def acknowledgement_entry_error(entry: str) -> str:
    """Why ``entry`` is not a valid acknowledgement entry, or ``""``.

    An entry is `<scanner>:<item-id>:<token>` — a refused result row's
    ``slug`` (already an address) joined to its ``acknowledgement_token``.

    The address half is validated by `clean_target_error`, which is the
    load-bearing part: the entry is passed to the CLI as a FLAG VALUE, and
    that anchored charset is what makes an entry beginning with `-`
    unrepresentable. Only the ADDRESSED form is accepted — the flag is
    item-bound, so a bare category or scanner slug cannot acknowledge
    anything.
    """
    fields = entry.split(":")
    if len(fields) != _ACK_ENTRY_FIELDS:
        return (
            f"{entry!r} is not a valid valuables acknowledgement. Expected "
            "'<scanner>:<item-id>:<token>' — the refused row's 'slug' joined "
            "to its 'acknowledgement_token' by a colon"
        )

    scanner, item_id, token = fields
    address = f"{scanner}:{item_id}"
    address_error = clean_target_error(address)
    if address_error:
        return f"{entry!r} does not address an item: {address_error}"

    if not _ACK_TOKEN_RE.match(token):
        return (
            f"{entry!r} carries no usable token: expected exactly 64 "
            "lowercase hex characters — the whole 'acknowledgement_token' "
            "the refusal printed, never a prefix and never uppercase"
        )

    return ""


def acknowledgement_item_address(entry: str) -> str:
    """The `<scanner>:<item-id>` a VALIDATED entry binds to.

    Only meaningful after `acknowledgement_entry_error` returned ``""``.
    """
    scanner, item_id, _token = entry.split(":")
    return f"{scanner}:{item_id}"


# ── Client-Side Timeout Policy ──────────────────────────────────────
#
# PROTOCOL.md "Subprocess Timeout" (cacheout fn-5 decision D18).
#
# Callers MUST apply NO client-side timeout to any CONFIRMED `clean` whose
# targets can execute the `git_worktree_reclaim` action. Not a longer
# timeout — none. That clean shells out to `git worktree remove` on a tree
# of unbounded size; an outer kill lands mid-removal and can leave the
# user's git repository in partial state. The CLI's own budgets (300 s per
# git invocation at delete time, plus its SIGTERM -> SIGKILL protocol) are
# the only correct bound, so no finite client-side number is safe here.
#
# Every OTHER invocation — `scan`, `disk-info`, `memory-stats`,
# `recommendations`, `smart-clean` (category-aggregates-only by contract),
# and dry-run cleans — keeps the existing budget.
#
# ── Why the decision reads ONLY the invocation's own argv ───────────
#
# The rule must hold for a clean regardless of what any UNRELATED scan
# happened to do to this process first. An earlier design cached each
# preflight `scan` row's `action` and let a cached
# `"action": "git_worktree_reclaim"` be the thing that disarmed the
# timeout for an address under a scanner we otherwise "resolved" as
# safe. That cache was replaced (not merged) on every scan, so a second
# scan that merely OMITTED an item — a concurrent scan, a transient
# scanner error, a `malformed_outcome` that excludes a scanner's whole
# outcome — silently demoted that item back to the finite budget. The
# clean then got SIGKILLed mid-`git worktree remove`: a fail-OPEN in the
# exact promise this policy exists to make, caused by mutable
# process-global state.
#
# So there is no cache and no scan state. The decision is a pure
# function of the argv being run plus the static category registry, and
# it is conservative in the only direction that is safe:
#
#     a CONFIRMED `clean` keeps the finite budget ONLY when EVERY target
#     is a bare, KNOWN category slug. Anything else — any
#     `<scanner>:<item-id>` address, any bare scanner slug, any token
#     this build does not recognize — gets NO timeout.
#
# That is a superset of PROTOCOL.md's four caller-decidable triggers
# (slug, address, preflight row, scanner-ambiguous), so it can never
# grant a timeout the contract forbids. `git_worktree_reclaim` is
# reserved for per-item scanners but is NOT the private property of the
# `git_worktrees` slug — cacheout's own validator only forbids the
# AGGREGATE `categories` scanner from emitting it — so "an address under
# build_artifacts cannot reach git" was never a sound inference and is
# not made here.
#
# Memory growth bound: zero. Nothing per-scan, per-item or per-target is
# retained anywhere.
#
# The price, taken deliberately: a confirmed per-item clean
# (`build_artifacts`, `orphaned_caches`, …) no longer has a client-side
# kill. PROTOCOL.md permits one there; over-waiting on a wedged CLI is a
# recoverable annoyance, and a premature kill of a composite reclaim is
# a corrupted repository. The CLI bounds itself (300 s per git
# invocation at delete time, plus its SIGTERM -> SIGKILL escalation).

#: Client-side budget for every invocation the no-timeout rule does not cover.
CLI_TIMEOUT_SECONDS: float = 120.0

#: The per-item scanner whose confirmed cleans shell out to git. Kept as an
#: explicit, named trigger even though the conservative default already
#: covers it — PROTOCOL.md's triggers 1 and 2 name this slug, and a reader
#: comparing code to contract should find it.
GIT_WORKTREE_SCANNER_SLUG = "git_worktrees"


def target_may_reclaim_worktrees(target: str) -> bool:
    """Whether cleaning ``target`` could execute a ``git_worktree_reclaim``.

    Conservative by construction: True unless ``target`` is a bare, known
    CATEGORY slug — the one form this client can positively resolve to an
    aggregate clean that never reaches a per-item reclaim action. Pure:
    no scan state, no process globals, no I/O.
    """
    scanner, sep, _item_id = target.partition(":")

    # PROTOCOL triggers 1 + 2: the scanner slug itself, bare or addressed.
    if scanner == GIT_WORKTREE_SCANNER_SLUG:
        return True

    # Any `<scanner>:<item-id>` address. A per-item row's action is the
    # scan's to declare, not ours to infer from the scanner slug, and an
    # id we cannot vouch for is exactly PROTOCOL's "unknown or uncached
    # item id ... treat it as composite".
    if sep:
        return True

    # A bare token is a category slug or a scanner slug. Only a known
    # category slug is provably an aggregate clean.
    return target not in CATEGORY_MAP


#: The ONLY CLI error code whose stderr envelope is a structured RESULT
#: rather than an opaque failure. `CLEAN_FAILED` is a DELETE-TIME verdict
#: (PROTOCOL.md, "Exit-code policy"): the run reached delete time, every
#: resolved target errored, and `details` carries the per-target rows —
#: including the `valuables` / `acknowledgement_token` pair a refusal must
#: hand back for the retry to be possible at all.
#:
#: Every other exit-1 code means the run NEVER reached delete time and has
#: no rows to report: CONFIRMATION_REQUIRED (details.plan), INVALID_ARGUMENTS,
#: MISSING_ARGUMENT, ROOT_REFUSED, UNKNOWN_COMMAND. Those stay hard errors,
#: as does any crash, missing binary, or non-JSON stderr.
_STRUCTURED_CLEAN_FAILURE_CODE = "CLEAN_FAILED"

#: Subcommand -> the `details` key carrying that command's per-target rows.
#: Membership here is also the subcommand whitelist: a `CLEAN_FAILED`-shaped
#: payload on the stderr of a `scan` or `memory-stats` is not a clean verdict
#: and is never treated as one.
_CLEAN_FAILED_ROWS_KEY = {"clean": "results", "smart-clean": "cleaned"}


def parse_clean_failed_envelope(
    args: Sequence[str], stderr: str,
) -> Optional[dict]:
    """The `CLEAN_FAILED` stderr envelope as a result payload, or ``None``.

    A confirmed clean whose only target is refused by the valuables gate is
    a TOTAL failure, so the CLI exits 1 with an empty stdout and this
    envelope on stderr. Treating that exit code as opaque discarded the
    refusal rows — so the single-item refusal, the COMMON case, could never
    hand back the `acknowledgement_token` its own retry requires.

    Recognition is a WHITELIST on shape, never on the exit code: this
    returns ``None`` — meaning "raise, this is a genuine failure" — unless
    all of the following hold.

    1. the invocation was a `clean` or `smart-clean` (`_CLEAN_FAILED_ROWS_KEY`);
    2. stderr parses as a JSON **object**;
    3. it says ``ok`` is exactly ``False`` (a self-contradicting ``ok: true``
       on a nonzero exit is not a payload to trust);
    4. ``error.code`` is exactly ``CLEAN_FAILED``;
    5. ``details`` is an object carrying that command's rows key as a LIST.

    Anything else — a crash, a missing binary, a usage error, a truncated or
    non-JSON stderr, a plausible-looking envelope from the wrong subcommand —
    fails at least one clause and still raises.

    The returned payload is the CLI's own ``details`` plus the facts the
    exit code carried and the details did not: nothing was freed, and this
    was a total failure. ``dry_run`` is ``False`` by contract, not by
    guess — `CLEAN_FAILED` is a delete-time verdict and a dry run performs
    no deletion. No ``schema_version`` is synthesized: the CLI did not send
    one here, and inventing it would assert something it never said.
    """
    if not args:
        return None
    rows_key = _CLEAN_FAILED_ROWS_KEY.get(args[0])
    if rows_key is None:
        return None

    text = stderr.strip()
    if not text:
        return None
    try:
        envelope = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None

    # `is not False`, not `not ...`: a MISSING `ok` is not a denial.
    if envelope.get("ok") is not False:
        return None

    error = envelope.get("error")
    if not isinstance(error, dict):
        return None
    if error.get("code") != _STRUCTURED_CLEAN_FAILURE_CODE:
        return None

    details = envelope.get("details")
    if not isinstance(details, dict):
        return None
    rows = details.get(rows_key)
    if not isinstance(rows, list):
        return None

    # Forward every additive `details` key (`scanner_errors`, `target_gb`, …)
    # rather than allow-listing — the protocol grows by addition.
    payload = {k: v for k, v in details.items() if k != rows_key}
    payload[rows_key] = rows
    payload["clean_failed"] = True
    payload["clean_failed_error"] = {
        "code": error.get("code"),
        "message": error.get("message"),
    }
    payload.setdefault("dry_run", False)
    payload.setdefault("total_freed_bytes", 0)
    payload.setdefault("total_estimated_up_to_bytes", 0)
    return payload


def resolve_cli_timeout(args: Sequence[str]) -> Optional[float]:
    """Client-side timeout for a CLI invocation, or None meaning NO timeout.

    ``args`` is the argv tail handed to ``AppEngine._run`` — the subcommand
    followed by its positional targets and flags. This is the whole input:
    the decision cannot be changed by anything that ran before it.
    """
    if not args or args[0] != "clean":
        return CLI_TIMEOUT_SECONDS

    # Only a CONFIRMED clean reaches delete time. A dry run performs no
    # removal and no git invocation, so it keeps the ordinary budget.
    if "--confirm" not in args:
        return CLI_TIMEOUT_SECONDS

    # Targets are the leading positionals; stop at the first flag so a
    # future flag's VALUE is never mistaken for a target.
    targets = list(takewhile(lambda a: not a.startswith("-"), args[1:]))
    if not targets:
        # Nothing to resolve, so we cannot tell what would run. The CLI
        # rejects a targetless clean instantly with MISSING_ARGUMENT, so
        # waiting costs nothing — take the conservative branch anyway.
        return None

    if any(target_may_reclaim_worktrees(t) for t in targets):
        return None
    return CLI_TIMEOUT_SECONDS


# ── App-Delegated Engine ────────────────────────────────────────────

class AppEngine:
    """Delegates operations to Cacheout CLI binary."""

    def __init__(self, binary_path: str):
        self.binary = binary_path
        # Deliberately stateless across invocations. The client-side
        # timeout decision is a pure function of each invocation's argv
        # (see `resolve_cli_timeout`), so no scan can change how a later
        # clean is bounded — and there is nothing here to grow.

    async def _run(self, *args: str) -> dict:
        """Run a Cacheout CLI command asynchronously and parse JSON output.

        The client-side timeout is decided by `resolve_cli_timeout`, which
        returns None — wait indefinitely — for confirmed cleans that can
        execute `git_worktree_reclaim` (PROTOCOL.md, cacheout D18).

        A nonzero exit raises `RuntimeError` UNLESS stderr carries a
        well-formed `CLEAN_FAILED` envelope for this clean / smart-clean, in
        which case the structured verdict is returned as data — see
        `parse_clean_failed_envelope` for the whitelist.
        """
        cmd = [self.binary, "--cli", *args, "--format", "json"]
        timeout = resolve_cli_timeout(args)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"Cacheout CLI timed out after {timeout:g}s: {' '.join(cmd)}"
            )
        stdout = stdout_bytes.decode() if stdout_bytes else ""
        stderr = stderr_bytes.decode() if stderr_bytes else ""
        if proc.returncode != 0:
            # A nonzero exit is NOT by itself a structured result. Only a
            # provably well-formed CLEAN_FAILED envelope from a clean /
            # smart-clean is recovered as data (see
            # `parse_clean_failed_envelope`); everything else — usage errors,
            # root refusal, a crash, non-JSON stderr — still raises here.
            recovered = parse_clean_failed_envelope(args, stderr)
            if recovered is not None:
                return recovered
            raise RuntimeError(f"Cacheout CLI error: {stderr.strip()}")
        return json.loads(stdout)

    async def scan_all(self) -> dict:
        """Run `--cli scan` and return the schema-4 envelope.

        Schema 4 (PROTOCOL.md) changed scan output from a top-level array to
        `{schema_version, categories, scanner_items, scanner_errors}` — the
        `categories` rows are field-for-field the schema-3 rows, and
        `scanner_items` adds per-item scanner findings (`build_artifacts`,
        `orphaned_caches`, `git_worktrees`) with opaque `item_id`s reusable
        as `clean` targets. A legacy schema-3
        array from an older CLI is wrapped into the same envelope shape so
        callers can always read `data["categories"]`.
        """
        data = await self._run("scan")
        if isinstance(data, list):  # schema <= 3: top-level category array
            return {
                "schema_version": 3,
                "categories": data,
                "scanner_items": [],
                "scanner_errors": [],
            }
        # No row is cached. A later `clean` decides its own timeout from
        # its own argv (`resolve_cli_timeout`), so a scan that omits an
        # item — concurrency, a transient scanner error, a
        # `malformed_outcome` — cannot demote that item's clean to a
        # finite budget.
        return data

    async def clean(
        self,
        slugs: list[str],
        dry_run: bool = False,
        acknowledgements: Sequence[str] = (),
    ) -> dict:
        """Run `--cli clean` on the given targets.

        Targets follow the schema-4 address grammar: category slugs,
        per-item scanner slugs (`build_artifacts`, `orphaned_caches`,
        `git_worktrees`), or `<scanner>:<item-id>` addresses echoed from
        `scan_all()`'s `scanner_items`. A confirmed clean runs with NO
        client-side timeout unless EVERY target is a bare, known category
        slug — see `resolve_cli_timeout`. Destructive
        invocations pass `--confirm` (required since schema 3 — the MCP
        tool call itself is the user's consent); dry runs pass `--dry-run`
        instead. Schema-4 result rows carry additive `scanner_id`/`item_id`
        fields and a top-level `schema_version`.

        ``acknowledgements`` are the caller's item-bound valuables
        acknowledgements (`acknowledgement_entry_error` owns the grammar),
        each emitted as its own `--acknowledge-valuables <entry>` pair —
        the flag is repeatable and one entry authorizes exactly one item.
        They are forwarded on the dry-run path too, because the CLI
        validates entry FORM on every path and only MATCHES tokens on the
        confirmed one, so a dry run is how a caller checks an entry
        without deleting anything.

        Argv shape is load-bearing. Targets stay the LEADING positionals
        (the CLI refuses a positional that appears after any flag), each
        entry is emitted immediately after its own flag occurrence, and
        `_run` appends the trailing `--format json` — so this valued flag
        is never left in trailing position, where a missing value would
        make the acknowledgement read as ABSENT and run the clean
        UNACKNOWLEDGED.
        """
        args = ["clean"] + slugs
        if dry_run:
            args.append("--dry-run")
        else:
            args.append("--confirm")
        for entry in acknowledgements:
            args += ["--acknowledge-valuables", entry]
        return await self._run(*args)

    async def smart_clean(self, target_gb: float, dry_run: bool = False, include_caution: bool = False) -> dict:
        """Run `--cli smart-clean` toward the target.

        Destructive invocations pass `--confirm` (schema 3 gate); dry runs
        pass `--dry-run`. Schema-4 payloads carry a top-level
        `schema_version` and additive `scanner_id`/`item_id` row fields.
        """
        args = ["smart-clean", str(target_gb)]
        if dry_run:
            args.append("--dry-run")
        else:
            args.append("--confirm")
        if include_caution:
            args.append("--include-caution")
        return await self._run(*args)

    async def disk_info(self) -> dict:
        return await self._run("disk-info")

    async def memory_stats(self) -> dict:
        """Delegate to `--cli memory-stats` and return parsed JSON."""
        return await self._run("memory-stats")

    async def recommendations(self) -> list:
        """Delegate to `--cli recommendations` and return parsed JSON array."""
        return await self._run("recommendations")


# ── Helpers ──────────────────────────────────────────────────────────

def _human_bytes(n: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _log_cleanup(category: str, bytes_freed: int) -> None:
    """Append to ~/.cacheout/cleanup.log (shared with the GUI app)."""
    log_dir = Path.home() / ".cacheout"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "cleanup.log"

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    size_str = _human_bytes(bytes_freed)
    entry = f"[{timestamp}] Cleaned {category}: {size_str} (via MCP)\n"

    with open(log_file, "a") as f:
        f.write(entry)
