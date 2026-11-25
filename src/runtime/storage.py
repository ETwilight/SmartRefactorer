"""Select directories for run data, models, and archives from local storage settings."""

import json
import os
from pathlib import Path

RESEARCH_ROOT = Path(__file__).resolve().parents[1]


def data_root():
    """Read the environment override or machine setting, falling back to the user data
    directory.
    """
    configured = os.environ.get('SMARTREFACTORER_DATA_ROOT')
    settings = RESEARCH_ROOT / 'local-storage.json'
    if configured is None and settings.is_file():
        configured = json.loads(settings.read_text(encoding='utf-8'))['data_root']
    return Path(configured).expanduser() if configured else Path.home() / 'SmartRefactorerData'


DATA_ROOT = data_root()
RESULTS_ROOT = DATA_ROOT / 'results'
MODELS_ROOT = DATA_ROOT / 'models'
ARCHIVE_ROOT = DATA_ROOT / 'archive'
