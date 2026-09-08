"""Structured JSONL diagnostic writer."""

import json
from pathlib import Path
from typing import Any, Dict, Optional


class JsonlWriter:
    """Thread-safe or streaming JSONL file logger."""

    def __init__(self, file_path: str = "data/output/results.jsonl"):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.file_path, "a", encoding="utf-8")

    def write_record(self, record: Dict[str, Any]) -> None:
        """Write a single JSON record followed by a newline."""
        line = json.dumps(record, ensure_ascii=False)
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        """Flush and close output file."""
        if self._file and not self._file.closed:
            self._file.flush()
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
