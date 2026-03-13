"""Standalone memory statistics via sysctl and Mach host_statistics64.

Used when CacheOut.app is not installed. Reads macOS kernel memory
counters via ``sysctl -n <key>`` for scalar values (hw.memsize,
hw.pagesize, compressor stats, swap, pressure). For VM page counts
(free, active, inactive, wired) uses the Mach ``host_statistics64``
API via ctypes -- the same kernel interface the Swift app calls
directly via its C bindings.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import json
import struct
import sys
from typing import Any, Optional


# ── Mach host_statistics64 via ctypes ────────────────────────────────

# Load libSystem (always present on macOS)
_libc_path = ctypes.util.find_library("System")
_libc = ctypes.CDLL(_libc_path) if _libc_path else None

# Mach constants
HOST_VM_INFO64 = 4
KERN_SUCCESS = 0

# ── Thrashing heuristic constants (aligned with CompressorTracker.swift) ──
THRASHING_ABSOLUTE_MINIMUM = 100.0  # decompressions/sec
THRASHING_RATIO_THRESHOLD = 2.0     # decompression_rate / compression_rate


def _get_vm_stats_via_mach() -> Optional[dict]:
    """Read VM page counts via Mach host_statistics64 (same API as Swift app).

    Returns dict with page counts: free, active, inactive, wired.
    Returns None if the Mach call fails.
    """
    if _libc is None:
        return None

    host = None
    try:
        # Get host port
        mach_host_self = _libc.mach_host_self
        mach_host_self.restype = ctypes.c_uint32
        host = mach_host_self()

        # vm_statistics64_data_t fields are natural_t (unsigned int, 4 bytes
        # on macOS even on arm64). The kernel returns the struct as an array
        # of integer_t (int32) values. Max count = struct_size / sizeof(int32).
        # Empirically the kernel returns count=40 (160 bytes).
        max_count = 40
        count = ctypes.c_uint32(max_count)
        buf = ctypes.create_string_buffer(max_count * 4)

        host_statistics64 = _libc.host_statistics64
        host_statistics64.restype = ctypes.c_int
        host_statistics64.argtypes = [
            ctypes.c_uint32,  # host
            ctypes.c_int,     # flavor
            ctypes.c_void_p,  # info
            ctypes.POINTER(ctypes.c_uint32),  # count
        ]

        kr = host_statistics64(host, HOST_VM_INFO64, buf, ctypes.byref(count))
        if kr != KERN_SUCCESS:
            return None

        # vm_statistics64_data_t layout (natural_t = uint32):
        # [0] free_count
        # [1] active_count
        # [2] inactive_count
        # [3] wire_count
        actual_count = count.value
        if actual_count < 4:
            return None

        values = struct.unpack_from(f"<{actual_count}I", buf.raw)

        return {
            "free": values[0],
            "active": values[1],
            "inactive": values[2],
            "wired": values[3],
        }
    except (OSError, ValueError, struct.error):
        return None
    finally:
        # Release the Mach port right to avoid kernel resource leaks
        if host is not None:
            try:
                _libc.mach_port_deallocate(_libc.mach_task_self(), host)
            except (OSError, ctypes.ArgumentError):
                pass



def _parse_byte_value(s: str) -> float:
    """Parse a byte value string like '1234.56M' or '0.00K' to bytes."""
    s = s.strip()
    if not s:
        return 0.0
    multipliers = {
        "K": 1024,
        "M": 1024 ** 2,
        "G": 1024 ** 3,
        "T": 1024 ** 4,
    }
    if s[-1].upper() in multipliers:
        return float(s[:-1]) * multipliers[s[-1].upper()]
    return float(s)


# ── Async subprocess helper ──────────────────────────────────────────

async def _async_run(cmd: list[str], timeout: float) -> tuple[str, Optional[str]]:
    """Run a subprocess asynchronously with a timeout.

    Uses asyncio.create_subprocess_exec (no shell) for safety.
    Returns (stdout, None) on success or ("", error_message) on failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            return "", f"Command {cmd[0]} exited with code {proc.returncode}: {stderr.strip()}"
        return stdout, None
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except (ProcessLookupError, OSError):
            pass
        return "", f"Command {cmd[0]} timed out after {timeout}s"
    except (OSError, FileNotFoundError) as e:
        return "", f"Failed to run {cmd[0]}: {e}"


