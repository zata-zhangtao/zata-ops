"""Low-level database dump and restore helpers shared by db commands.

Wraps ``pg_dump`` / ``psql`` for PostgreSQL and ``sqlite3`` for SQLite. These
helpers exist as pure functions so the typer wrapper layer can ``--dry-run``
them without ever shelling out.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

_GZIP_MAGIC = b"\x1f\x8b"
_GZIP_DEFLATE_MAGIC = b"\x1f\x8b\x08"
_LEGACY_GZIP_FILENAME_MARKERS = (b"database.sql", b"database.sql.gz")
_INCOMPATIBLE_SET_PREFIXES = (b"SET transaction_timeout",)
_POSTGRES_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_sqlite_path(db_url: str) -> Path:
    """Extract the filesystem path from a SQLite URL.

    Supports ``sqlite:///absolute/path`` and ``sqlite://relative/path``.

    Args:
        db_url: SQLite database URL.

    Returns:
        Filesystem path to the SQLite database file.
    """
    parsed_db_url = urlparse(db_url)
    if parsed_db_url.path:
        normalized_db_path = parsed_db_url.path
        if parsed_db_url.netloc:
            normalized_db_path = "/" + parsed_db_url.netloc + normalized_db_path
        return Path(normalized_db_path)
    return Path(db_url.replace("sqlite://", ""))


def parse_postgres_url(db_url: str) -> dict[str, str | int]:
    """Parse a PostgreSQL connection URL into its component fields.

    Args:
        db_url: PostgreSQL connection URL.

    Returns:
        Mapping with keys ``host``, ``port``, ``database``, ``user``, ``password``.
    """
    parsed_db_url = urlparse(db_url)
    return {
        "host": parsed_db_url.hostname or "localhost",
        "port": parsed_db_url.port or 5432,
        "database": parsed_db_url.path.lstrip("/")
        if parsed_db_url.path
        else "postgres",
        "user": parsed_db_url.username or "postgres",
        "password": parsed_db_url.password or "",
    }


def _build_psql_env(password: str) -> dict[str, str]:
    """Build an environment dictionary with ``PGPASSWORD`` injected when given.

    Args:
        password: Optional password for ``PGPASSWORD``.

    Returns:
        Copy of ``os.environ`` plus ``PGPASSWORD`` if a password was provided.
    """
    runtime_env = os.environ.copy()
    if password:
        runtime_env["PGPASSWORD"] = password
    return runtime_env


def backup_database(db_url: str, output_path: Path) -> None:
    """Back up a database to a gzipped SQL file.

    Args:
        db_url: Database URL (``sqlite://`` or ``postgresql://``).
        output_path: Path to write the gzipped backup file.

    Raises:
        ValueError: If the database scheme is not supported.
        FileNotFoundError: If a SQLite database file does not exist.
        subprocess.CalledProcessError: If the underlying dump command fails.
    """
    db_url_lower: str = db_url.lower()
    if "sqlite" in db_url_lower:
        _backup_sqlite(db_url, output_path)
    elif "postgres" in db_url_lower:
        _backup_postgres(db_url, output_path)
    else:
        raise ValueError(f"Unsupported database scheme in URL: {db_url}")


def _backup_sqlite(db_url: str, output_path: Path) -> None:
    """Dump a SQLite database via ``sqlite3 .dump`` into a gzipped file."""
    sqlite_db_path = parse_sqlite_path(db_url)
    if not sqlite_db_path.exists():
        raise FileNotFoundError(f"SQLite database file not found: {sqlite_db_path}")

    completed_dump_proc = subprocess.run(
        ["sqlite3", str(sqlite_db_path), ".dump"],
        capture_output=True,
        text=True,
        check=True,
    )
    with gzip.open(output_path, "wt", encoding="utf-8") as gzip_writer:
        gzip_writer.write(completed_dump_proc.stdout)


def _backup_postgres(db_url: str, output_path: Path) -> None:
    """Dump a PostgreSQL database via ``pg_dump`` into a gzipped file."""
    parsed_postgres_url = parse_postgres_url(db_url)
    pg_dump_cmd_list: list[str] = [
        "pg_dump",
        "-h",
        str(parsed_postgres_url["host"]),
        "-p",
        str(parsed_postgres_url["port"]),
        "-U",
        str(parsed_postgres_url["user"]),
        "-d",
        str(parsed_postgres_url["database"]),
        "--encoding=UTF8",
        "-F",
        "p",
    ]
    runtime_env = _build_psql_env(str(parsed_postgres_url["password"]))
    runtime_env["PGCLIENTENCODING"] = "UTF8"

    with gzip.open(output_path, "wb") as gzip_writer:
        pg_dump_proc = subprocess.Popen(
            pg_dump_cmd_list, env=runtime_env, stdout=subprocess.PIPE
        )
        if pg_dump_proc.stdout is None:
            raise RuntimeError("pg_dump stdout pipe was not created")
        with pg_dump_proc.stdout:
            shutil.copyfileobj(pg_dump_proc.stdout, gzip_writer)
        pg_dump_return_code = pg_dump_proc.wait()
        if pg_dump_return_code != 0:
            raise subprocess.CalledProcessError(pg_dump_return_code, pg_dump_cmd_list)


def restore_database(
    db_url: str,
    sql_gz_path: Path,
    sanitize_invalid_utf8: bool = False,
) -> None:
    """Restore a database from a gzipped SQL dump.

    Args:
        db_url: Target database URL.
        sql_gz_path: Path to the ``database.sql.gz`` file.
        sanitize_invalid_utf8: Replace invalid UTF-8 bytes with the Unicode
            replacement character before piping the dump to PostgreSQL.

    Raises:
        ValueError: If the database scheme is not supported.
        subprocess.CalledProcessError: If the restore command fails.
    """
    db_url_lower: str = db_url.lower()
    if "sqlite" in db_url_lower:
        _restore_sqlite(db_url, sql_gz_path)
    elif "postgres" in db_url_lower:
        _restore_postgres(
            db_url, sql_gz_path, sanitize_invalid_utf8=sanitize_invalid_utf8
        )
    else:
        raise ValueError(f"Unsupported database scheme: {db_url}")


def _restore_sqlite(db_url: str, sql_gz_path: Path) -> None:
    """Restore a SQLite database by piping SQL into ``sqlite3``."""
    sqlite_db_path = parse_sqlite_path(db_url)
    sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
    _pipe_gzip_to_command(sql_gz_path, ["sqlite3", str(sqlite_db_path)])


def _restore_postgres(
    db_url: str,
    sql_gz_path: Path,
    sanitize_invalid_utf8: bool = False,
) -> None:
    """Restore a PostgreSQL database by piping SQL into ``psql``."""
    parsed_postgres_url = parse_postgres_url(db_url)
    runtime_env = _build_psql_env(str(parsed_postgres_url["password"]))

    try:
        if shutil.which("createdb"):
            subprocess.run(
                [
                    "createdb",
                    "-h",
                    str(parsed_postgres_url["host"]),
                    "-p",
                    str(parsed_postgres_url["port"]),
                    "-U",
                    str(parsed_postgres_url["user"]),
                    str(parsed_postgres_url["database"]),
                ],
                env=runtime_env,
                capture_output=True,
                check=True,
            )
        else:
            subprocess.run(
                [
                    "psql",
                    "-b",
                    "-h",
                    str(parsed_postgres_url["host"]),
                    "-p",
                    str(parsed_postgres_url["port"]),
                    "-U",
                    str(parsed_postgres_url["user"]),
                    "-d",
                    "postgres",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    f"CREATE DATABASE {parsed_postgres_url['database']}",
                ],
                env=runtime_env,
                capture_output=True,
                check=True,
            )
    except subprocess.CalledProcessError:
        pass

    _pipe_gzip_to_command(
        sql_gz_path,
        [
            "psql",
            "-b",
            "-v",
            "ON_ERROR_STOP=1",
            "--single-transaction",
            "-h",
            str(parsed_postgres_url["host"]),
            "-p",
            str(parsed_postgres_url["port"]),
            "-U",
            str(parsed_postgres_url["user"]),
            "-d",
            str(parsed_postgres_url["database"]),
        ],
        env=runtime_env,
        sanitize_invalid_utf8=sanitize_invalid_utf8,
    )


def _sanitize_invalid_utf8_line(line: bytes) -> tuple[bytes, bool]:
    """Replace invalid UTF-8 bytes in a single dump line.

    Args:
        line: Raw dump line bytes.

    Returns:
        Tuple of (possibly-rewritten line, whether replacement occurred).
    """
    try:
        line.decode("utf-8")
        return line, False
    except UnicodeDecodeError:
        return line.decode("utf-8", errors="replace").encode("utf-8"), True


def find_legacy_gzip_tail_index(line: bytes) -> int:
    """Return the index where malformed legacy gzip tail bytes begin.

    Args:
        line: Raw dump line bytes from a non-gzipped (legacy) ``.sql.gz`` file.

    Returns:
        Byte index where the legacy gzip tail begins, or ``-1`` if no tail.
    """
    gzip_tail_index = line.find(_GZIP_DEFLATE_MAGIC)
    if gzip_tail_index >= 0:
        return gzip_tail_index

    for legacy_filename_marker in _LEGACY_GZIP_FILENAME_MARKERS:
        marker_index = line.find(legacy_filename_marker)
        if marker_index > 0:
            prefix_bytes = line[:marker_index]
            if not prefix_bytes.decode("utf-8", errors="ignore").strip():
                return 0
            try:
                prefix_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return 0

    return -1


def _pipe_gzip_to_command(
    sql_gz_path: Path,
    cmd: list[str],
    env: dict[str, str] | None = None,
    sanitize_invalid_utf8: bool = False,
) -> None:
    """Stream a SQL dump into a subprocess via stdin.

    Supports both gzipped (``.sql.gz``) and plain SQL files by sniffing the
    gzip magic header. Also skips ``SET`` statements that reference parameters
    which may not exist on the target PostgreSQL instance.

    Args:
        sql_gz_path: Path to the SQL dump (gzipped or plain).
        cmd: Command list to pipe the dump into via stdin.
        env: Optional environment for the subprocess.
        sanitize_invalid_utf8: When True, replace invalid UTF-8 bytes before
            writing to stdin.

    Raises:
        subprocess.CalledProcessError: If the receiving command exits non-zero.
    """
    with open(sql_gz_path, "rb") as magic_probe_file:
        magic_header_bytes = magic_probe_file.read(2)

    is_gzipped_dump = magic_header_bytes == _GZIP_MAGIC
    dump_reader = (
        gzip.open(sql_gz_path, "rb") if is_gzipped_dump else open(sql_gz_path, "rb")
    )

    pipe_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=env)
    invalid_utf8_replacement_count = 0
    embedded_gzip_tail_skipped = False
    try:
        for raw_dump_line in dump_reader:
            stop_after_line = False
            current_dump_line = raw_dump_line
            if not is_gzipped_dump:
                gzip_tail_index = find_legacy_gzip_tail_index(current_dump_line)
                if gzip_tail_index >= 0:
                    current_dump_line = current_dump_line[:gzip_tail_index]
                    embedded_gzip_tail_skipped = True
                    stop_after_line = True

            stripped_dump_line = current_dump_line.strip()
            if current_dump_line and any(
                stripped_dump_line.startswith(prefix)
                for prefix in _INCOMPATIBLE_SET_PREFIXES
            ):
                if stop_after_line:
                    break
                continue
            if sanitize_invalid_utf8:
                current_dump_line, replaced = _sanitize_invalid_utf8_line(
                    current_dump_line
                )
                if replaced:
                    invalid_utf8_replacement_count += 1
            if current_dump_line:
                pipe_proc.stdin.write(current_dump_line)  # type: ignore[union-attr]
            if stop_after_line:
                break
    finally:
        dump_reader.close()
        pipe_proc.stdin.close()  # type: ignore[union-attr]
        pipe_proc.wait()
        if embedded_gzip_tail_skipped:
            print(
                "[WARN] Ignored embedded gzip bytes at the end of a plain SQL "
                "dump. This indicates a malformed legacy backup."
            )
        if invalid_utf8_replacement_count:
            print(
                f"[WARN] Replaced invalid UTF-8 bytes in "
                f"{invalid_utf8_replacement_count} dump line(s)."
            )
        if pipe_proc.returncode != 0:
            raise subprocess.CalledProcessError(pipe_proc.returncode, pipe_proc.args)


def quote_postgres_identifier_path(identifier_path: str) -> str | None:
    """Safely quote a PostgreSQL table identifier path.

    Args:
        identifier_path: Table identifier, optionally schema-qualified
            (e.g. ``public.users``).

    Returns:
        Quoted identifier path (e.g. ``"public"."users"``), or ``None`` if the
        input is not a safe identifier.
    """
    identifier_parts = identifier_path.split(".")
    if not identifier_parts or len(identifier_parts) > 2:
        return None
    if any(
        not _POSTGRES_IDENTIFIER_PATTERN.fullmatch(part) for part in identifier_parts
    ):
        return None
    return ".".join(f'"{part}"' for part in identifier_parts)


def get_table_row_count(db_url: str, table_name: str) -> int | None:
    """Return the row count of ``table_name`` or ``None`` if unavailable.

    Args:
        db_url: PostgreSQL connection URL.
        table_name: Table name (optionally schema-qualified) to query.

    Returns:
        Row count, or ``None`` if the table does not exist or the input is unsafe.
    """
    quoted_table_name = quote_postgres_identifier_path(table_name)
    if quoted_table_name is None:
        return None

    parsed_postgres_url = parse_postgres_url(db_url)
    runtime_env = _build_psql_env(str(parsed_postgres_url["password"]))
    completed_psql_proc = subprocess.run(
        [
            "psql",
            "-h",
            str(parsed_postgres_url["host"]),
            "-p",
            str(parsed_postgres_url["port"]),
            "-U",
            str(parsed_postgres_url["user"]),
            "-d",
            str(parsed_postgres_url["database"]),
            "-t",
            "-A",
            "-c",
            f"SELECT COUNT(*) FROM {quoted_table_name}",
        ],
        env=runtime_env,
        capture_output=True,
        text=True,
    )
    if completed_psql_proc.returncode != 0:
        return None
    try:
        return int(completed_psql_proc.stdout.strip())
    except ValueError:
        return None


def clean_target_schema(db_url: str) -> None:
    """Drop and recreate the ``public`` schema on a PostgreSQL target.

    Args:
        db_url: Target PostgreSQL database URL.

    Raises:
        subprocess.CalledProcessError: If the SQL command fails.
    """
    parsed_postgres_url = parse_postgres_url(db_url)
    runtime_env = _build_psql_env(str(parsed_postgres_url["password"]))
    subprocess.run(
        [
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            str(parsed_postgres_url["host"]),
            "-p",
            str(parsed_postgres_url["port"]),
            "-U",
            str(parsed_postgres_url["user"]),
            "-d",
            str(parsed_postgres_url["database"]),
            "-c",
            "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
        ],
        env=runtime_env,
        check=True,
        capture_output=True,
    )


def drop_and_recreate_target_db(db_url: str) -> None:
    """Drop and recreate the target PostgreSQL database.

    Args:
        db_url: Target PostgreSQL database URL.

    Raises:
        subprocess.CalledProcessError: If ``dropdb`` or ``createdb`` fails.
    """
    parsed_postgres_url = parse_postgres_url(db_url)
    runtime_env = _build_psql_env(str(parsed_postgres_url["password"]))
    subprocess.run(
        [
            "dropdb",
            "--if-exists",
            "-h",
            str(parsed_postgres_url["host"]),
            "-p",
            str(parsed_postgres_url["port"]),
            "-U",
            str(parsed_postgres_url["user"]),
            str(parsed_postgres_url["database"]),
        ],
        env=runtime_env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "createdb",
            "-h",
            str(parsed_postgres_url["host"]),
            "-p",
            str(parsed_postgres_url["port"]),
            "-U",
            str(parsed_postgres_url["user"]),
            str(parsed_postgres_url["database"]),
        ],
        env=runtime_env,
        check=True,
        capture_output=True,
    )
