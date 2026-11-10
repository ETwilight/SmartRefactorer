"""Windows console policy for a noninteractive host process and its children.

Enabled only by background CLI entry points. This changes process creation
flags in the current interpreter; it neither edits shared environment code nor
touches another experiment's running process, windows, streams or return codes.
"""

import os
import subprocess

_OriginalPopen = subprocess.Popen


class _BackgroundPopen(_OriginalPopen):
    def __init__(self, *args, **kwargs):
        if os.name == 'nt':
            flags = kwargs.get('creationflags', 0)
            explicit_console = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS
            if not flags & explicit_console:
                kwargs['creationflags'] = flags | subprocess.CREATE_NO_WINDOW
        super().__init__(*args, **kwargs)


def enable_background_children():
    """Keep SSH/preflight/knowledge children hidden in this background host only."""
    if os.name == 'nt' and subprocess.Popen is _OriginalPopen:
        subprocess.Popen = _BackgroundPopen
