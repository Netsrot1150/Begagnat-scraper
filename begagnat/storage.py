"""SQLite state: which listings we have seen, and what they used to cost.

The database is what turns ``search`` into ``watch``. Without it every run
would report every listing as new.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .models import Listing, utcnow

DEFAULT_DB = Path.home() / ".local" / "share" / "begagnat" / "listings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    price_sek     REAL,
    gpu_name      TEXT,
    vram_gb       INTEGER,
    kr_per_gb     REAL,
    score         REAL,
    location      TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    notified      INTEGER NOT NULL DEFAULT 0,
    payload       TEXT,
    provisional   INTEGER NOT NULL DEFAULT 0
);
-- `provisional` is 1 when price_sek is a standing auction bid rather than a
-- cost. Such a row is kept - the auction may still be the find of the week -
-- but stays out of the price statistics, where one 1 kr opening bid would
-- otherwise halve a thin model's median. The comment lives out here because
-- SQLite replays a table's original CREATE text on ALTER TABLE ... DROP
-- COLUMN, and a trailing in-body comment makes that replay unparseable.
CREATE INDEX IF NOT EXISTS idx_listings_fingerprint ON listings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_listings_gpu ON listings(gpu_name);

CREATE TABLE IF NOT EXISTS ai_verdicts (
    key          TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    judged_at    TEXT NOT NULL,
    PRIMARY KEY (key, content_hash)
);

CREATE TABLE IF NOT EXISTS price_history (
    key       TEXT NOT NULL,
    seen_at   TEXT NOT NULL,
    price_sek REAL,
    PRIMARY KEY (key, seen_at)
);

-- A saved search ("bevakning"). The criteria are stored as JSON so the schema
-- does not have to change every time Criteria grows a field.
CREATE TABLE IF NOT EXISTS watches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    criteria       TEXT NOT NULL,
    source_options TEXT NOT NULL DEFAULT '{}',
    interval_s     INTEGER NOT NULL DEFAULT 900,
    enabled        INTEGER NOT NULL DEFAULT 1,
    use_ai         INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    last_run_at    TEXT,
    last_error     TEXT,
    last_hits      INTEGER NOT NULL DEFAULT 0
);

-- Which watch surfaced which listing, and when it first did.
CREATE TABLE IF NOT EXISTS watch_hits (
    watch_id   INTEGER NOT NULL,
    key        TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    PRIMARY KEY (watch_id, key)
);

-- The feed: what changed, so the UI can show it once and then stop shouting.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id   INTEGER,
    kind       TEXT NOT NULL,
    key        TEXT,
    created_at TEXT NOT NULL,
    seen       INTEGER NOT NULL DEFAULT 0,
    payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_unseen ON events(seen);

-- The user's own marks on a listing. Kept apart from `listings` so a re-scrape
-- never overwrites them.
CREATE TABLE IF NOT EXISTS listing_flags (
    key        TEXT PRIMARY KEY,
    starred    INTEGER NOT NULL DEFAULT 0,
    hidden     INTEGER NOT NULL DEFAULT 0,
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The web UI's request handlers and the watch scheduler each open their
        # own connection to the same file, so WAL (concurrent readers alongside
        # one writer) and a real busy timeout are not optional.
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    #: Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does
    #: nothing to a table that already exists, so an existing database needs
    #: them added by hand. Keep every entry idempotent and defaulted.
    _ADDED_COLUMNS = {
        "listings": {"provisional": "INTEGER NOT NULL DEFAULT 0"},
    }

    def _migrate(self) -> None:
        for table, columns in self._ADDED_COLUMNS.items():
            existing = {
                row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def record(self, listing: Listing) -> tuple[bool, float | None]:
        """Upsert a listing.

        Returns ``(is_new, previous_price_sek)``. A listing counts as new when
        neither its id nor its fingerprint has been seen - the fingerprint
        catches sellers who delete and re-post the same ad to bump it.
        """
        now = utcnow().isoformat()
        cur = self.conn.execute("SELECT * FROM listings WHERE key = ?", (listing.key,))
        row = cur.fetchone()

        if row is None:
            twin = self.conn.execute(
                "SELECT * FROM listings WHERE fingerprint = ? LIMIT 1", (listing.fingerprint,)
            ).fetchone()
            row = twin

        is_new = row is None
        previous_price = row["price_sek"] if row else None
        first_seen = row["first_seen"] if row else now
        listing.first_seen = listing.first_seen or utcnow()

        self.conn.execute(
            """
            INSERT INTO listings (key, source, external_id, fingerprint, title, url,
                                  price_sek, gpu_name, vram_gb, kr_per_gb, score,
                                  location, first_seen, last_seen, notified, payload,
                                  provisional)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT notified FROM listings WHERE key=?),0),?,?)
            ON CONFLICT(key) DO UPDATE SET
                price_sek   = excluded.price_sek,
                kr_per_gb   = excluded.kr_per_gb,
                score       = excluded.score,
                title       = excluded.title,
                last_seen   = excluded.last_seen,
                payload     = excluded.payload,
                provisional = excluded.provisional
            """,
            (
                listing.key, listing.source, listing.external_id, listing.fingerprint,
                listing.title, listing.url, listing.price_sek,
                listing.gpu.name if listing.gpu else None,
                listing.gpu.total_vram_gb if listing.gpu else None,
                round(listing.kr_per_gb, 1) if listing.kr_per_gb else None,
                listing.score, listing.location, first_seen, now, listing.key,
                json.dumps(listing.to_dict(), ensure_ascii=False),
                int(listing.price_is_provisional),
            ),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO price_history (key, seen_at, price_sek) VALUES (?,?,?)",
            (listing.key, now, listing.price_sek),
        )
        self.conn.commit()
        return is_new, previous_price

    def get_verdict(self, key: str, content_hash: str) -> str | None:
        """Return a cached AI verdict, but only for this exact ad text."""
        row = self.conn.execute(
            "SELECT verdict FROM ai_verdicts WHERE key = ? AND content_hash = ?",
            (key, content_hash),
        ).fetchone()
        return row["verdict"] if row else None

    def save_verdict(self, key: str, content_hash: str, verdict: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ai_verdicts (key, content_hash, verdict, judged_at)"
            " VALUES (?,?,?,?)",
            (key, content_hash, verdict, utcnow().isoformat()),
        )
        self.conn.commit()

    def mark_notified(self, keys: list[str]) -> None:
        self.conn.executemany(
            "UPDATE listings SET notified = 1 WHERE key = ?", [(k,) for k in keys]
        )
        self.conn.commit()

    def pending_notification(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM listings WHERE notified = 0 ORDER BY score DESC"
            )
        )

    def best(self, limit: int = 20, gpu_name: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM listings WHERE score > 0"
        params: list = []
        if gpu_name:
            sql += " AND gpu_name = ?"
            params.append(gpu_name)
        sql += " ORDER BY score DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params))

    def price_stats(self, gpu_name: str) -> dict:
        """Historic asking prices for one model, to judge whether a deal is a deal."""
        rows = self.conn.execute(
            "SELECT price_sek FROM listings WHERE gpu_name = ? AND price_sek IS NOT NULL",
            (gpu_name,),
        ).fetchall()
        prices = sorted(r["price_sek"] for r in rows)
        if not prices:
            return {}
        middle = len(prices) // 2
        median = prices[middle] if len(prices) % 2 else (prices[middle - 1] + prices[middle]) / 2
        return {"count": len(prices), "min": prices[0], "median": median, "max": prices[-1]}

    # ------------------------------------------------------------------
    # Watches ("bevakningar")
    # ------------------------------------------------------------------

    def list_watches(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM watches ORDER BY name"))

    def get_watch(self, watch_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()

    def save_watch(
        self,
        name: str,
        criteria: dict,
        *,
        watch_id: int | None = None,
        source_options: dict | None = None,
        interval_s: int = 900,
        enabled: bool = True,
        use_ai: bool = False,
    ) -> int:
        """Create or update a watch. Returns its id."""
        payload = (
            name,
            json.dumps(criteria, ensure_ascii=False),
            json.dumps(source_options or {}, ensure_ascii=False),
            int(interval_s),
            int(bool(enabled)),
            int(bool(use_ai)),
        )
        if watch_id is None:
            cur = self.conn.execute(
                "INSERT INTO watches (name, criteria, source_options, interval_s, enabled,"
                " use_ai, created_at) VALUES (?,?,?,?,?,?,?)",
                (*payload, utcnow().isoformat()),
            )
            watch_id = int(cur.lastrowid)
        else:
            self.conn.execute(
                "UPDATE watches SET name=?, criteria=?, source_options=?, interval_s=?,"
                " enabled=?, use_ai=? WHERE id=?",
                (*payload, watch_id),
            )
        self.conn.commit()
        return watch_id

    def delete_watch(self, watch_id: int) -> None:
        self.conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        self.conn.execute("DELETE FROM watch_hits WHERE watch_id = ?", (watch_id,))
        self.conn.execute("DELETE FROM events WHERE watch_id = ?", (watch_id,))
        self.conn.commit()

    def due_watches(self, now: datetime | None = None) -> list[sqlite3.Row]:
        """Enabled watches whose interval has elapsed (or that never ran)."""
        moment = now or utcnow()
        due = []
        for row in self.conn.execute("SELECT * FROM watches WHERE enabled = 1"):
            last = row["last_run_at"]
            if not last:
                due.append(row)
                continue
            try:
                elapsed = (moment - datetime.fromisoformat(last)).total_seconds()
            except ValueError:
                due.append(row)
                continue
            if elapsed >= row["interval_s"]:
                due.append(row)
        return due

    def finish_watch_run(self, watch_id: int, *, hits: int, error: str = "") -> None:
        self.conn.execute(
            "UPDATE watches SET last_run_at=?, last_hits=?, last_error=? WHERE id=?",
            (utcnow().isoformat(), hits, error, watch_id),
        )
        self.conn.commit()

    def link_watch_hit(self, watch_id: int, key: str) -> bool:
        """Record that a watch matched a listing. True the first time only.

        This is what makes "new" mean *new to this watch*: a listing already in
        `listings` because another watch found it must still be announced once
        for this one.
        """
        now = utcnow().isoformat()
        seen = self.conn.execute(
            "SELECT 1 FROM watch_hits WHERE watch_id = ? AND key = ?", (watch_id, key)
        ).fetchone()
        if seen is None:
            self.conn.execute(
                "INSERT INTO watch_hits (watch_id, key, first_seen, last_seen) VALUES (?,?,?,?)",
                (watch_id, key, now, now),
            )
            self.conn.commit()
            return True
        self.conn.execute(
            "UPDATE watch_hits SET last_seen = ? WHERE watch_id = ? AND key = ?",
            (now, watch_id, key),
        )
        self.conn.commit()
        return False

    def watch_hit_keys(self, watch_id: int) -> list[str]:
        return [
            r["key"]
            for r in self.conn.execute(
                "SELECT key FROM watch_hits WHERE watch_id = ? ORDER BY first_seen DESC",
                (watch_id,),
            )
        ]

    # ------------------------------------------------------------------
    # Event feed
    # ------------------------------------------------------------------

    def add_event(
        self,
        kind: str,
        *,
        watch_id: int | None = None,
        key: str | None = None,
        payload: dict | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (watch_id, kind, key, created_at, payload) VALUES (?,?,?,?,?)",
            (
                watch_id,
                kind,
                key,
                utcnow().isoformat(),
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def events(self, *, limit: int = 100, unseen_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM events"
        if unseen_only:
            sql += " WHERE seen = 0"
        sql += " ORDER BY id DESC LIMIT ?"
        return list(self.conn.execute(sql, (limit,)))

    def unseen_event_count(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS n FROM events WHERE seen = 0").fetchone()["n"]
        )

    def mark_events_seen(self, ids: list[int] | None = None) -> None:
        if ids is None:
            self.conn.execute("UPDATE events SET seen = 1 WHERE seen = 0")
        else:
            self.conn.executemany("UPDATE events SET seen = 1 WHERE id = ?", [(i,) for i in ids])
        self.conn.commit()

    # ------------------------------------------------------------------
    # The user's own marks
    # ------------------------------------------------------------------

    FLAG_FIELDS = ("starred", "hidden", "note")

    def set_flag(self, key: str, **fields) -> None:
        """Star, hide, or annotate a listing.

        Kept in its own table so a re-scrape of the ad never overwrites what the
        user decided about it.
        """
        unknown = set(fields) - set(self.FLAG_FIELDS)
        if unknown:
            raise ValueError(f"unknown listing flag: {', '.join(sorted(unknown))}")
        now = utcnow().isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO listing_flags (key, updated_at) VALUES (?,?)", (key, now)
        )
        for name, value in fields.items():
            stored = str(value) if name == "note" else int(bool(value))
            self.conn.execute(
                f"UPDATE listing_flags SET {name} = ?, updated_at = ? WHERE key = ?",
                (stored, now, key),
            )
        self.conn.commit()

    def flags(self) -> dict[str, dict]:
        return {
            r["key"]: {
                "starred": bool(r["starred"]),
                "hidden": bool(r["hidden"]),
                "note": r["note"],
            }
            for r in self.conn.execute("SELECT * FROM listing_flags")
        }

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def price_series(self, key: str) -> list[tuple[str, float]]:
        return [
            (r["seen_at"], r["price_sek"])
            for r in self.conn.execute(
                "SELECT seen_at, price_sek FROM price_history WHERE key = ?"
                " AND price_sek IS NOT NULL ORDER BY seen_at",
                (key,),
            )
        ]

    def model_price_window(self, gpu_name: str, days: int = 180) -> dict:
        """Asking prices for one model over a window, as percentiles.

        "Below the last six months' median" is a better buy signal than a fixed
        cap the user has to guess, so hand the caller the shape of the
        distribution rather than only its extremes.

        Hidden ads are left out: hiding one is the user saying it is not a real
        offer for this model (a bundle, a case, a mislabelled ad), and one such
        price drags the median for a thin model badly. Running auctions are
        left out for the same reason - a 1 kr opening bid is not an asking
        price, and there are usually enough of them to move the median.
        """
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        prices = sorted(
            r["price_sek"]
            for r in self.conn.execute(
                "SELECT l.price_sek FROM listings l"
                " LEFT JOIN listing_flags f ON f.key = l.key"
                " WHERE l.gpu_name = ? AND l.price_sek IS NOT NULL AND l.last_seen >= ?"
                " AND COALESCE(f.hidden, 0) = 0 AND COALESCE(l.provisional, 0) = 0",
                (gpu_name, cutoff),
            )
        )
        if not prices:
            return {}

        def percentile(fraction: float) -> float:
            if len(prices) == 1:
                return prices[0]
            position = fraction * (len(prices) - 1)
            low = int(position)
            high = min(low + 1, len(prices) - 1)
            return prices[low] + (prices[high] - prices[low]) * (position - low)

        return {
            "count": len(prices),
            "days": days,
            "min": prices[0],
            "p25": round(percentile(0.25), 1),
            "median": round(percentile(0.5), 1),
            "p75": round(percentile(0.75), 1),
            "max": prices[-1],
        }

    def reindex(self, *, dry_run: bool = False) -> list[dict]:
        """Re-run the title parser over every stored row.

        A row keeps whatever the parser believed on the day it was first seen,
        so every parser fix leaves history behind: the database still held a
        144 GB RTX 3060 (a `Gaming X 12GB` read as twelve cards) long after
        that bug was fixed. This re-identifies each row from its stored title
        and description and rewrites the derived columns.

        Prices are *not* recomputed - the stored price is what the ad said at
        the time, and `provisional` came from bid data that `raw` no longer
        holds. Only the identification and what follows from it changes.

        Returns one dict per changed row, so the caller can show the diff.
        """
        from . import gpu as gpu_mod
        from .query import score as score_listing

        changes = []
        for row in self.conn.execute("SELECT * FROM listings"):
            payload = json.loads(row["payload"] or "{}")
            match = gpu_mod.identify(row["title"], payload.get("description") or "")
            name = match.name if match else None
            vram = match.total_vram_gb if match else None
            if (name, vram) == (row["gpu_name"], row["vram_gb"]):
                continue

            listing = Listing(
                source=row["source"], external_id=row["external_id"],
                title=row["title"], url=row["url"], price=payload.get("price"),
                currency=payload.get("currency", "SEK"), price_sek=row["price_sek"],
            )
            listing.gpu = match
            per_gb = round(listing.kr_per_gb, 1) if listing.kr_per_gb else None
            new_score = score_listing(listing) if match else 0.0

            changes.append({
                "key": row["key"], "title": row["title"],
                "was": (row["gpu_name"], row["vram_gb"]), "now": (name, vram),
            })
            if dry_run:
                continue

            payload["gpu"] = asdict(match) if match else None
            payload["kr_per_gb"] = per_gb
            payload["score"] = new_score
            self.conn.execute(
                "UPDATE listings SET gpu_name = ?, vram_gb = ?, kr_per_gb = ?,"
                " score = ?, payload = ? WHERE key = ?",
                (name, vram, per_gb, new_score,
                 json.dumps(payload, ensure_ascii=False), row["key"]),
            )
        if not dry_run:
            self.conn.commit()
        return changes

    def known_models(self) -> list[str]:
        return [
            r["gpu_name"]
            for r in self.conn.execute(
                "SELECT DISTINCT gpu_name FROM listings WHERE gpu_name IS NOT NULL"
                " ORDER BY gpu_name"
            )
        ]
