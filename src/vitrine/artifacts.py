"""Disk-backed artifact store for the display pipeline.

Persists large objects (DataFrames, chart specs) to disk so the WebSocket
stays lightweight. The artifact store uses a session directory layout:

    {vitrine_dir}/display/{session_id}/
    ├── index.json              # Card descriptors in insertion order
    ├── artifacts/
    │   ├── {card_id}.parquet   # DataFrame artifacts
    │   ├── {card_id}.json      # Plotly specs, key-value data
    │   └── {card_id}.svg       # Rendered matplotlib figures
    ├── revisions/
    │   ├── {card_id}.json      # Earlier versions of a replaced card
    │   └── {card_id}/{n}.*     # Their artifact files, by revision number
    ├── code/
    │   └── {sha256}.py         # Scripts that made cards, by content hash
    └── meta.json               # Session metadata
"""

from __future__ import annotations

import contextlib
import filecmp
import io
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb

if TYPE_CHECKING:
    # Imported where a card needs it: the server imports this module at start,
    # and pandas' first import in a fresh environment can take longer than the
    # start command waits.
    import pandas as pd

from vitrine._types import (
    AgentContext,
    CardDescriptor,
    CardProvenance,
    CardType,
    CodeOrigin,
)

logger = logging.getLogger(__name__)

# Script copies are named by their SHA-256; anything else must not reach a path.
_CODE_HASH = re.compile(r"[0-9a-f]{1,64}")


def _duckdb_safe_path(path: Path) -> str:
    """Escape a file path for safe interpolation into DuckDB SQL string literals.

    Single quotes in the path are doubled to prevent SQL injection.
    """
    from vitrine._utils import duckdb_safe_path

    return duckdb_safe_path(path)


def _serialize_card(card: CardDescriptor) -> dict[str, Any]:
    """Serialize a CardDescriptor to a JSON-compatible dict."""
    d: dict[str, Any] = {
        "card_id": card.card_id,
        "card_type": card.card_type.value,
        "title": card.title,
        "description": card.description,
        "timestamp": card.timestamp,
        "study": card.study,
        "dismissed": card.dismissed,
        "deleted": card.deleted,
        "deleted_at": card.deleted_at,
        "artifact_id": card.artifact_id,
        "artifact_type": card.artifact_type,
        "preview": card.preview,
        "response_requested": card.response_requested,
        "prompt": card.prompt,
        "timeout": card.timeout,
        "actions": card.actions,
        "response_action": card.response_action,
        "response_message": card.response_message,
        "response_values": card.response_values,
        "response_summary": card.response_summary,
        "response_artifact_id": card.response_artifact_id,
        "response_timestamp": card.response_timestamp,
        "annotations": card.annotations,
    }
    if card.provenance:
        d["provenance"] = {
            "source": card.provenance.source,
            "query": card.provenance.query,
            "code_hash": card.provenance.code_hash,
            "dataset": card.provenance.dataset,
            "timestamp": card.provenance.timestamp,
        }
    else:
        d["provenance"] = None
    if card.agent:
        d["agent"] = {
            "host": card.agent.host,
            "session_id": card.agent.session_id,
            "turn_id": card.agent.turn_id,
            "recorded_at": card.agent.recorded_at,
        }
    else:
        d["agent"] = None
    if card.code:
        d["code"] = {
            "path": card.code.path,
            "line": card.code.line,
            "sha256": card.code.sha256,
        }
    else:
        d["code"] = None
    d["why"] = card.why
    return d