# ── ps output parser ────────────────────────────────────────────────

def parse_ps_output(output: str) -> list[dict]:
    """Parse output of ``ps -axo pid=,rss=,comm=``.

    Each line: ``<pid>  <rss_kb>  <command_path>``
    Returns list of dicts with pid, rss_kb, rss_mb, command, note.
    """
    results = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(parts[1])
        except ValueError:
            continue
        command = parts[2].strip()
        results.append({
            "pid": pid,
            "rss_kb": rss_kb,
            "rss_mb": round(rss_kb / 1024.0, 2),
            "command": command,
            "note": "RSS-based estimate (not true physical footprint)",
        })
    return results


# ── vm_stat output parser ───────────────────────────────────────────

def parse_vm_stat_output(output: str) -> dict:
    """Parse vm_stat output into a dict of counter_name -> page_count.

    Lines look like: ``Pages compressed:    1234567``
    Returns dict mapping lowercase key (spaces replaced with _) to int value.
    """
    result = {}
    for line in output.strip().splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip().rstrip(".")
        try:
            result[key.strip().lower().replace(" ", "_")] = int(val)
        except ValueError:
            continue
    return result


# ── sysctl parser (for compressor bytes) ────────────────────────────

async def _async_sysctl_int(key: str, timeout: float = 5.0) -> Optional[int]:
    """Read a single integer sysctl value asynchronously."""
    stdout, err = await _async_run(["sysctl", "-n", key], timeout=timeout)
    if err is not None:
        return None
    try:
        return int(stdout.strip())
    except ValueError:
        return None


# ── Sysctl pressure and compressor ratio parsers ─────────────────────

# Raw kernel pressure level mapping (intentionally simpler than Swift PressureTier)
PRESSURE_LABEL_MAP: dict[int, str] = {
    0: "normal",
    1: "warn",
    2: "critical",
    4: "urgent",
}


def parse_sysctl_pressure_level(raw_value: int) -> str:
    """Map a raw kern.memorystatus_vm_pressure_level int to a label string.

    Mapping: 0→normal, 1→warn, 2→critical, 4→urgent.
    Unknown values return "unknown".
    """
    return PRESSURE_LABEL_MAP.get(raw_value, "unknown")


async def parse_sysctl_compressor_ratio() -> Optional[float]:
    """Read compressor ratio from sysctl: compressed_bytes / bytes_used.

    Returns None if either sysctl value is unavailable.
    Returns 0.0 if bytes_used == 0 (nothing compressed).
    """
    compressed = await _async_sysctl_int("vm.compressor_compressed_bytes")
    used = await _async_sysctl_int("vm.compressor_bytes_used")

    if compressed is None or used is None:
        return None
    if used == 0:
        return 0.0
    return round(float(compressed) / float(used), 4)


# ── Async swap parser ────────────────────────────────────────────────

async def _async_parse_swap_used() -> Optional[float]:
    """Parse swap used bytes from ``sysctl vm.swapusage`` asynchronously.

    Output format: ``vm.swapusage: total = X  used = Y  free = Z  ...``
    Returns the "used" value in bytes, or None on failure.
    """
    stdout, err = await _async_run(["sysctl", "vm.swapusage"], timeout=5.0)
    if err is not None:
        return None
    for part in stdout.split("  "):
        part = part.strip()
        if part.startswith("used = "):
            val_str = part[len("used = "):]
            return _parse_byte_value(val_str)
    return None


