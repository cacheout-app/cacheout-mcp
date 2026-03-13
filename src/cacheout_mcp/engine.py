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
import shutil
import socket as socket_mod
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

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
        return {
            "total": _human_bytes(self.total_bytes),
            "free": _human_bytes(self.free_bytes),
            "used": _human_bytes(self.used_bytes),
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


# ── App-Delegated Engine ────────────────────────────────────────────

class AppEngine:
    """Delegates operations to Cacheout CLI binary."""

    def __init__(self, binary_path: str):
        self.binary = binary_path

    async def _run(self, *args: str) -> dict:
        """Run a Cacheout CLI command asynchronously and parse JSON output."""
        cmd = [self.binary, "--cli", *args, "--format", "json"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=120,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Cacheout CLI timed out after 120s: {' '.join(cmd)}")
        stdout = stdout_bytes.decode() if stdout_bytes else ""
        stderr = stderr_bytes.decode() if stderr_bytes else ""
        if proc.returncode != 0:
            raise RuntimeError(f"Cacheout CLI error: {stderr.strip()}")
        return json.loads(stdout)

    async def scan_all(self) -> list[dict]:
        return await self._run("scan")

    async def clean(self, slugs: list[str], dry_run: bool = False) -> dict:
        args = ["clean"] + slugs
        if dry_run:
            args.append("--dry-run")
        return await self._run(*args)

    async def smart_clean(self, target_gb: float, dry_run: bool = False, include_caution: bool = False) -> dict:
        args = ["smart-clean", str(target_gb)]
        if dry_run:
            args.append("--dry-run")
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