def _deserialize_card(d: dict[str, Any]) -> CardDescriptor:
    """Deserialize a dict back into a CardDescriptor."""
    provenance = None
    if d.get("provenance"):
        p = d["provenance"]
        provenance = CardProvenance(
            source=p.get("source"),
            query=p.get("query"),
            code_hash=p.get("code_hash"),
            dataset=p.get("dataset"),
            timestamp=p.get("timestamp"),
        )
    agent = None
    if d.get("agent"):
        a = d["agent"]
        agent = AgentContext(
            host=a.get("host") or "unknown",
            session_id=a.get("session_id"),
            turn_id=a.get("turn_id"),
            recorded_at=a.get("recorded_at") or "",
        )
    code = None
    if d.get("code"):
        c = d["code"]
        code = CodeOrigin(
            path=c.get("path") or "",
            line=c.get("line"),
            sha256=c.get("sha256"),
        )
    return CardDescriptor(
        card_id=d["card_id"],
        card_type=CardType("decision" if d["card_type"] == "form" else d["card_type"]),
        title=d.get("title"),
        description=d.get("description"),
        timestamp=d.get("timestamp", ""),
        study=d.get("study"),
        dismissed=d.get("dismissed", False),
        deleted=d.get("deleted", False),
        deleted_at=d.get("deleted_at"),
        artifact_id=d.get("artifact_id"),
        artifact_type=d.get("artifact_type"),
        preview=d.get("preview", {}),
        provenance=provenance,
        agent=agent,
        code=code,
        why=d.get("why"),
        response_requested=d.get("response_requested", False),
        prompt=d.get("prompt"),
        timeout=d.get("timeout"),
        actions=d.get("actions"),
        response_action=d.get("response_action"),
        response_message=d.get("response_message"),
        response_values=d.get("response_values", {}),
        response_summary=d.get("response_summary"),
        response_artifact_id=d.get("response_artifact_id"),
        response_timestamp=d.get("response_timestamp"),
        annotations=d.get("annotations", []),
    )


