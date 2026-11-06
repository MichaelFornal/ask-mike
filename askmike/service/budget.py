# ported from deal-terms-desk service/budget.py on 2026-11-05; reserve and settle take dollars (two models price differently)
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


STALE_S = 600.0  # an open reservation this old belongs to a call that died: far beyond the timeout × attempts

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend(
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'settled', 'released')), worst REAL NOT NULL,
    usd REAL NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS spend_month ON spend(month);
CREATE INDEX IF NOT EXISTS spend_day ON spend(day);
"""
TOTAL = "SELECT COALESCE(SUM(CASE status WHEN 'open' THEN worst ELSE usd END), 0) FROM spend WHERE {col} = ?"


def _keys(t: float) -> tuple[str, str]:
    """(UTC day, UTC month) for a timestamp: the budget's calendar, whatever the server's timezone."""
    d = datetime.fromtimestamp(t, timezone.utc)
    return d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")


class Budget:
    """The spend ledger and its month and day caps. A call reserves its worst case first and settles at actual
    cost after, so neither concurrent calls nor a crash between the two can take spend past a cap."""

    def __init__(self, db, month_cap: float, day_cap: float, clock=time.time):
        db = Path(db)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.month_cap, self.day_cap, self.clock = float(month_cap), float(day_cap), clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        with self._lock:
            self._tx(self._reconcile)

    def _tx(self, fn):
        c = self._conn
        c.execute("BEGIN IMMEDIATE")  # takes the write lock now, so another process cannot interleave
        try:
            out = fn(c)
            c.execute("COMMIT")
            return out
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def _reconcile(self, c) -> None:
        c.execute("UPDATE spend SET status = 'settled', usd = worst WHERE status = 'open' AND ts < ?",
                  (self.clock() - STALE_S,))

    @staticmethod
    def _totals(c, now: float) -> tuple[float, float]:
        day, month = _keys(now)
        return (c.execute(TOTAL.format(col="month"), (month,)).fetchone()[0],
                c.execute(TOTAL.format(col="day"), (day,)).fetchone()[0])

    def reserve(self, worst_usd: float) -> int | None:
        """A reservation id, or None when the worst case would take the month or the day past its cap. A worst case
        that is not a positive, finite number raises ValueError: it would open headroom or poison the totals."""
        worst = float(worst_usd)
        if not math.isfinite(worst) or worst <= 0:
            raise ValueError(f"reserve: worst_usd must be a positive, finite number, got {worst_usd!r}")

        def fn(c):
            self._reconcile(c)
            now = self.clock()
            month, day = self._totals(c, now)
            if month + worst > self.month_cap or day + worst > self.day_cap:
                return None
            d, m = _keys(now)
            return c.execute("INSERT INTO spend(ts, day, month, status, worst) VALUES (?, ?, ?, 'open', ?)",
                             (now, d, m, worst)).lastrowid
        with self._lock:
            return self._tx(fn)

    def settle(self, rid: int, usd: float | None) -> float:
        """Book a call at its actual cost in dollars; None (a timeout: billed or not is unknown) books its worst case.
        An actual cost above the worst case is booked as it is: it was billed. A negative or non-finite usd raises
        ValueError and leaves the reservation as it was. The token columns stay in the schema for DTD parity and are
        not filled (0)."""
        if usd is not None and (not math.isfinite(float(usd)) or float(usd) < 0):
            raise ValueError(f"settle: usd must be a finite number of dollars, zero or more, got {usd!r}")

        def fn(c):
            row = c.execute("SELECT worst, status FROM spend WHERE id = ?", (rid,)).fetchone()
            if rid is None or row is None:
                raise ValueError(f"settle: no reservation with id {rid!r}")
            if row[1] == "released":
                raise ValueError(f"settle: reservation {rid!r} was released and cannot be settled")
            booked = row[0] if usd is None else float(usd)
            c.execute("UPDATE spend SET status = 'settled', usd = ? WHERE id = ?", (booked, rid))
            return booked
        with self._lock:
            return self._tx(fn)

    def release(self, rid: int) -> None:
        """The call was never billed (refused before it ran): its reservation no longer counts."""
        with self._lock:
            self._tx(lambda c: c.execute("UPDATE spend SET status = 'released', usd = 0 WHERE id = ? AND"
                                         " status = 'open'", (rid,)))

    def spent(self) -> dict:
        """This UTC month's and day's spend in USD, open reservations at their worst case."""
        with self._lock:
            month, day = self._totals(self._conn, self.clock())
        return {"month": round(month, 6), "day": round(day, 6)}

    def state(self, min_call_usd: float) -> str:
        """'reached' when less than one typical call is left under either cap."""
        s = self.spent()
        if self.month_cap - s["month"] < min_call_usd or self.day_cap - s["day"] < min_call_usd:
            return "reached"
        return "ok"
