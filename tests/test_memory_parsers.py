"""Golden-file style tests for ps, vm_stat, and sysctl parsers + capability gating."""

from __future__ import annotations

import asyncio
import json
import pytest

from cacheout_mcp.memory_tools import (
    THRASHING_ABSOLUTE_MINIMUM,
    THRASHING_RATIO_THRESHOLD,
    parse_app_memory_stats,
    parse_app_top_processes,
    parse_ps_output,
    parse_vm_stat_output,
)
from cacheout_mcp.memory_models import GetCompressorHealthInput, GetProcessMemoryInput


# ── ps parser tests ─────────────────────────────────────────────────

PS_GOLDEN = """\
  501 123456 /Applications/Safari.app/Contents/MacOS/Safari
  502  98765 /usr/sbin/cfprefsd
  503  45678 /System/Library/CoreServices/Finder.app/Contents/MacOS/Finder
  504      0 /usr/sbin/syslogd
"""

PS_EMPTY = ""
PS_MALFORMED = """\
not a valid line
abc def ghi
123
"""


class TestPsParser:
    def test_golden_output(self):
        result = parse_ps_output(PS_GOLDEN)
        assert len(result) == 4

        # First process
        assert result[0]["pid"] == 501
        assert result[0]["rss_kb"] == 123456
        assert result[0]["rss_mb"] == round(123456 / 1024.0, 2)
        assert result[0]["command"] == "/Applications/Safari.app/Contents/MacOS/Safari"
        assert "RSS-based estimate" in result[0]["note"]

        # Zero RSS process
        assert result[3]["rss_kb"] == 0
        assert result[3]["rss_mb"] == 0.0

    def test_empty_output(self):
        result = parse_ps_output(PS_EMPTY)
        assert result == []

    def test_malformed_lines_skipped(self):
        result = parse_ps_output(PS_MALFORMED)
        assert result == []

    def test_rss_mb_precision(self):
        result = parse_ps_output("  1 1025 /bin/test\n")
        assert result[0]["rss_mb"] == round(1025 / 1024.0, 2)


# ── vm_stat parser tests ────────────────────────────────────────────

