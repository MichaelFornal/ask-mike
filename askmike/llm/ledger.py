# ported from deal-terms-desk pipeline/ledger.py on 2026-11-03; put() is lock-guarded and the key field is configurable
import threading
import json
import os
from pathlib import Path


class Ledger:
    """Append-only JSONL keyed by one field (`key` by default); the latest record for a key wins. A torn last line (a kill mid-write) is ignored."""

    def __init__(self, path: Path, key: str = "key"):
        self.path = Path(path)
        self.key = key
        self._recs: dict[str, dict] = {}
        self._lock = threading.Lock()
        if self.path.exists():
            data = self.path.read_text(encoding="utf-8")
            lines = data.split("\n")
            torn = False
            if not data.endswith("\n"):
                lines = lines[:-1]
                torn = True
            for line in lines:
                if line.strip():
                    rec = json.loads(line)
                    self._recs[rec[self.key]] = rec
            # Rewrite without the torn last line, so the next append starts on a fresh line. The rewrite goes
            # through a .part file and a rename, so a kill during it leaves the old ledger, never a shorter one.
            if torn:
                part = self.path.with_name(self.path.name + ".part")
                try:
                    part.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
                    os.replace(part, self.path)
                finally:
                    part.unlink(missing_ok=True)

    def get(self, key_value: str) -> dict | None:
        return self._recs.get(key_value)

    def put(self, rec: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._recs[rec[self.key]] = rec


class MemoryLedger:
    """Ledger's get/put, held in memory only: a one-off ask writes nothing to disk."""

    def __init__(self, key: str = "key"):
        self.key = key
        self._recs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, key_value: str) -> dict | None:
        return self._recs.get(key_value)

    def put(self, rec: dict) -> None:
        with self._lock:
            self._recs[rec[self.key]] = rec
