"""Source-time validation and a bounded live queue; no MASsoft/network commands."""

from __future__ import annotations

import argparse
import csv
import math
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from massoft_protocol import MASsoftProtocolError


def parse_start_utc(value: str) -> tuple[str, float]:
    """Require an explicit zone; never infer run origin from the receiving clock."""
    value = value.strip()
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("SourceStartUTC requires ISO time with Z or an explicit UTC offset")
    if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValueError("Invalid UTC offset")
    dt = datetime.fromisoformat(value).astimezone(timezone.utc)
    epoch = dt.timestamp()
    if not math.isfinite(epoch) or epoch <= 0:
        raise ValueError("SourceStartUTC must be after the Unix epoch")
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z"), epoch


@dataclass(frozen=True)
class TimedRow:
    values: tuple[float, ...]
    raw: str
    elapsed_ms: int
    source_time: float
    receipt_time: float
    receipt_monotonic: float


class SourceGuard:
    """Called under the client's sample lock; all errors latch until reconfigured.

    Old rows are counted but never queued. Pausing keeps validating the stream
    without buffering it. Only eligible live rows use bounded queue storage.
    """

    def __init__(
        self,
        start,
        *,
        max_age,
        capacity=256,
        not_before=0.0,
        wall,
        monotonic,
        clock_tolerance=1.0,
    ):
        if not math.isfinite(start) or start <= 0:
            raise ValueError("Invalid source start timestamp")
        if not math.isfinite(max_age) or max_age <= 0:
            raise ValueError("SourceMaxAge must be positive and finite")
        if type(capacity) is not int or not 1 <= capacity <= 100000:
            raise ValueError("data_queue_size must be an integer in 1..100000")
        if (
            not all(math.isfinite(x) for x in (wall, monotonic, not_before, clock_tolerance))
            or clock_tolerance < 0
        ):
            raise ValueError("Invalid clock reference or tolerance")
        self.start, self.max_age, self.capacity = start, max_age, capacity
        self.not_before = not_before
        self.clock_tolerance = clock_tolerance
        self._clock_offset = wall - monotonic
        self._queue = deque()
        self.enabled = False
        self.latest = None
        self.error = None
        self.history_rows = self.dropped_rows = self.duplicates = 0

    def _fail(self, message):
        self.error = message
        self._queue.clear()
        raise MASsoftProtocolError(message)

    def check_clock(self, wall, monotonic):
        if self.error:
            raise MASsoftProtocolError(self.error)
        if not math.isfinite(wall) or not math.isfinite(monotonic):
            self._fail("Invalid IOC clock reading")
        if abs((wall - monotonic) - self._clock_offset) > self.clock_tolerance:
            self._fail("IOC clock stepped; reopen and verify SourceStartUTC")

    def set_enabled(self, enabled, *, not_before=None):
        self.enabled = bool(enabled)
        self._queue.clear()
        if not_before is not None:
            self.not_before = max(self.not_before, not_before)

    def push(self, values, raw, elapsed_ms, *, wall, monotonic):
        self.check_clock(wall, monotonic)
        if type(elapsed_ms) is not int or not 0 <= elapsed_ms <= 2**53:
            self._fail("Missing or invalid elapsed milliseconds; DataMsFmt=1 is required")
        source = self.start + elapsed_ms / 1000.0
        row = TimedRow(tuple(values), raw, elapsed_ms, source, wall, monotonic)
        if source > wall + self.clock_tolerance:
            self._fail("Source timestamp is in the future; verify run origin and VM clock")
        if self.latest is not None:
            if elapsed_ms < self.latest.elapsed_ms:
                self._fail("Elapsed counter reset or moved backwards; reopen and verify run origin")
            if elapsed_ms == self.latest.elapsed_ms:
                if row.values != self.latest.values:
                    self._fail("Different measurements share one elapsed counter")
                self.duplicates += 1
                return
        self.latest = row
        if wall - source > self.max_age or source <= self.not_before:
            self.history_rows += 1
            return
        if not self.enabled:
            return
        if len(self._queue) >= self.capacity:
            self.dropped_rows += len(self._queue) + 1
            self._fail("Live data queue overflow; publication stopped (no silent overwrite)")
        self._queue.append(row)

    def drain(self, *, wall, monotonic, limit=64):
        self.check_clock(wall, monotonic)
        rows = []
        while self._queue and len(rows) < limit:
            row = self._queue[0]
            if row.source_time > wall:
                break  # Small rounding/clock skew: wait, never publish into the future.
            self._queue.popleft()
            if row.source_time <= self.not_before or wall - row.source_time > self.max_age:
                self.dropped_rows += 1
                continue
            self.not_before = row.source_time
            rows.append(row)
        return rows

    def snapshot(self):
        return {
            "latest": self.latest,
            "queue_depth": len(self._queue),
            "history_rows": self.history_rows,
            "dropped_rows": self.dropped_rows,
            "duplicates": self.duplicates,
            "error": self.error,
        }


def csv_start_utc(path, *, date_order, utc_offset):
    """Offline helper for an operator-verified export of the selected experiment."""
    if date_order not in ("mdy", "dmy"):
        raise ValueError("date_order must be mdy or dmy")
    if not re.fullmatch(r"[+-]\d{2}:\d{2}", utc_offset):
        raise ValueError("Supply the UTC offset at run start, such as -04:00")
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for index, row in enumerate(csv.reader(stream)):
            if index >= 128:
                break
            if len(row) == 4 and row[0].strip() == "Date" and row[2].strip() == "Time":
                date_format = "%m/%d/%Y" if date_order == "mdy" else "%d/%m/%Y"
                for time_format in ("%I:%M:%S %p", "%H:%M:%S", "%H:%M:%S.%f"):
                    try:
                        dt = datetime.strptime(
                            f"{row[1].strip()} {row[3].strip()} {utc_offset}",
                            f"{date_format} {time_format} %z",
                        )
                    except ValueError:
                        continue
                    return parse_start_utc(dt.isoformat())[0]
                raise ValueError("Unrecognized CSV date/time or UTC offset; verify header")
    raise ValueError("No MASsoft Date/Time header found in the first 128 CSV records")


def main():
    parser = argparse.ArgumentParser(
        description="Read run start from a MASsoft CSV; no EPICS writes"
    )
    parser.add_argument("csv", type=Path)
    parser.add_argument("--date-order", choices=("mdy", "dmy"), required=True)
    parser.add_argument(
        "--utc-offset", required=True, help="Offset at run start, e.g. --utc-offset=-04:00"
    )
    args = parser.parse_args()
    print(csv_start_utc(args.csv, date_order=args.date_order, utc_offset=args.utc_offset))


if __name__ == "__main__":
    main()