class ArtifactStore:
    """Disk-backed store for display artifacts.

    Each session gets its own directory. Card descriptors are maintained
    in an index.json file. Large artifacts (DataFrames, chart specs) are
    stored as separate files in an artifacts/ subdirectory.

    Args:
        session_dir: Path to the session directory. Created if it doesn't exist.
        session_id: Unique identifier for this session.
    """

    def __init__(self, session_dir: Path, session_id: str) -> None:
        self.session_dir = session_dir
        self.session_id = session_id
        self._artifacts_dir = session_dir / "artifacts"
        self._index_path = session_dir / "index.json"
        self._meta_path = session_dir / "meta.json"

        # Ensure directories exist
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Initialize index and session metadata if they don't exist. Checked
        # again under the lock: a client that starts later must not reset what
        # another one has written meanwhile.
        if not self._index_path.exists():
            with self._changing(self._index_path):
                if not self._index_path.exists():
                    self._write_index([])

        if not self._meta_path.exists():
            with self._changing(self._meta_path):
                if not self._meta_path.exists():
                    self._write_json(
                        self._meta_path,
                        {
                            "session_id": session_id,
                            "start_time": datetime.now(timezone.utc).isoformat(),
                            "study_names": [],
                        },
                    )

    @contextlib.contextmanager
    def _changing(self, path: Path) -> Iterator[None]:
        """Hold an exclusive lock while reading, changing and writing ``path``.

        Every worktree of a repository shares one store, and each client writes
        the index itself before telling the server, so two agents can change a
        study's files at the same time. Without the lock both read the same old
        list and the last one to write drops the other's card.
        """
        from vitrine._utils import lock_file, unlock_file

        lock_fd = None
        try:
            lock_fd = open(path.with_suffix(".lock"), "w")
            lock_file(lock_fd, exclusive=True, blocking=True)
        except OSError:
            # A store that cannot hold a lock (a read-only one) is written as before.
            if lock_fd is not None:
                lock_fd.close()
            lock_fd = None
        try:
            yield
        finally:
            if lock_fd is not None:
                try:
                    unlock_file(lock_fd)
                except OSError:
                    pass
                lock_fd.close()

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        """Replace ``path`` in one step, so a reader never sees half a file.

        A half-written index reads as empty, and appending to that would keep
        only the new card.
        """
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def _read_index(self) -> list[dict[str, Any]]:
        """Read the card index from disk."""
        try:
            return json.loads(self._index_path.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _write_index(self, cards: list[dict[str, Any]]) -> None:
        """Write the card index to disk."""
        self._write_json(self._index_path, cards)

    def adopt_artifacts(self, from_id: str, to_id: str) -> None:
        """Give ``from_id``'s artifact files to ``to_id``, replacing ``to_id``'s own."""
        for path in self._artifacts_dir.glob(f"{to_id}.*"):
            path.unlink()
        for path in self._artifacts_dir.glob(f"{from_id}.*"):
            # Keep everything after the id: an image card's files are
            # "<id>.image-0-<hash>.png", not just "<id>.png".
            path.rename(self._artifacts_dir / f"{to_id}{path.name[len(from_id) :]}")

    def remove_from_index(self, card_id: str) -> None:
        """Drop a card's entry from the index, keeping its artifacts."""
        with self._changing(self._index_path):
            cards = self._read_index()
            kept = [c for c in cards if c.get("card_id") != card_id]
            if len(kept) != len(cards):
                self._write_index(kept)

    def _revisions_path(self, card_id: str) -> Path:
        return self.session_dir / "revisions" / f"{card_id}.json"

    @contextlib.contextmanager
    def replacing(self, card_id: str) -> Iterator[None]:
        """Hold a card's replace lock while ``show(replace=...)`` rewrites it.

        Two processes replacing the same card would otherwise both snapshot
        the same old version before either updates it.
        """
        lock_dir = self.session_dir / "revisions"
        lock_dir.mkdir(parents=True, exist_ok=True)
        with self._changing(lock_dir / f"{card_id}-replace"):
            yield

    def keep_revision(self, card_id: str) -> int | None:
        """Keep a card's current version before ``show(replace=...)`` overwrites it.

        The card's index entry and a copy of its artifact files go under
        ``revisions/``; the entry carries the agent session that made that
        version. Returns the revision number, or None for a card that is not
        in this store.
        """
        entry = next(
            (d for d in self._read_index() if d.get("card_id") == card_id), None
        )
        if entry is None:
            return None
        path = self._revisions_path(card_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._changing(path):
            revisions = self.list_revisions(card_id)
            number = len(revisions) + 1
            kept: list[str] = []
            for source in sorted(self._artifacts_dir.glob(f"{card_id}.*")):
                # Everything after the id: an image card keeps several files
                # of one kind ("<id>.image-0-<hash>.png", "<id>.thumb-0-...").
                target = (
                    path.parent / card_id / f"{number}{source.name[len(card_id) :]}"
                )
                target.parent.mkdir(exist_ok=True)
                shutil.copy2(source, target)
                kept.append(target.name)
            revisions.append(
                {
                    "revision": number,
                    "replaced_at": datetime.now(timezone.utc).isoformat(),
                    "card": entry,
                    "artifacts": kept,
                }
            )
            self._write_json(path, revisions)
        return number

    def same_content(self, card_id: str, other_id: str) -> bool:
        """Whether two cards in this store show the same thing.

        Compares type, title, description, preview and artifact bytes, so a
        script re-run that produces the same result is recognised.
        """
        entries = {
            d.get("card_id"): d
            for d in self._read_index()
            if d.get("card_id") in (card_id, other_id)
        }
        if card_id not in entries or other_id not in entries:
            return False
        fields = ("card_type", "title", "description", "preview")
        if any(entries[card_id].get(f) != entries[other_id].get(f) for f in fields):
            return False

        ours = sorted(self._artifacts_dir.glob(f"{card_id}.*"))
        theirs = sorted(self._artifacts_dir.glob(f"{other_id}.*"))
        if [p.name[len(card_id) :] for p in ours] != [
            p.name[len(other_id) :] for p in theirs
        ]:
            return False
        # filecmp compares sizes first, then reads both files in small chunks.
        return all(filecmp.cmp(a, b, shallow=False) for a, b in zip(ours, theirs))

    def list_revisions(self, card_id: str) -> list[dict[str, Any]]:
        """A card's earlier versions, oldest first.

        Each has ``revision`` (from 1), ``replaced_at``, ``card`` (the index
        entry as it was) and ``artifacts`` (file names under
        ``revisions/{card_id}/``).
        """
        try:
            return json.loads(self._revisions_path(card_id).read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def keep_code(self, sha256: str, source: bytes, suffix: str) -> None:
        """Keep a copy of the script a card came from, once per version.

        Copies live under ``code/``, named by the script's SHA-256, so a
        card still shows the code that made it after the script changes.
        """
        if not _CODE_HASH.fullmatch(sha256 or ""):
            raise ValueError(f"not a SHA-256 hex digest: {sha256!r}")
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
            suffix = ".txt"
        path = self.session_dir / "code" / f"{sha256}{suffix}"
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(source)
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def read_code(self, sha256: str) -> str | None:
        """The kept copy of a script, by its SHA-256, or None."""
        if not _CODE_HASH.fullmatch(sha256 or ""):
            return None
        for path in (self.session_dir / "code").glob(f"{sha256}.*"):
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    def _append_to_index(self, card_dict: dict[str, Any]) -> None:
        """Append a card to the index."""
        with self._changing(self._index_path):
            cards = self._read_index()
            cards.append(card_dict)
            self._write_index(cards)

    def _track_study(self, study: str | None) -> None:
        """Track a study name in session metadata."""
        if not study:
            return
        with self._changing(self._meta_path):
            try:
                meta = json.loads(self._meta_path.read_text())
            except (json.JSONDecodeError, FileNotFoundError):
                meta = {"session_id": self.session_id, "study_names": []}
            names = meta.get("study_names", [])
            if study not in names:
                meta["study_names"] = [*names, study]
                self._write_json(self._meta_path, meta)

    def store_card(self, card: CardDescriptor) -> None:
        """Store a card descriptor in the index.

        Args:
            card: The card descriptor to store.
        """
        self._append_to_index(_serialize_card(card))
        self._track_study(card.study)

    def store_dataframe(self, card_id: str, df: pd.DataFrame) -> Path:
        """Store a DataFrame as a Parquet artifact.

        Args:
            card_id: Unique card identifier used as the filename.
            df: DataFrame to store.

        Returns:
            Path to the stored Parquet file.
        """
        path = self._artifacts_dir / f"{card_id}.parquet"
        df.to_parquet(path, index=False)
        logger.debug(f"Stored DataFrame artifact: {path} ({len(df)} rows)")
        return path

    def store_json(self, card_id: str, data: dict[str, Any]) -> Path:
        """Store a JSON artifact (e.g., Plotly spec, key-value data).

        Args:
            card_id: Unique card identifier used as the filename.
            data: Dictionary to store as JSON.

        Returns:
            Path to the stored JSON file.
        """
        path = self._artifacts_dir / f"{card_id}.json"
        path.write_text(json.dumps(data, indent=2, default=str))
        logger.debug(f"Stored JSON artifact: {path}")
        return path

    def store_image(self, card_id: str, data: bytes, fmt: str) -> Path:
        """Store an image artifact (SVG, PNG).

        Args:
            card_id: Unique card identifier used as the filename.
            data: Raw image bytes.
            fmt: Image format extension (e.g., 'svg', 'png').

        Returns:
            Path to the stored image file.
        """
        path = self._artifacts_dir / f"{card_id}.{fmt}"
        path.write_bytes(data)
        logger.debug(f"Stored image artifact: {path} ({len(data)} bytes)")
        return path

    def store_card_file(self, card_id: str, name: str, data: bytes) -> Path:
        """Store one of an image card's files as ``<card_id>.<name>``."""
        path = self.card_file_path(card_id, name)
        if path is None:
            raise ValueError(f"Not a card file name: {name!r}")
        path.write_bytes(data)
        return path

    def card_file_path(self, card_id: str, name: str) -> Path | None:
        """Where an image card keeps the file ``name``, or None for a name it never writes."""
        from vitrine.images import CARD_FILE_RE

        if not CARD_FILE_RE.match(name) or "/" in card_id or "\\" in card_id:
            return None
        return self._artifacts_dir / f"{card_id}.{name}"

    @staticmethod
    def _sanitize_search(search: str) -> str | None:
        """Sanitize a search string, returning None if invalid."""
        if not search or not search.strip():
            return None
        s = search.strip()
        # Reject SQL keywords to prevent injection
        sql_keywords = re.compile(
            r"\b(DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|EXEC|UNION)\b",
            re.IGNORECASE,
        )
        if sql_keywords.search(s):
            return None
        # Reject SQL comment syntax and statement terminators
        if "--" in s or ";" in s:
            return None
        # Only allow alphanumeric, spaces, basic punctuation
        if not re.match(r"^[\w\s.,\-:/'\"()]+$", s):
            return None
        return s

    def _parquet_columns(
        self, path: Path, con: duckdb.DuckDBPyConnection
    ) -> list[tuple[str, str]]:
        """Return list of (column_name, column_type) for a Parquet file."""
        safe = _duckdb_safe_path(path)
        rows = con.execute(
            f"SELECT name, type FROM parquet_schema('{safe}') WHERE type IS NOT NULL"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def _build_search_where(
        self,
        search: str,
        col_info: list[tuple[str, str]],
    ) -> str:
        """Build a WHERE clause that searches across all columns."""
        # Escape ILIKE wildcards first, then single quotes for SQL strings
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        escaped = escaped.replace("'", "''")
        clauses = []
        for col_name, col_type in col_info:
            upper = col_type.upper()
            if any(t in upper for t in ("VARCHAR", "UTF8", "STRING", "TEXT")):
                clauses.append(f"\"{col_name}\" ILIKE '%{escaped}%' ESCAPE '\\'")
            else:
                # Cast non-text columns to VARCHAR for general search
                clauses.append(
                    f"CAST(\"{col_name}\" AS VARCHAR) ILIKE '%{escaped}%' ESCAPE '\\'"
                )
        if not clauses:
            return ""
        return " WHERE " + " OR ".join(clauses)

    def read_table_page(
        self,
        card_id: str,
        offset: int = 0,
        limit: int = 50,
        sort_col: str | None = None,
        sort_asc: bool = True,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Read a page of rows from a stored Parquet artifact using DuckDB.

        Args:
            card_id: Card ID whose Parquet artifact to read.
            offset: Row offset for paging.
            limit: Maximum rows to return.
            sort_col: Column to sort by (None for insertion order).
            sort_asc: Sort ascending if True, descending if False.
            search: Free-text search string (ILIKE across all columns).

        Returns:
            Dict with 'columns', 'rows', 'total_rows', 'offset', 'limit'.

        Raises:
            FileNotFoundError: If no Parquet artifact exists for this card_id.
        """
        path = self._artifacts_dir / f"{card_id}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"No Parquet artifact for card {card_id}")

        con = duckdb.connect(":memory:")
        try:
            safe = _duckdb_safe_path(path)
            col_info = self._parquet_columns(path, con)
            col_names = [c[0] for c in col_info]

            # Build WHERE clause for search
            where = ""
            sanitized = self._sanitize_search(search) if search else None
            if sanitized:
                where = self._build_search_where(sanitized, col_info)

            # Get total row count (filtered if searching)
            total = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{safe}'){where}"
            ).fetchone()[0]

            # Build query
            query = f"SELECT * FROM read_parquet('{safe}'){where}"

            if sort_col and sort_col in col_names:
                direction = "ASC" if sort_asc else "DESC"
                query += f' ORDER BY "{sort_col}" {direction}'

            query += f" LIMIT {int(limit)} OFFSET {int(offset)}"

            result = con.execute(query)
            columns = [desc[0] for desc in result.description]
            rows = [list(row) for row in result.fetchall()]

            return {
                "columns": columns,
                "rows": rows,
                "total_rows": total,
                "offset": offset,
                "limit": limit,
            }
        finally:
            con.close()

    def table_stats(self, card_id: str) -> dict[str, dict[str, Any]]:
        """Compute per-column statistics for a stored Parquet artifact.

        Args:
            card_id: Card ID whose Parquet artifact to analyze.

        Returns:
            Dict mapping column name -> stats dict with keys like
            min, max, mean, null_count, approx_unique.

        Raises:
            FileNotFoundError: If no Parquet artifact exists for this card_id.
        """
        path = self._artifacts_dir / f"{card_id}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"No Parquet artifact for card {card_id}")

        con = duckdb.connect(":memory:")
        try:
            safe = _duckdb_safe_path(path)
            col_info = self._parquet_columns(path, con)
            stats: dict[str, dict[str, Any]] = {}

            for col_name, col_type in col_info:
                upper = col_type.upper()
                is_numeric = any(
                    t in upper
                    for t in (
                        "INT",
                        "FLOAT",
                        "DOUBLE",
                        "DECIMAL",
                        "NUMERIC",
                        "BIGINT",
                        "SMALLINT",
                        "TINYINT",
                    )
                )

                aggs = [
                    f'COUNT(*) - COUNT("{col_name}") AS null_count',
                    f'APPROX_COUNT_DISTINCT("{col_name}") AS approx_unique',
                    f'MIN("{col_name}") AS min_val',
                    f'MAX("{col_name}") AS max_val',
                ]
                if is_numeric:
                    aggs.append(f'AVG("{col_name}") AS mean_val')

                row = con.execute(
                    f"SELECT {', '.join(aggs)} FROM read_parquet('{safe}')"
                ).fetchone()

                col_stats: dict[str, Any] = {
                    "null_count": row[0],
                    "approx_unique": row[1],
                    "min": self._serialize_value(row[2]),
                    "max": self._serialize_value(row[3]),
                }
                if is_numeric:
                    col_stats["mean"] = round(row[4], 4) if row[4] is not None else None

                stats[col_name] = col_stats

            return stats
        finally:
            con.close()

    @staticmethod
    def _serialize_value(val: Any) -> Any:
        """Convert DuckDB values to JSON-serializable types."""
        if val is None:
            return None
        if hasattr(val, "isoformat"):
            return val.isoformat()
        try:
            json.dumps(val)
            return val
        except (TypeError, ValueError):
            return str(val)

    def export_table_csv(
        self,
        card_id: str,
        sort_col: str | None = None,
        sort_asc: bool = True,
        search: str | None = None,
    ) -> str:
        """Export a full table as CSV (with optional sort/search but no pagination).

        Args:
            card_id: Card ID whose Parquet artifact to export.
            sort_col: Column to sort by.
            sort_asc: Sort ascending if True.
            search: Free-text search filter.

        Returns:
            CSV string of the full (optionally filtered/sorted) table.

        Raises:
            FileNotFoundError: If no Parquet artifact exists for this card_id.
        """
        path = self._artifacts_dir / f"{card_id}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"No Parquet artifact for card {card_id}")

        con = duckdb.connect(":memory:")
        try:
            safe = _duckdb_safe_path(path)
            col_info = self._parquet_columns(path, con)
            col_names = [c[0] for c in col_info]

            where = ""
            sanitized = self._sanitize_search(search) if search else None
            if sanitized:
                where = self._build_search_where(sanitized, col_info)

            query = f"SELECT * FROM read_parquet('{safe}'){where}"

            if sort_col and sort_col in col_names:
                direction = "ASC" if sort_asc else "DESC"
                query += f' ORDER BY "{sort_col}" {direction}'

            df = con.execute(query).fetchdf()
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            return buf.getvalue()
        finally:
            con.close()

    def get_artifact(self, card_id: str) -> bytes | dict[str, Any]:
        """Retrieve a raw artifact by card ID.

        Checks for Parquet, JSON, SVG, and PNG files in order.

        Args:
            card_id: Card ID to look up.

        Returns:
            Raw bytes for binary artifacts, or dict for JSON artifacts.

        Raises:
            FileNotFoundError: If no artifact exists for this card_id.
        """
        for ext in ("parquet", "json", "svg", "png"):
            path = self._artifacts_dir / f"{card_id}.{ext}"
            if path.exists():
                if ext == "json":
                    return json.loads(path.read_text())
                return path.read_bytes()
        raise FileNotFoundError(f"No artifact found for card {card_id}")

    def get_card(self, card_id: str) -> CardDescriptor | None:
        """Look up a single card by ID without deserializing all cards.

        Args:
            card_id: The card ID to find.

        Returns:
            CardDescriptor if found, None otherwise.
        """
        for d in self._read_index():
            if d["card_id"] == card_id:
                return _deserialize_card(d)
        return None

    def list_cards(self, study: str | None = None) -> list[CardDescriptor]:
        """List all card descriptors in insertion order.

        Args:
            study: If provided, filter to cards with this study name.

        Returns:
            List of CardDescriptors in the order they were added.
        """
        cards = [_deserialize_card(d) for d in self._read_index()]
        if study is not None:
            cards = [c for c in cards if c.study == study]
        return cards

    def update_card(self, card_id: str, **changes: Any) -> CardDescriptor | None:
        """Update fields on an existing card.

        Args:
            card_id: ID of the card to update.
            **changes: Field names and new values.

        Returns:
            Updated CardDescriptor, or None if card not found.
        """
        with self._changing(self._index_path):
            index = self._read_index()
            for d in index:
                if d["card_id"] == card_id:
                    for key, value in changes.items():
                        if key == "card_type" and isinstance(value, CardType):
                            d[key] = value.value
                        else:
                            d[key] = value
                    self._write_index(index)
                    return _deserialize_card(d)
        return None

    def rename_study(self, old_label: str, new_label: str) -> int:
        """Update the study field on all cards matching old_label.

        Args:
            old_label: Current study label to match.
            new_label: New study label to set.

        Returns:
            Number of cards updated.
        """
        with self._changing(self._index_path):
            index = self._read_index()
            count = 0
            for d in index:
                if d.get("study") == old_label:
                    d["study"] = new_label
                    count += 1
            if count:
                self._write_index(index)
        return count

    def relocate(self, new_dir: Path, new_session_id: str) -> None:
        """Update internal paths after the study directory has been renamed.

        Args:
            new_dir: The new study directory path (must already exist).
            new_session_id: New session/directory identifier.
        """
        self.session_dir = new_dir
        self.session_id = new_session_id
        self._artifacts_dir = new_dir / "artifacts"
        self._index_path = new_dir / "index.json"
        self._meta_path = new_dir / "meta.json"

    def store_selection(self, selection_id: str, rows: list, columns: list) -> Path:
        """Store a selection of rows as a Parquet artifact.

        Args:
            selection_id: Unique ID for this selection.
            rows: List of row data (each row is a list of values).
            columns: Column names corresponding to row values.

        Returns:
            Path to the stored Parquet file.
        """
        import pandas as pd

        df = pd.DataFrame(rows, columns=columns)
        return self.store_dataframe(selection_id, df)

    def store_selection_json(self, selection_id: str, data: dict) -> Path:
        """Store a chart point selection as a JSON artifact.

        Args:
            selection_id: Unique ID for this selection.
            data: Dict with selection data (e.g., {"points": [...]}).

        Returns:
            Path to the stored JSON file.
        """
        return self.store_json(selection_id, data)

    def delete_session(self) -> None:
        """Delete the entire session directory."""
        if self.session_dir.exists():
            shutil.rmtree(self.session_dir)
            logger.debug(f"Deleted session directory: {self.session_dir}")
