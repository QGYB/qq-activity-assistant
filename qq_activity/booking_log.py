# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Private local timing and failure evidence; never logs request bodies or cookies."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import time


class BookingLog:
    def __init__(self, data_dir, job_id, opening=None):
        identifier = str(job_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
            identifier = hashlib.sha256(identifier.encode()).hexdigest()[:32]
        self.directory = Path(data_dir) / "booking-logs"
        self.path = self.directory / f"{identifier}.jsonl"
        self.identifier, self.opening = identifier, opening
        self.started = time.perf_counter()

    def event(self, name, **fields):
        try:
            current = datetime.now(timezone(timedelta(hours=8)))
            row = {"at": current.isoformat(), "job_id": self.identifier, "event": name,
                   "elapsed_ms": round((time.perf_counter()-self.started)*1000, 3)}
            if self.opening is not None:
                row["since_open_ms"] = round((current-self.opening).total_seconds()*1000, 3)
            # Callers supply only phase/status/receipt metadata, never profiles.
            row.update(fields)
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False)+"\n")
        except (OSError, TypeError, ValueError):
            pass  # A diagnostics failure must not cause a second submission.

    def capture(self, page):
        if page is None:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            screenshot = self.directory / f"{self.identifier}-result.png"
            page.screenshot(path=str(screenshot), timeout=1500)
            text = page.locator("body").inner_text(timeout=1000)[:6000]
            (self.directory / f"{self.identifier}-result.txt").write_text(text, encoding="utf-8")
            self.event("failure_evidence_saved", screenshot=screenshot.name)
        except Exception:
            self.event("failure_evidence_unavailable")
