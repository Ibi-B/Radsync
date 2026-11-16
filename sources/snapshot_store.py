"""Storage-neutral interface and local-file implementation for provider snapshots.

The refresh pipeline depends on this interface rather than on snapshot paths.
The local adapter remains the offline/test implementation and bootstrap source
for external-object migration.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from sources.state import (
    load_snapshot as load_local_snapshot,
    save_snapshot as save_local_snapshot,
    snapshot_counts,
    snapshot_path,
    _validate_snapshot_file,
)


@runtime_checkable
class SnapshotStore(Protocol):
    def save_snapshot(self, source_id: str, stations: list[dict]) -> dict:
        ...

    def load_snapshot(self, source_id: str) -> dict | None:
        ...

    def verify_snapshot(self, source_id: str) -> dict | None:
        ...

    def prune_snapshots(self, source_id: str | None = None, *, apply: bool = False) -> list[str]:
        ...


class LocalFileSnapshotStore:
    """SnapshotStore backed by the existing per-provider JSON last-good files.

    prune_snapshots only targets incomplete temporary files. It never deletes a
    valid or legacy last-good provider snapshot.
    """

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)

    def save_snapshot(self, source_id, stations):
        return save_local_snapshot(self.state_dir, source_id, stations)

    def load_snapshot(self, source_id):
        return load_local_snapshot(self.state_dir, source_id)

    def verify_snapshot(self, source_id):
        path = snapshot_path(self.state_dir, source_id)
        if not path.is_file() or not _validate_snapshot_file(path, source_id):
            return None
        counts = snapshot_counts(self.state_dir, source_id)
        if counts is None:
            return None
        return {"source": source_id, **counts, "path": str(path)}

    def prune_snapshots(self, source_id=None, *, apply=False):
        sources_dir = self.state_dir / "sources"
        if not sources_dir.exists():
            return []
        patterns = ("*.json.tmp", "*.json.external.tmp")
        candidates = []
        expected = f"{source_id}.json" if source_id else None
        for pattern in patterns:
            for path in sources_dir.glob(pattern):
                if expected and not path.name.startswith(expected):
                    continue
                candidates.append(path)
        selected = sorted({path for path in candidates})
        if apply:
            for path in selected:
                path.unlink(missing_ok=True)
        return [str(path) for path in selected]
