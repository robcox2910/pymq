"""Safe saving -- never leave a half-written file behind.

The usual way to save a file is: open it, wipe it, write the new
contents. If the computer crashes after the wipe but before the writing
finishes, your data is gone -- the old version was wiped and the new one
never arrived.

So we save like a careful student copying out their homework:

1. Write the new version on a **fresh sheet** (a temporary file sitting
   right next to the real one).
2. Make sure the **ink is dry** -- ``fsync`` forces the bytes out of the
   computer's memory and onto the disk.
3. **Swap** the fresh sheet in for the old one in a single instant step
   (``Path.replace``). There is never a moment with no complete file: you
   see either the old version or the new one, never half of each.

Real databases (SQLite, PostgreSQL, Redis) all use this same trick.
"""

import contextlib
import os
import stat
import tempfile
from pathlib import Path

# Permissions for a brand-new file: owner can read/write, others can read.
_NEW_FILE_MODE = 0o644


def atomic_write_text(path: Path, text: str) -> None:
    """Replace the file at *path* with *text*, so a crash can never leave it half-written.

    Args:
        path: The file to create or replace (its folder must already exist).
        text: The new contents, saved as UTF-8.

    """
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else _NEW_FILE_MODE
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.chmod(mode)
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)  # don't leave the scrap sheet lying around
        raise
    _sync_folder(path.parent)


def _sync_folder(folder: Path) -> None:
    """Make the swap itself durable by syncing the folder's list of files.

    Some systems (like Windows) can't sync a folder; there the swap is
    still safe, just not guaranteed to survive a power cut.
    """
    with contextlib.suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