async def _async_parse_swap_total() -> Optional[float]:
    """Parse swap total bytes from ``sysctl vm.swapusage`` asynchronously.

    Returns the "total" value in bytes, or None on failure.
    """
    stdout, err = await _async_run(["sysctl", "vm.swapusage"], timeout=5.0)
    if err is not None:
        return None
    for part in stdout.split("  "):
        part = part.strip()
        if part.startswith("total = "):
            val_str = part[len("total = "):]
            return _parse_byte_value(val_str)
    return None


# ── Public API ───────────────────────────────────────────────────────

async def get_standalone_memory_stats() -> dict:
    """Collect memory stats using sysctl and Mach APIs.

    Uses ``sysctl -n`` for scalar values and the Mach ``host_statistics64``
    API (via ctypes) for VM page counts. This matches the Swift app's
    approach of using ``host_statistics64`` for page counts and ``sysctlbyname``
    for everything else.

    Returns a dict matching the MemoryStatsOutput schema, or a dict
    with an ``error`` key if critical values cannot be read.
    """
    mib = 1048576.0

    # Page size -- must be queried dynamically via sysctl
    page_size = await _async_sysctl_int("hw.pagesize")
    if page_size is None:
        return {"error": "Failed to query hw.pagesize via sysctl"}

    # Total physical memory via sysctl
    total_mem = await _async_sysctl_int("hw.memsize")
    if total_mem is None:
        return {"error": "Failed to query hw.memsize via sysctl"}

    # VM page counts via Mach host_statistics64 (same API as Swift app)
    vm_stats = _get_vm_stats_via_mach()
    if vm_stats is None:
        return {"error": "Failed to read VM statistics via host_statistics64"}

    free_pages = vm_stats["free"]
    active_pages = vm_stats["active"]
    inactive_pages = vm_stats["inactive"]
    wired_pages = vm_stats["wired"]

    page_size_f = float(page_size)
    free_mb = float(free_pages) * page_size_f / mib
    active_mb = float(active_pages) * page_size_f / mib
    inactive_mb = float(inactive_pages) * page_size_f / mib
    wired_mb = float(wired_pages) * page_size_f / mib

    # Compressor stats via sysctl (required -- return error if unavailable)
    compressor_bytes_used = await _async_sysctl_int("vm.compressor_bytes_used")
    compressor_compressed_bytes = await _async_sysctl_int("vm.compressor_compressed_bytes")

    if compressor_bytes_used is None:
        return {"error": "Failed to query vm.compressor_bytes_used via sysctl"}
    if compressor_compressed_bytes is None:
        return {"error": "Failed to query vm.compressor_compressed_bytes via sysctl"}

    compressed_mb = float(compressor_bytes_used) / mib
    compressor_ratio = (
        float(compressor_compressed_bytes) / float(compressor_bytes_used)
        if compressor_bytes_used > 0
        else 0.0
    )

    # Swap usage via sysctl (required -- return error if unavailable)
    swap_used_bytes = await _async_parse_swap_used()
    if swap_used_bytes is None:
        return {"error": "Failed to query vm.swapusage via sysctl"}
    swap_used_mb = swap_used_bytes / mib

    # Memory pressure level via sysctl
    pressure_level = await _async_sysctl_int("kern.memorystatus_vm_pressure_level")
    if pressure_level is None:
        pressure_level = 1  # default to normal if unavailable

    # Derived fields
    estimated_available_mb = free_mb + inactive_mb

    # Memory tier (matches Swift logic exactly)
    if pressure_level >= 4 or estimated_available_mb < 512:
        memory_tier = "critical"
    elif pressure_level >= 2 or estimated_available_mb < 1500:
        memory_tier = "constrained"
    elif estimated_available_mb < 4000:
        memory_tier = "moderate"
    elif estimated_available_mb < 8000:
        memory_tier = "comfortable"
    else:
        memory_tier = "abundant"

    total_physical_mb = float(total_mem) / mib

    return {
        "total_physical_mb": round(total_physical_mb, 2),
        "free_mb": round(free_mb, 2),
        "active_mb": round(active_mb, 2),
        "inactive_mb": round(inactive_mb, 2),
        "wired_mb": round(wired_mb, 2),
        "compressed_mb": round(compressed_mb, 2),
        "compressor_ratio": round(compressor_ratio, 4),
        "swap_used_mb": round(swap_used_mb, 2),
        "pressure_level": pressure_level,
        "memory_tier": memory_tier,
        "estimated_available_mb": round(estimated_available_mb, 2),
        "mode": "standalone",
    }


# ── Process Memory (standalone) ──────────────────────────────────────

async def get_standalone_process_memory(top_n: int, sort_by: Optional[str]) -> dict:
    """Get top processes by memory usage using ``ps``.

    Returns the standardized response envelope.
    """
    resolved_sort = sort_by if sort_by else "rss"

    # Validate sort key for standalone
    valid_keys = ["rss"]
    if resolved_sort not in valid_keys:
        return {
            "mode": "standalone",
            "capabilities": {
                "sort_by_rss": True,
                "sort_by_phys_footprint": False,
                "sort_by_pageins": False,
            },
            "data": {
                "error": f"Invalid sort_by '{resolved_sort}' for standalone mode. Valid: {valid_keys}",
                "available_sort_keys": valid_keys,
                "sort_by_applied": None,
            },
            "partial": True,
        }

    stdout, err = await _async_run(
        ["ps", "-axo", "pid=,rss=,comm="],
        timeout=5.0,
    )

    if err is not None:
        return {
            "mode": "standalone",
            "capabilities": {
                "sort_by_rss": True,
                "sort_by_phys_footprint": False,
                "sort_by_pageins": False,
            },
            "data": {
                "error": err,
                "available_sort_keys": valid_keys,
                "sort_by_applied": None,
            },
            "partial": True,
        }

    processes = parse_ps_output(stdout)
    # Sort by rss_kb descending
    processes.sort(key=lambda p: p["rss_kb"], reverse=True)
    processes = processes[:top_n]

    return {
        "mode": "standalone",
        "capabilities": {
            "sort_by_rss": True,
            "sort_by_phys_footprint": False,
            "sort_by_pageins": False,
        },
        "data": {
            "processes": processes,
            "count": len(processes),
            "sort_by_applied": resolved_sort,
            "available_sort_keys": valid_keys,
        },
        "partial": False,
    }


# ── Process Memory (app mode) ───────────────────────────────────────

def parse_app_top_processes(output: str) -> tuple[list[dict], bool, Optional[str]]:
    """Parse ``--cli top-processes`` output (TopProcessesEnvelope shape).

    Expected JSON: ``{"source": str, "partial": bool, "results": [...]}``
    Returns (processes, partial, source).
    """
    data = json.loads(output)
    source = data.get("source")
    partial = data.get("partial", False)
    results = data.get("results", [])

    processes = []
    for entry in results:
        processes.append({
            "pid": entry.get("pid", 0),
            "name": entry.get("name", ""),
            "phys_footprint_mb": round(entry.get("physFootprint", 0) / 1048576.0, 2),
            "command": entry.get("name", ""),
        })

    return processes, partial, source


