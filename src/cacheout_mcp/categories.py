"""Cache category definitions — mirrors Cacheout.app's Categories.swift."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List


class RiskLevel(str, Enum):
    SAFE = "safe"
    REVIEW = "review"
    CAUTION = "caution"


@dataclass(frozen=True)
class CacheCategory:
    """A scannable/cleanable cache location on macOS."""

    name: str
    slug: str  # machine-friendly ID, e.g. "xcode_derived_data"
    description: str
    paths: tuple[str, ...]  # relative to ~
    risk_level: RiskLevel
    rebuild_note: str
    default_selected: bool = True
    # Priority for smart_clean (lower = cleared first)
    clean_priority: int = 50
    # Optional shell command for cleanup (runs instead of file deletion)
    clean_command: str | None = None

    @property
    def resolved_paths(self) -> List[Path]:
        """Return only paths that actually exist on this system."""
        home = Path.home()
        results = []
        for p in self.paths:
            full = home / p
            if full.exists() and full.is_dir():
                results.append(full)
        return results


# ── Category Registry ────────────────────────────────────────────────
# Ordered by clean_priority (lowest = safest to nuke first)

ALL_CATEGORIES: list[CacheCategory] = [
    CacheCategory(
        name="Xcode DerivedData",
        slug="xcode_derived_data",
        description="Build artifacts and indexes. Xcode rebuilds automatically.",
        paths=("Library/Developer/Xcode/DerivedData",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="Xcode rebuilds on next build",
        clean_priority=10,
    ),
    CacheCategory(
        name="Homebrew Cache",
        slug="homebrew_cache",
        description="Downloaded formula bottles and source tarballs.",
        paths=("Library/Caches/Homebrew",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="Equivalent to 'brew cleanup'",
        clean_priority=15,
    ),
    CacheCategory(
        name="npm Cache",
        slug="npm_cache",
        description="Cached npm packages. Re-downloads on next install.",
        paths=(".npm/_cacache",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="npm re-downloads packages as needed",
        clean_priority=20,
    ),
    CacheCategory(
        name="Yarn Cache",
        slug="yarn_cache",
        description="Cached Yarn packages and metadata.",
        paths=("Library/Caches/Yarn",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="Yarn re-downloads packages as needed",
        clean_priority=20,
    ),
    CacheCategory(
        name="pnpm Store",
        slug="pnpm_store",
        description="Content-addressable pnpm package store.",
        paths=("Library/pnpm/store",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="pnpm re-downloads packages as needed",
        clean_priority=20,
    ),
    CacheCategory(
        name="Playwright Browsers",
        slug="playwright_browsers",
        description="Downloaded browser binaries for Playwright testing.",
        paths=("Library/Caches/ms-playwright", "Library/Caches/ms-playwright-go"),
        risk_level=RiskLevel.SAFE,
        rebuild_note="Reinstall with 'npx playwright install'",
        clean_priority=25,
    ),
    CacheCategory(
        name="CocoaPods Cache",
        slug="cocoapods_cache",
        description="Cached CocoaPods specs and downloaded pods.",
        paths=("Library/Caches/CocoaPods",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="'pod install' re-downloads as needed",
        clean_priority=25,
    ),
    CacheCategory(
        name="Swift PM Cache",
        slug="swift_pm_cache",
        description="Swift Package Manager resolved packages.",
        paths=("Library/Caches/org.swift.swiftpm",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="SPM re-resolves on next build",
        clean_priority=25,
    ),
    CacheCategory(
        name="Gradle Cache",
        slug="gradle_cache",
        description="Gradle build cache and downloaded dependencies.",
        paths=(".gradle/caches",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="Gradle re-downloads on next build",
        clean_priority=30,
    ),
    CacheCategory(
        name="pip Cache",
        slug="pip_cache",
        description="Cached Python packages from pip installs.",
        paths=("Library/Caches/pip", "Library/Caches/pip-tools"),
        risk_level=RiskLevel.SAFE,
        rebuild_note="pip re-downloads packages as needed",
        clean_priority=30,
    ),
    CacheCategory(
        name="Browser Caches",
        slug="browser_caches",
        description="Cached web content from Brave, Chrome, and other browsers.",
        paths=(
            "Library/Caches/BraveSoftware",
            "Library/Caches/Google",
            "Library/Caches/com.brave.Browser",
            "Library/Caches/com.google.Chrome",
        ),
        risk_level=RiskLevel.REVIEW,
        rebuild_note="Browsers rebuild caches as you browse",
        clean_priority=40,
    ),
    CacheCategory(
        name="VS Code Cache",
        slug="vscode_cache",
        description="VS Code update downloads and extension cache.",
        paths=(
            "Library/Caches/com.microsoft.VSCode.ShipIt",
            "Library/Caches/com.microsoft.VSCode",
        ),
        risk_level=RiskLevel.SAFE,
        rebuild_note="VS Code re-downloads as needed",
        clean_priority=35,
    ),
    CacheCategory(
        name="Electron Cache",
        slug="electron_cache",
        description="Shared Electron framework cache used by Electron-based apps.",
        paths=("Library/Caches/electron",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="Re-downloads when Electron apps need it",
        clean_priority=35,
    ),
    CacheCategory(
        name="Xcode Device Support",
        slug="xcode_device_support",
        description="Debug symbols for connected iOS devices.",
        paths=("Library/Developer/Xcode/iOS DeviceSupport",),
        risk_level=RiskLevel.REVIEW,
        rebuild_note="Re-downloads when you connect a device",
        default_selected=True,
        clean_priority=45,
    ),
    CacheCategory(
        name="Docker Disk Image",
        slug="docker_disk",
        description="Docker's virtual disk. Contains all images, containers, and volumes.",
        paths=(
            "Library/Containers/com.docker.docker/Data/vms/0/data",
            "Library/Containers/com.docker.docker/Data",
        ),
        risk_level=RiskLevel.CAUTION,
        rebuild_note="Run 'docker system prune -a' first, or delete to reset Docker completely",
        default_selected=False,
        clean_priority=90,
    ),
    # ── New categories from v2 hybrid discovery ──────────────────────
    CacheCategory(
        name="Simulator Devices",
        slug="simulator_devices",
        description="iOS/watchOS/tvOS simulator data and device states. Can be several GB.",
        paths=("Library/Developer/CoreSimulator/Devices",),
        risk_level=RiskLevel.REVIEW,
        rebuild_note="Recreated when you use Simulator. Run 'xcrun simctl delete unavailable' for targeted cleanup.",
        default_selected=False,
        clean_priority=50,
        clean_command="xcrun simctl shutdown all 2>/dev/null; xcrun simctl delete unavailable 2>/dev/null; xcrun simctl erase all 2>/dev/null",
    ),
    CacheCategory(
        name="Bun Cache",
        slug="bun_cache",
        description="Bun package manager install cache.",
        paths=(".bun/install/cache",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="Bun re-downloads packages as needed",
        clean_priority=20,
    ),
    CacheCategory(
        name="node-gyp Cache",
        slug="node_gyp_cache",
        description="Native Node.js addon build headers and artifacts.",
        paths=("Library/Caches/node-gyp",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="Re-downloads when native modules are built",
        clean_priority=25,
    ),
    CacheCategory(
        name="uv Cache",
        slug="uv_cache",
        description="Fast Python package installer cache. Can grow large with many environments.",
        paths=(".cache/uv",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="uv re-downloads packages as needed. Clean with 'uv cache clean'.",
        clean_priority=15,
    ),
    CacheCategory(
        name="PyTorch Hub Models",
        slug="torch_hub",
        description="Downloaded PyTorch models and datasets.",
        paths=(".cache/torch",),
        risk_level=RiskLevel.REVIEW,
        rebuild_note="Models re-download on next use (can be slow for large models)",
        default_selected=False,
        clean_priority=55,
    ),
    CacheCategory(
        name="ChatGPT Desktop Cache",
        slug="chatgpt_desktop_cache",
        description="OpenAI ChatGPT desktop app cache.",
        paths=("Library/Caches/com.openai.atlas",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="ChatGPT re-creates cache on next launch",
        clean_priority=30,
    ),
    CacheCategory(
        name="Prisma Engines",
        slug="prisma_engines",
        description="Prisma ORM query engine binaries.",
        paths=(".cache/prisma",),
        risk_level=RiskLevel.SAFE,
        rebuild_note="Re-downloads on next 'prisma generate'",
        clean_priority=25,
    ),
    CacheCategory(
        name="TypeScript Build Cache",
        slug="typescript_cache",
        description="TypeScript compiler disk cache and Next.js SWC binaries.",
        paths=("Library/Caches/typescript", "Library/Caches/next-swc"),
        risk_level=RiskLevel.SAFE,
        rebuild_note="Regenerated on next build",
        clean_priority=20,
    ),
]

CATEGORY_MAP: dict[str, CacheCategory] = {c.slug: c for c in ALL_CATEGORIES}
