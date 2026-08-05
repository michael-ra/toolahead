#!/usr/bin/env python3
"""Remove local user/group metadata from built source distributions."""

from __future__ import annotations

import argparse
import gzip
import os
from pathlib import Path
import tarfile
import tempfile


def normalize(path: Path) -> None:
    """Rewrite *path* with neutral tar ownership and a stable gzip header."""
    path = path.resolve()
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)

    try:
        with tarfile.open(path, "r:gz") as source, temporary.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as zipped:
                with tarfile.open(
                    fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT
                ) as target:
                    for member in source.getmembers():
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        payload = source.extractfile(member) if member.isfile() else None
                        target.addfile(member, payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Strip local owner metadata from Python sdist archives."
    )
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args()
    for archive in args.archives:
        normalize(archive)


if __name__ == "__main__":
    main()