async def get_app_process_memory(
    app_engine, top_n: int, sort_by: Optional[str]
) -> dict:
    """Get top processes via ``--cli top-processes``.

    Returns the standardized response envelope.
    """
    resolved_sort = sort_by if sort_by else "phys_footprint"

    valid_keys = ["phys_footprint"]
    if resolved_sort not in valid_keys:
        return {
            "mode": "app",
            "capabilities": {
                "sort_by_rss": False,
                "sort_by_phys_footprint": True,
                "sort_by_pageins": False,
            },
            "data": {
                "error": f"Invalid sort_by '{resolved_sort}' for app mode. Valid: {valid_keys}",
                "available_sort_keys": valid_keys,
                "sort_by_applied": None,
            },
            "partial": True,
        }

    stdout, err = await _async_run(
        [app_engine.binary, "--cli", "top-processes", "--top", str(top_n)],
        timeout=20.0,
    )

    if err is not None:
        return {
            "mode": "app",
            "capabilities": {
                "sort_by_rss": False,
                "sort_by_phys_footprint": True,
                "sort_by_pageins": False,
            },
            "data": {
                "error": err,
                "available_sort_keys": valid_keys,
                "sort_by_applied": None,
            },
            "partial": True,
        }

    try:
        processes, partial, source = parse_app_top_processes(stdout)
    except (json.JSONDecodeError, KeyError) as e:
        return {
            "mode": "app",
            "capabilities": {
                "sort_by_rss": False,
                "sort_by_phys_footprint": True,
                "sort_by_pageins": False,
            },
            "data": {
                "error": f"Failed to parse CLI output: {e}",
                "available_sort_keys": valid_keys,
                "sort_by_applied": None,
            },
            "partial": True,
        }

    return {
        "mode": "app",
        "capabilities": {
            "sort_by_rss": False,
            "sort_by_phys_footprint": True,
            "sort_by_pageins": False,
        },
        "data": {
            "processes": processes,
            "count": len(processes),
            "sort_by_applied": resolved_sort,
            "available_sort_keys": valid_keys,
            "source": source,
        },
        "partial": partial,
    }


# ── Compressor Health (standalone) ───────────────────────────────────

async def get_standalone_compressor_health() -> dict:
    """Get compressor health using vm_stat and sysctl with dual-sample rates.

    Takes two vm_stat samples ~1s apart to compute compression/decompression
    rates per second. Uses sysctl for compressor ratio.

    Returns the standardized response envelope.
    """
    partial = False
    errors = []

    # -- Compressor ratio from sysctl --
    compressed_bytes = await _async_sysctl_int("vm.compressor_compressed_bytes")
    bytes_used = await _async_sysctl_int("vm.compressor_bytes_used")

    if compressed_bytes is not None and bytes_used is not None and bytes_used > 0:
        ratio = round(float(compressed_bytes) / float(bytes_used), 4)
    elif bytes_used == 0:
        ratio = 0.0
    else:
        ratio = None
        partial = True
        errors.append("Failed to read compressor bytes via sysctl")

    compressed_mb = round(bytes_used / 1048576.0, 2) if bytes_used is not None else None
    original_mb = round(compressed_bytes / 1048576.0, 2) if compressed_bytes is not None else None

    # -- Dual-sample vm_stat for rates --
    sample1_stdout, err1 = await _async_run(["vm_stat"], timeout=5.0)

    await asyncio.sleep(1.0)

    sample2_stdout, err2 = await _async_run(["vm_stat"], timeout=5.0)

    compression_rate = None
    decompression_rate = None

    if err1 is None and err2 is None:
        s1 = parse_vm_stat_output(sample1_stdout)
        s2 = parse_vm_stat_output(sample2_stdout)

        compressions_1 = s1.get("compressions")
        compressions_2 = s2.get("compressions")
        decompressions_1 = s1.get("decompressions")
        decompressions_2 = s2.get("decompressions")

        if compressions_1 is not None and compressions_2 is not None:
            compression_rate = round(float(compressions_2 - compressions_1), 2)
        if decompressions_1 is not None and decompressions_2 is not None:
            decompression_rate = round(float(decompressions_2 - decompressions_1), 2)
    else:
        partial = True
        if err1:
            errors.append(f"vm_stat sample 1: {err1}")
        if err2:
            errors.append(f"vm_stat sample 2: {err2}")

    # -- Thrashing heuristic (aligned with CompressorTracker.swift) --
    thrashing = None
    if decompression_rate is not None and compression_rate is not None:
        thrashing = (
            decompression_rate > THRASHING_ABSOLUTE_MINIMUM
            and (compression_rate == 0 or decompression_rate > THRASHING_RATIO_THRESHOLD * compression_rate)
        )

    # -- Pressure level --
    pressure_raw = await _async_sysctl_int("kern.memorystatus_vm_pressure_level")
    pressure_label_map = {0: "normal", 1: "warn", 2: "critical", 4: "urgent"}
    pressure_label = pressure_label_map.get(pressure_raw, "unknown") if pressure_raw is not None else "unknown"

    data: dict = {
        "compressor_ratio": ratio,
        "compressed_mb": compressed_mb,
        "original_data_mb": original_mb,
        "compression_rate_per_sec": compression_rate,
        "decompression_rate_per_sec": decompression_rate,
        "thrashing": thrashing,
        "thrashing_sustained": None,
        "thrashing_note": "requires 30s+ sustained sampling for confirmation",
        "pressure_level": pressure_raw,
        "pressure_label": pressure_label,
        "trend": "unknown",
        "trend_note": "insufficient history for trend",
    }

    if errors:
        data["errors"] = errors

    return {
        "mode": "standalone",
        "capabilities": {
            "ratio": ratio is not None,
            "rates": compression_rate is not None,
            "thrashing_instantaneous": thrashing is not None,
            "thrashing_sustained": False,
            "trend": False,
        },
        "data": data,
        "partial": True,  # always partial: trend requires history
    }


# ── Compressor Health (app mode) ────────────────────────────────────

def parse_app_memory_stats(output: str) -> dict:
    """Parse ``--cli memory-stats`` output (SystemStatsDTO shape).

    Expected JSON fields: compressionRatio, compressions, decompressions,
    compressedBytes, compressorBytesUsed, pressureLevel, swapUsedBytes, etc.
    """
    return json.loads(output)


async def get_app_compressor_health(app_engine) -> dict:
    """Get compressor health via ``--cli memory-stats`` with dual-sample.

    Takes two CLI samples ~1s apart for rate computation.
    Returns the standardized response envelope.
    """
    partial = False
    errors = []

    # Sample 1
    s1_stdout, err1 = await _async_run(
        [app_engine.binary, "--cli", "memory-stats"],
        timeout=15.0,
    )

    await asyncio.sleep(1.0)

    # Sample 2
    s2_stdout, err2 = await _async_run(
        [app_engine.binary, "--cli", "memory-stats"],
        timeout=15.0,
    )

    if err1 is not None:
        return {
            "mode": "app",
            "capabilities": {
                "ratio": False, "rates": False,
                "thrashing_instantaneous": False,
                "thrashing_sustained": False, "trend": False,
            },
            "data": {"error": err1},
            "partial": True,
        }

    try:
        s1 = parse_app_memory_stats(s1_stdout)
    except (json.JSONDecodeError, KeyError) as e:
        return {
            "mode": "app",
            "capabilities": {
                "ratio": False, "rates": False,
                "thrashing_instantaneous": False,
                "thrashing_sustained": False, "trend": False,
            },
            "data": {"error": f"Failed to parse CLI output: {e}"},
            "partial": True,
        }

    ratio = s1.get("compressionRatio")
    compressed_bytes = s1.get("compressedBytes")
    bytes_used = s1.get("compressorBytesUsed")
    pressure_raw = s1.get("pressureLevel")
    swap_bytes = s1.get("swapUsedBytes")

    compressed_mb = round(bytes_used / 1048576.0, 2) if bytes_used is not None else None
    original_mb = round(compressed_bytes / 1048576.0, 2) if compressed_bytes is not None else None
    swap_mb = round(swap_bytes / 1048576.0, 2) if swap_bytes is not None else None

    pressure_label_map = {0: "normal", 1: "warn", 2: "critical", 4: "urgent"}
    pressure_label = pressure_label_map.get(pressure_raw, "unknown") if pressure_raw is not None else "unknown"

    # Compute rates from dual sample
    compression_rate = None
    decompression_rate = None
    if err2 is None:
        try:
            s2 = parse_app_memory_stats(s2_stdout)
            c1 = s1.get("compressions", 0)
            c2 = s2.get("compressions", 0)
            d1 = s1.get("decompressions", 0)
            d2 = s2.get("decompressions", 0)
            compression_rate = round(float(c2 - c1), 2)
            decompression_rate = round(float(d2 - d1), 2)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            partial = True
            errors.append(f"Failed to parse second sample: {e}")
    else:
        partial = True
        errors.append(f"Second sample failed: {err2}")

    # Thrashing heuristic
    thrashing = None
    if decompression_rate is not None and compression_rate is not None:
        thrashing = (
            decompression_rate > THRASHING_ABSOLUTE_MINIMUM
            and (compression_rate == 0 or decompression_rate > THRASHING_RATIO_THRESHOLD * compression_rate)
        )

    data: dict = {
        "compressor_ratio": ratio,
        "compressed_mb": compressed_mb,
        "original_data_mb": original_mb,
        "swap_used_mb": swap_mb,
        "compression_rate_per_sec": compression_rate,
        "decompression_rate_per_sec": decompression_rate,
        "thrashing": thrashing,
        "thrashing_sustained": None,
        "thrashing_note": "requires 30s+ sustained sampling for confirmation",
        "pressure_level": pressure_raw,
        "pressure_label": pressure_label,
        "trend": "unknown",
        "trend_note": "insufficient history for trend",
    }

    if errors:
        data["errors"] = errors

    return {
        "mode": "app",
        "capabilities": {
            "ratio": ratio is not None,
            "rates": compression_rate is not None,
            "thrashing_instantaneous": thrashing is not None,
            "thrashing_sustained": False,
            "trend": False,
        },
        "data": data,
        "partial": True,  # always partial: trend requires history
    }