VM_STAT_GOLDEN = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                3456.
Pages active:                             45678.
Pages inactive:                           12345.
Pages speculative:                         1234.
Pages throttled:                              0.
Pages wired down:                         23456.
Pages purgeable:                           5678.
"Translation faults":                 987654321.
Pages copy-on-write:                   12345678.
Pages zero filled:                    234567890.
Pages reactivated:                      1234567.
Pages purged:                            234567.
File-backed pages:                        34567.
Anonymous pages:                          23456.
Pages stored in compressor:              123456.
Pages occupied by compressor:             34567.
Decompressions:                         5678901.
Compressions:                           4567890.
Pageins:                                 123456.
Pageouts:                                  1234.
Swapins:                                    123.
Swapouts:                                   456.
"""


class TestVmStatParser:
    def test_golden_output(self):
        result = parse_vm_stat_output(VM_STAT_GOLDEN)

        assert result["pages_free"] == 3456
        assert result["pages_active"] == 45678
        assert result["pages_inactive"] == 12345
        assert result["pages_wired_down"] == 23456
        assert result["compressions"] == 4567890
        assert result["decompressions"] == 5678901
        assert result["pages_stored_in_compressor"] == 123456
        assert result["pageins"] == 123456
        assert result["pageouts"] == 1234

    def test_empty_output(self):
        result = parse_vm_stat_output("")
        assert result == {}

    def test_header_line_skipped(self):
        result = parse_vm_stat_output("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n")
        # Header has a colon but value isn't a number — should be skipped
        assert "mach_virtual_memory_statistics" not in result

    def test_trailing_dots_stripped(self):
        result = parse_vm_stat_output("Pages free:    100.\n")
        assert result["pages_free"] == 100


# ── sysctl parser tests ─────────────────────────────────────────────

class TestSysctlParser:
    def test_parse_app_memory_stats_golden(self):
        """Test parsing SystemStatsDTO JSON from --cli memory-stats."""
        golden = json.dumps({
            "compressionRatio": 2.5,
            "compressions": 1000000,
            "decompressions": 500000,
            "compressedBytes": 536870912,
            "compressorBytesUsed": 214748365,
            "pressureLevel": 1,
            "swapUsedBytes": 104857600,
        })
        result = parse_app_memory_stats(golden)
        assert result["compressionRatio"] == 2.5
        assert result["compressions"] == 1000000
        assert result["decompressions"] == 500000
        assert result["pressureLevel"] == 1

    def test_parse_app_memory_stats_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            parse_app_memory_stats("not json")


# ── App top-processes parser tests ──────────────────────────────────

class TestAppTopProcessesParser:
    def test_golden_output(self):
        golden = json.dumps({
            "source": "ProcessMemoryScanner",
            "partial": False,
            "results": [
                {"pid": 100, "name": "Safari", "physFootprint": 524288000},
                {"pid": 200, "name": "Xcode", "physFootprint": 209715200},
            ],
        })
        processes, partial, source = parse_app_top_processes(golden)
        assert len(processes) == 2
        assert processes[0]["pid"] == 100
        assert processes[0]["phys_footprint_mb"] == round(524288000 / 1048576.0, 2)
        assert processes[0]["name"] == "Safari"
        assert source == "ProcessMemoryScanner"
        assert partial is False

    def test_partial_flag(self):
        golden = json.dumps({
            "source": "fallback",
            "partial": True,
            "results": [],
        })
        processes, partial, source = parse_app_top_processes(golden)
        assert processes == []
        assert partial is True

    def test_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            parse_app_top_processes("not json {")


# ── Capability gating tests ─────────────────────────────────────────

class TestCapabilityGating:
    def test_standalone_capabilities(self):
        """Standalone mode: sort_by_rss=true, others false."""
        result = asyncio.run(_mock_standalone_process_memory_capabilities())
        caps = result["capabilities"]
        assert caps["sort_by_rss"] is True
        assert caps["sort_by_phys_footprint"] is False
        assert caps["sort_by_pageins"] is False

    def test_standalone_invalid_sort_key(self):
        """Standalone rejects phys_footprint as sort key."""
        result = asyncio.run(_mock_standalone_process_memory_invalid_sort())
        assert result["partial"] is True
        assert "error" in result["data"]
        assert result["capabilities"]["sort_by_phys_footprint"] is False

    def test_pageins_always_gated(self):
        """sort_by_pageins is false in all modes."""
        result = asyncio.run(_mock_standalone_process_memory_capabilities())
        assert result["capabilities"]["sort_by_pageins"] is False

    def test_process_memory_input_rejects_extra_fields(self):
        """ConfigDict(extra='forbid') rejects unknown fields."""
        with pytest.raises(Exception):
            GetProcessMemoryInput(top_n=5, sort_by="rss", unknown_field="bad")

    def test_compressor_health_input_rejects_extra_fields(self):
        with pytest.raises(Exception):
            GetCompressorHealthInput(unknown_field="bad")


# ── Thrashing heuristic tests ───────────────────────────────────────

class TestThrashingHeuristic:
    def test_thrashing_detected(self):
        """decompression > 100 AND decompression > 2x compression => thrashing."""
        decompression_rate = 250.0
        compression_rate = 50.0
        assert decompression_rate > THRASHING_ABSOLUTE_MINIMUM
        assert decompression_rate > THRASHING_RATIO_THRESHOLD * compression_rate

    def test_no_thrashing_low_rate(self):
        """decompression < 100 => no thrashing even if ratio is high."""
        decompression_rate = 50.0
        compression_rate = 10.0
        assert not (decompression_rate > THRASHING_ABSOLUTE_MINIMUM)

    def test_no_thrashing_balanced_ratio(self):
        """decompression > 100 but ratio <= 2x => no thrashing."""
        decompression_rate = 150.0
        compression_rate = 100.0
        assert decompression_rate > THRASHING_ABSOLUTE_MINIMUM
        assert not (decompression_rate > THRASHING_RATIO_THRESHOLD * compression_rate)

    def test_thrashing_constants_match_swift(self):
        """Verify constants match CompressorTracker.swift values."""
        assert THRASHING_ABSOLUTE_MINIMUM == 100.0
        assert THRASHING_RATIO_THRESHOLD == 2.0


# ── Helper mocks for capability tests ───────────────────────────────

async def _mock_standalone_process_memory_capabilities() -> dict:
    """Return capability structure without actually running ps."""
    from cacheout_mcp.memory_tools import get_standalone_process_memory
    from unittest.mock import AsyncMock, patch

    mock_output = "  1 100 /bin/test\n  2 200 /bin/test2\n"
    with patch("cacheout_mcp.memory_tools._async_run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (mock_output, None)
        return await get_standalone_process_memory(top_n=5, sort_by=None)


async def _mock_standalone_process_memory_invalid_sort() -> dict:
    """Test standalone with invalid sort key."""
    from cacheout_mcp.memory_tools import get_standalone_process_memory
    return await get_standalone_process_memory(top_n=5, sort_by="phys_footprint")
