"""One immutable import boundary shared by ingest, inventory and approval scope."""
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

@dataclass(frozen=True)
class SourceSnapshot:
    root: Path
    files: object

    @property
    def hashes(self):
        return {name: hashlib.sha256(content).hexdigest() for name, content in self.files.items()}


def snapshot_input(directory, limits=None):
    if limits is None:
        from .limits import Limits
        limits = Limits()
    requested = Path(directory).absolute()
    if requested.is_symlink():
        raise ValueError('SYMLINK_REJECTED: authorized input root must not be a symbolic link')
    root = requested.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Input directory does not exist')
    entries = sorted(root.iterdir())
    # Preflight every entry (including hidden/broken/directory links) before any read.
    for entry in entries:
        if entry.is_symlink():
            raise ValueError('SYMLINK_REJECTED: input entry refused (no target read)')
    regular = [p for p in entries if p.is_file() and not p.name.startswith('.')]
    if limits:
        limits.check_files(regular)
    files = {}
    for entry in regular:
        fd = os.open(entry, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('INVALID_INPUT: regular files only')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                content = stream.read((limits.max_file_bytes + 1) if limits else -1)
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('SOURCE_CHANGED: file changed during snapshot')
            if limits:
                limits.check_file_bytes(content)
            if limits:
                limits.check_total_bytes(sum(map(len, files.values())) + len(content))
            files[entry.name] = content
        finally:
            os.close(fd)
    if limits:
        limits.check_total_bytes(sum(map(len, files.values())))
    return SourceSnapshot(root, MappingProxyType(files))