# ── Memory Intervention ─────────────────────────────────────────────

# Canonical intervention names from INTEGRATION-PLAN.md
CANONICAL_INTERVENTIONS = [
    "purge",
    "trigger_pressure_warn",
    "reduce_transparency",
    "delete_sleepimage",
    "cleanup_snapshots",
    "flush_compositor",
]

# Intervention descriptions for dry-run responses
_INTERVENTION_DESCRIPTIONS: dict[str, str] = {
    "purge": "Flush the Unified Buffer Cache to reclaim purgeable memory",
    "trigger_pressure_warn": "Manually trigger a memory pressure warning event",
    "reduce_transparency": "Toggle macOS transparency to reduce compositor memory",
    "delete_sleepimage": "Remove /var/vm/sleepimage to reclaim disk space",
    "cleanup_snapshots": "Clean up orphaned APFS snapshots",
    "flush_compositor": "Toggle display mode to flush compositor memory",
}

# Timeouts: mode-aware per spec
_STANDALONE_PURGE_TIMEOUT = 35.0
_APP_PURGE_TIMEOUT = 40.0


def _intervention_capabilities(mode: str) -> dict[str, bool]:
    """Build the capabilities boolean map for the current mode."""
    if mode == "standalone":
        return {
            "purge": True,
            "trigger_pressure_warn": False,
            "reduce_transparency": False,
            "delete_sleepimage": False,
            "cleanup_snapshots": False,
            "flush_compositor": False,
        }
    # app mode: only purge is currently wired up
    return {
        "purge": True,
        "trigger_pressure_warn": False,
        "reduce_transparency": False,
        "delete_sleepimage": False,
        "cleanup_snapshots": False,
        "flush_compositor": False,
    }


