"""Files only the user may read: tokens, logs that contain URLs with tokens, config."""

from __future__ import annotations

import os
from pathlib import Path


def open_private(path: Path) -> int:
    """Open `path` for writing, truncated, and return the fd.

    The mode is 0600 before anything is written, even when the file already existed with a
    wider mode, so a token written next is never readable by others.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd
