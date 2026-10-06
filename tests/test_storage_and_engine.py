import json

import pytest

from begagnat.engine import run_search
from begagnat.fx import Fx
from begagnat.models import Listing
from begagnat.query import Criteria, analyse
from begagnat.sources.base import Source, SourceError
from begagnat.storage import Store

FX = Fx.default()


def listing(title="Nvidia Tesla P100 16GB", price=1250, ext="1", source="test"):
    item = Listing(
        source=source, external_id=ext, title=title, url=f"http://x/{ext}",
        price=price, currency="SEK", location="Stockholm", raw={"country": "SE"},
    )
    return analyse(item, Criteria(), FX)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


def test_first_sighting_is_new_second_is_not(store):
    assert store.record(listing())[0] is True
    assert store.record(listing())[0] is False


def test_relisted_ad_with_a_new_id_is_not_reported_again(store):
    store.record(listing(ext="1"))
    is_new, _ = store.record(listing(ext="2"))  # same title, price and location
    assert is_new is False


def test_price_change_is_reported(store):
    store.record(listing(price=1500))
    is_new, previous = store.record(listing(price=1100))
    assert is_new is False
    assert previous == 1500


def test_best_orders_by_score(store):
    store.record(listing(title="Tesla P100 16GB", price=1200, ext="a"))
    store.record(listing(title="Tesla P100 16GB", price=4000, ext="b"))
    rows = store.best()
    assert [row["price_sek"] for row in rows] == [1200, 4000]


def test_price_stats(store):
    for index, price in enumerate([1000, 2000, 3000]):
        store.record(listing(price=price, ext=str(index)))
    stats = store.price_stats("Tesla P100")
    assert stats == {"count": 3, "min": 1000, "median": 2000, "max": 3000}


def test_payload_round_trips(store):
    store.record(listing())
    row = store.best()[0]
    payload = json.loads(row["payload"])
    assert payload["gpu"]["name"] == "Tesla P100"
    assert payload["kr_per_gb"] == pytest.approx(78.1, abs=0.1)


def test_notified_flag(store):
    store.record(listing())
    assert len(store.pending_notification()) == 1
    store.mark_notified([listing().key])
    assert store.pending_notification() == []


# ------------------------------------------------------------------ engine
class StubSource(Source):
    name = "stub"

    def __init__(self, listings):
        super().__init__(fetcher=None)
        self._listings = listings

    def search(self, criteria):
        return list(self._listings)


class BrokenSource(Source):
    name = "broken"

    def __init__(self):
        super().__init__(fetcher=None)

    def search(self, criteria):
        raise SourceError("markup changed")


def raw(title, price, ext):
    return Listing(
        source="stub", external_id=ext, title=title, url=f"http://x/{ext}",
        price=price, currency="SEK", raw={"country": "SE"},
    )


def test_run_search_splits_hits_from_rejects(store):
    source = StubSource(
        [
            raw("Nvidia Tesla P100 16GB", 1250, "1"),
            raw("Tesla P100 16GB", 9000, "2"),          # over budget
            raw("Trasig Tesla P40 24GB", 700, "3"),     # broken
            raw("Cykel till salu", 500, "4"),           # not a GPU
        ]
    )
    result = run_search(Criteria(max_price=2000), [source], FX, store)

    assert [l.external_id for l in result.hits] == ["1"]
    assert len(result.rejected) == 3
    assert result.fetched == 4
    assert len(result.new) == 1


def test_a_broken_source_does_not_sink_the_run(store):
    good = StubSource([raw("Nvidia Tesla P100 16GB", 1250, "1")])
    result = run_search(Criteria(), [good, BrokenSource()], FX, store)

    assert len(result.hits) == 1
    assert "markup changed" in result.errors["broken"]


def test_second_run_reports_price_drop_not_new(store):
    criteria = Criteria()
    run_search(criteria, [StubSource([raw("Tesla P100 16GB", 2000, "1")])], FX, store)
    result = run_search(criteria, [StubSource([raw("Tesla P100 16GB", 1400, "1")])], FX, store)

    assert result.new == []
    assert len(result.price_drops) == 1
    dropped, previous = result.price_drops[0]
    assert previous == 2000 and dropped.price_sek == 1400


def test_hits_are_sorted_best_first(store):
    source = StubSource(
        [
            raw("Tesla P100 16GB", 4000, "1"),
            raw("Tesla P100 16GB", 1200, "2"),
            raw("Tesla P100 16GB", 2400, "3"),
        ]
    )
    result = run_search(Criteria(), [source], FX, store)
    assert [l.external_id for l in result.hits] == ["2", "3", "1"]


# --------------------------------------------------- running auctions (INV-25)


def auction(title="Nvidia Tesla P100 16GB", bid=1, ext="a1", **extra):
    item = Listing(
        source="test", external_id=ext, title=title, url=f"http://x/{ext}",
        price=bid, currency="SEK", location="Stockholm",
        raw={"country": "SE", "is_auction": True, "bid_count": 0,
             "current_bid": bid, "ends_at": "2026-09-11T21:18", **extra},
    )
    return analyse(item, Criteria(), FX)


def test_a_standing_bid_stays_out_of_the_price_statistics(store):
    """One 1 kr opening bid used to halve a thin model's median."""
    for index, price in enumerate([4000, 5000, 6000]):
        store.record(listing(price=price, ext=str(index)))
    store.record(auction(bid=1))

    window = store.model_price_window("Tesla P100")
    assert window["count"] == 3
    assert window["median"] == 5000


def test_a_buy_now_price_does_count(store):
    store.record(listing(price=4000, ext="0"))
    store.record(auction(bid=90, ext="a2", buy_now_price=110))
    assert store.model_price_window("Tesla P100")["count"] == 2


def test_the_auction_block_survives_into_sqlite(store):
    """`raw` is dropped at storage, so the UI needs the curated subset kept."""
    store.record(auction(bid=1))
    payload = json.loads(store.best()[0]["payload"])
    assert payload["price_is_provisional"] is True
    assert payload["auction"]["ends_at"] == "2026-09-11T21:18"


def test_an_older_database_gains_the_provisional_column(tmp_path):
    """A database written before the column existed must still open."""
    import sqlite3

    path = tmp_path / "old.db"
    with Store(path) as first:
        first.record(listing())
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE listings DROP COLUMN provisional")
    conn.commit()
    conn.close()

    with Store(path) as reopened:
        assert reopened.model_price_window("Tesla P100")["count"] == 1


def test_a_restarted_auction_is_not_a_price_drop(store):
    """A bid only falls when the auction restarts, which is not a discount."""
    criteria = Criteria()
    first = Listing(
        source="stub", external_id="1", title="Tesla P100 16GB", url="http://x/1",
        price=2000, currency="SEK",
        raw={"country": "SE", "is_auction": True, "bid_count": 9, "current_bid": 2000},
    )
    again = Listing(
        source="stub", external_id="1", title="Tesla P100 16GB", url="http://x/1",
        price=1, currency="SEK",
        raw={"country": "SE", "is_auction": True, "bid_count": 0, "current_bid": 1},
    )
    run_search(criteria, [StubSource([first])], FX, store)
    result = run_search(criteria, [StubSource([again])], FX, store)
    assert result.price_drops == []


# ---------------------------------------------------------- reindex (INV-29)


def test_reindex_corrects_a_stale_identification(store):
    """A row keeps whatever the parser believed the day it was first seen."""
    store.record(listing())
    store.conn.execute(
        "UPDATE listings SET gpu_name = 'Tesla P100', vram_gb = 144, kr_per_gb = 8.7"
    )
    store.conn.commit()

    changes = store.reindex()

    assert [c["now"] for c in changes] == [("Tesla P100", 16)]
    row = store.best()[0]
    assert row["vram_gb"] == 16
    assert row["kr_per_gb"] == pytest.approx(78.1, abs=0.1)
    assert json.loads(row["payload"])["gpu"]["vram_gb"] == 16


def test_reindex_dry_run_changes_nothing(store):
    store.record(listing())
    store.conn.execute("UPDATE listings SET vram_gb = 144")
    store.conn.commit()

    assert len(store.reindex(dry_run=True)) == 1
    assert store.best()[0]["vram_gb"] == 144


def test_reindex_leaves_a_correct_row_alone(store):
    store.record(listing())
    assert store.reindex() == []


def test_reindex_clears_a_row_the_parser_no_longer_matches(store):
    """"Palit RTX 3060 Ti" was stored as a 12 GB RTX 3060 before INV-19."""
    store.record(listing(title="Palit GeForce RTX 3060 Ti Dual"))
    store.conn.execute("UPDATE listings SET gpu_name = 'RTX 3060', vram_gb = 12")
    store.conn.commit()

    store.reindex()

    # `best()` only shows score > 0, so a cleared row drops out of it entirely.
    assert store.best() == []
    row = store.conn.execute("SELECT * FROM listings").fetchone()
    assert row["gpu_name"] is None
    assert row["score"] == 0.0