def _intervention_envelope(mode: str, data: dict, partial: bool = False) -> dict:
    """Wrap data in the standardized response envelope."""
    return {
        "mode": mode,
        "capabilities": _intervention_capabilities(mode),
        "data": data,
        "partial": partial,
    }


async def run_standalone_intervention(
    intervention_name: str, confirm: bool
) -> dict:
    """Execute a memory intervention in standalone mode.

    Only ``purge`` is supported. All others return a structured error.
    """
    mode = "standalone"

    # Unknown intervention name
    if intervention_name not in CANONICAL_INTERVENTIONS:
        return _intervention_envelope(mode, {
            "error": "unknown_intervention",
            "available": CANONICAL_INTERVENTIONS,
            "note": f"'{intervention_name}' is not a recognized intervention name",
        })

    # Non-purge intervention in standalone
    if intervention_name != "purge":
        return _intervention_envelope(mode, {
            "error": "unsupported_in_standalone",
            "available": ["purge"],
            "note": (
                f"'{intervention_name}' requires Cacheout.app. "
                "In standalone mode, only 'purge' is available."
            ),
        })

    # Dry-run
    if not confirm:
        return _intervention_envelope(mode, {
            "dry_run": True,
            "intervention": "purge",
            "description": _INTERVENTION_DESCRIPTIONS["purge"],
            "estimated_reclaim_mb": None,
            "estimate_note": "Estimate unavailable pre-execution",
        })

    # Execute purge
    stdout, err = await _async_run(["/usr/sbin/purge"], timeout=_STANDALONE_PURGE_TIMEOUT)
    if err is not None:
        return _intervention_envelope(mode, {
            "dry_run": False,
            "intervention": "purge",
            "success": False,
            "error": err,
        })

    return _intervention_envelope(mode, {
        "dry_run": False,
        "intervention": "purge",
        "success": True,
        "result": {"note": "UBC flushed via /usr/sbin/purge"},
    })


async def run_app_intervention(
    app_engine, intervention_name: str, confirm: bool
) -> dict:
    """Execute a memory intervention in app mode.

    Only ``purge`` is currently wired (via ``--cli purge``).
    All other canonical interventions are gated with a structured message.
    """
    mode = "app"

    # Unknown intervention name
    if intervention_name not in CANONICAL_INTERVENTIONS:
        return _intervention_envelope(mode, {
            "error": "unknown_intervention",
            "available": CANONICAL_INTERVENTIONS,
            "note": f"'{intervention_name}' is not a recognized intervention name",
        })

    # Gated interventions (everything except purge)
    if intervention_name != "purge":
        return _intervention_envelope(mode, {
            "error": "unavailable",
            "note": "Requires Cacheout CLI 'intervene' command (not yet implemented)",
            "intervention": intervention_name,
        })

    # Dry-run
    if not confirm:
        return _intervention_envelope(mode, {
            "dry_run": True,
            "intervention": "purge",
            "description": _INTERVENTION_DESCRIPTIONS["purge"],
            "estimated_reclaim_mb": None,
            "estimate_note": "Estimate unavailable pre-execution",
        })

    # Execute purge via CLI
    stdout, err = await _async_run(
        [app_engine.binary, "--cli", "purge"],
        timeout=_APP_PURGE_TIMEOUT,
    )
    if err is not None:
        return _intervention_envelope(mode, {
            "dry_run": False,
            "intervention": "purge",
            "success": False,
            "error": err,
        })

    # Parse CLI output if JSON, otherwise just report success
    result: dict = {"note": "Purge executed via Cacheout CLI"}
    try:
        cli_result = json.loads(stdout)
        result.update(cli_result)
    except (json.JSONDecodeError, TypeError):
        if stdout.strip():
            result["cli_output"] = stdout.strip()

    return _intervention_envelope(mode, {
        "dry_run": False,
        "intervention": "purge",
        "success": True,
        "result": result,
    })
