"""Stable installed assets and separate executable source ownership."""

from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent
RESEARCH_ROOT = SOURCE_ROOT.parents[1]
ASSET_ROOT = RESEARCH_ROOT / "assets/techmc"
