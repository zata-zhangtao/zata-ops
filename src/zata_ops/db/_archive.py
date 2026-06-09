"""File archiving utilities with incremental backup support via GNU tar."""

from __future__ import annotations

import subprocess
from pathlib import Path


def archive_directory(
    source_dir: Path,
    output_path: Path,
    snapshot_file: Path,
    is_full: bool,
) -> None:
    """Create a ``tar.gz`` archive of a directory with incremental support.

    Uses GNU tar's ``--listed-incremental`` mechanism:

    - Full backup resets the snapshot file and archives everything.
    - Incremental backup archives only files changed since the last snapshot.

    Args:
        source_dir: Directory to archive.
        output_path: Path to write the resulting ``.tar.gz`` file.
        snapshot_file: Path to the tar snapshot file that tracks incremental state.
        is_full: If ``True``, reset the snapshot and perform a full backup.

    Raises:
        subprocess.CalledProcessError: If GNU tar exits non-zero.
    """
    if is_full and snapshot_file.exists():
        snapshot_file.unlink()

    tar_cmd_list: list[str] = [
        "tar",
        "-czf",
        str(output_path),
        "--listed-incremental",
        str(snapshot_file),
        str(source_dir),
    ]
    subprocess.run(tar_cmd_list, check=True)


def restore_archive(
    archive_path: Path, target_dir: str, use_incremental: bool = False
) -> None:
    """Extract a ``tar.gz`` archive into ``target_dir``.

    Args:
        archive_path: Path to the ``.tar.gz`` archive.
        target_dir: Directory to extract into.
        use_incremental: If ``True``, pass ``-G`` so GNU tar honours incremental
            deletion metadata.

    Raises:
        subprocess.CalledProcessError: If GNU tar exits non-zero.
    """
    target_path = Path(target_dir)
    target_path.mkdir(parents=True, exist_ok=True)

    tar_cmd_list: list[str] = ["tar"]
    if use_incremental:
        tar_cmd_list.append("-G")
    tar_cmd_list.extend(["-xzf", str(archive_path), "-C", str(target_path)])

    subprocess.run(tar_cmd_list, check=True)
