"""Locate TechMC Python source, project-owned assets, and installed environment data.

SOURCE_ROOT owns the Python package. ASSET_ROOT owns submitted tasks,
schemas, bridge sources, and public procedures inside the project.
DATA_ROOT selects installed worlds, runtime files, knowledge data, and
environment logs through runtime.storage's configured local data root.
"""

from pathlib import Path
from runtime.storage import DATA_ROOT as STORAGE_ROOT

SOURCE_ROOT = Path(__file__).resolve().parent
RESEARCH_ROOT = SOURCE_ROOT.parents[1]
ASSET_ROOT = RESEARCH_ROOT / 'assets/techmc'
DATA_ROOT = STORAGE_ROOT / 'techmc'
