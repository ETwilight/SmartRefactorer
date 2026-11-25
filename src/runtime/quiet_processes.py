"""Configure child processes of background Windows commands to start with hidden consoles."""

import os
import subprocess

_OriginalPopen = subprocess.Popen


class _BackgroundPopen(_OriginalPopen):
    """Add CREATE_NO_WINDOW to Windows child processes using the standard Popen API.

    The inherited process object retains the standard Popen state and methods.
    __init__ combines creationflags with CREATE_NO_WINDOW unless the caller
    selects CREATE_NEW_CONSOLE or DETACHED_PROCESS. Other platforms pass all
    arguments directly to the original Popen implementation.

    Example:
        enable_background_children()
        process = subprocess.Popen(command)
        code = process.wait()

    On Windows the child starts with the configured console flags, and code is
    its exit status. The replacement applies within the current interpreter.
    """

    def __init__(self, *args, **kwargs):
        """Add the background console flag when applicable, then initialize the child
        process.
        """

        if os.name == 'nt':
            flags = kwargs.get('creationflags', 0)
            explicit_console = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS
            if not flags & explicit_console:
                kwargs['creationflags'] = flags | subprocess.CREATE_NO_WINDOW
        super().__init__(*args, **kwargs)


def enable_background_children():
    """Install the background Popen subclass once in the current Windows interpreter."""
    if os.name == 'nt' and subprocess.Popen is _OriginalPopen:
        subprocess.Popen = _BackgroundPopen
