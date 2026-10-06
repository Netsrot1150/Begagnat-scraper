"""Watches, the event feed, and the web API.

The rules that matter here are about *announcing*: a watch must announce a hit
once and only once, two watches must each get their own announcement for the
same ad, and the API must never let a caller widen a search past what the rule
engine allows.
"""

import json

import pytest
from fastapi.testclient import TestClient

from begagnat.models import GpuMatch, Listing
from begagnat.storage import Store
from begagnat.watchrunner import watch_to_dict
from begagnat.web.app import create_app


def make_listing(external_id="1", price_sek=1200.0, title="Nvidia Tesla P100 16GB"):
    listing = Listing(
        source="blocket", external_id=external_id, title=title,
        url=f"https://example.invalid/{external_id}", price=price_sek, currency="SEK",
    )
    listing.price_sek = price_sek
    listing.gpu = GpuMatch(
        name="Tesla P100", vendor="NVIDIA", arch="Pascal", vram_gb=16,
        ai_tier="good", bandwidth_gbs=732,
    )
    listing.score = 55.0
    return listing


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as db:
        yield db


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "web.db", run_scheduler=False)
    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------------ storage


def test_a_watch_announces_a_listing_once(store):
    watch_id = store.save_watch("p100", {"query": "tesla p100"})
    store.record(make_listing())

    assert store.link_watch_hit(watch_id, "blocket:1") is True
    assert store.link_watch_hit(watch_id, "blocket:1") is False


def test_two_watches_each_announce_the_same_listing(store):
    """A listing already in `listings` is still new *to a watch that has not seen it*."""
    first = store.save_watch("billiga", {"query": "tesla"})
    second = store.save_watch("stora", {"min_vram": 16})
    store.record(make_listing())

    assert store.link_watch_hit(first, "blocket:1") is True
    assert store.link_watch_hit(second, "blocket:1") is True


def test_a_watch_is_due_when_its_interval_has_elapsed(store):
    from datetime import timedelta

    from begagnat.models import utcnow

    watch_id = store.save_watch("p100", {"query": "x"}, interval_s=900)
    assert [w["id"] for w in store.due_watches()] == [watch_id], "never-run watches are due"

    store.finish_watch_run(watch_id, hits=0)
    assert store.due_watches() == []
    assert [w["id"] for w in store.due_watches(utcnow() + timedelta(seconds=901))] == [watch_id]


def test_a_disabled_watch_is_never_due(store):
    store.save_watch("paused", {"query": "x"}, enabled=False)
    assert store.due_watches() == []


def test_deleting_a_watch_takes_its_events_and_hits_with_it(store):
    watch_id = store.save_watch("p100", {"query": "x"})
    store.link_watch_hit(watch_id, "blocket:1")
    store.add_event("new", watch_id=watch_id, key="blocket:1")

    store.delete_watch(watch_id)

    assert store.list_watches() == []
    assert store.events() == []
    assert store.watch_hit_keys(watch_id) == []


def test_flags_survive_a_rescrape_of_the_listing(store):
    """The user's star is theirs; re-recording the ad must not clear it."""
    store.record(make_listing())
    store.set_flag("blocket:1", starred=True, note="ringt säljaren")

    store.record(make_listing(price_sek=1100.0))

    assert store.flags()["blocket:1"] == {
        "starred": True, "hidden": False, "note": "ringt säljaren"
    }


def test_an_unknown_flag_is_refused(store):
    with pytest.raises(ValueError, match="unknown listing flag"):
        store.set_flag("blocket:1", sold=True)


def test_model_price_window_reports_percentiles(store):
    for index, price in enumerate([800, 1000, 1200, 1400, 2000]):
        store.record(make_listing(external_id=str(index), price_sek=float(price)))

    window = store.model_price_window("Tesla P100", days=30)

    assert window["count"] == 5
    assert window["min"] == 800
    assert window["median"] == 1200
    assert window["p25"] == 1000
    assert window["max"] == 2000


def test_model_price_window_is_empty_for_an_unseen_model(store):
    assert store.model_price_window("RTX 5090") == {}


# ---------------------------------------------------------------- watch API


def test_the_api_round_trips_a_watch(client):
    created = client.post("/api/watches", json={
        "name": "P100-fynd",
        "interval_s": 1800,
        "criteria": {"query": "tesla p100", "max_price": 1400, "min_vram": 16},
    }).json()

    assert created["name"] == "P100-fynd"
    assert created["criteria"]["max_price"] == 1400
    assert created["interval_s"] == 1800

    listed = client.get("/api/watches").json()
    assert [w["id"] for w in listed] == [created["id"]]

    updated = client.put(f"/api/watches/{created['id']}", json={
        **created, "enabled": False,
    }).json()
    assert updated["enabled"] is False

    assert client.delete(f"/api/watches/{created['id']}").status_code == 200
    assert client.get("/api/watches").json() == []


def test_a_watch_without_a_query_or_a_model_is_refused(client):
    response = client.post("/api/watches", json={"name": "tom", "criteria": {"max_price": 100}})
    assert response.status_code == 400
    assert "query" in response.json()["error"]


def test_unknown_criteria_keys_are_dropped_rather_than_stored(client):
    """An older UI must not be able to smuggle fields into Criteria."""
    created = client.post("/api/watches", json={
        "name": "x", "criteria": {"query": "p100", "delete_everything": True},
    }).json()
    assert "delete_everything" not in created["criteria"]


def test_updating_a_missing_watch_is_a_404(client):
    assert client.put("/api/watches/999", json={
        "name": "x", "criteria": {"query": "p100"},
    }).status_code == 404


# -------------------------------------------------------------- listing API


def test_listings_are_filtered_and_flagged(client, tmp_path):
    with Store(tmp_path / "web.db") as db:
        db.record(make_listing("1", 1200.0))
        db.record(make_listing("2", 4000.0, title="Nvidia Tesla P100 16GB begagnad"))

    assert len(client.get("/api/listings").json()) == 2
    assert len(client.get("/api/listings?max_price=2000").json()) == 1

    client.post("/api/listings/blocket:2/flags", json={"hidden": True})
    assert [l["key"] for l in client.get("/api/listings").json()] == ["blocket:1"]
    assert len(client.get("/api/listings?include_hidden=true").json()) == 2

    client.post("/api/listings/blocket:1/flags", json={"starred": True})
    assert [l["key"] for l in client.get("/api/listings?starred=true").json()] == ["blocket:1"]


def test_listing_detail_carries_price_history_and_the_model_window(client, tmp_path):
    with Store(tmp_path / "web.db") as db:
        db.record(make_listing("1", 1500.0))
        db.record(make_listing("1", 1200.0))

    detail = client.get("/api/listings/blocket:1").json()

    assert detail["price_sek"] == 1200
    assert len(detail["price_history"]) >= 1
    assert detail["model_prices"]["count"] >= 1


def test_a_missing_listing_is_a_404_not_a_crash(client):
    assert client.get("/api/listings/blocket:nope").status_code == 404


def test_setting_no_known_flag_is_rejected(client):
    response = client.post("/api/listings/blocket:1/flags", json={"sold": True})
    assert response.status_code == 400


# ------------------------------------------------------------------ events


def test_the_feed_reports_and_then_clears_unseen_events(client, tmp_path):
    with Store(tmp_path / "web.db") as db:
        watch_id = db.save_watch("p100", {"query": "tesla p100"})
        db.add_event("new", watch_id=watch_id, key="blocket:1",
                     payload={"title": "Tesla P100", "price_sek": 1200})

    assert client.get("/api/status").json()["unseen_events"] == 1

    events = client.get("/api/events").json()
    assert events[0]["kind"] == "new"
    assert events[0]["watch_name"] == "p100"
    assert events[0]["payload"]["price_sek"] == 1200

    assert client.post("/api/events/seen", json={}).json()["unseen"] == 0
    assert client.get("/api/events?unseen_only=true").json() == []


# ------------------------------------------------------------------ search


def test_the_search_endpoint_refuses_an_empty_query(client):
    assert client.post("/api/search", json={"criteria": {"max_price": 100}}).status_code == 400


def test_status_lists_the_sources_the_ui_can_offer(client):
    status = client.get("/api/status").json()
    names = {source["name"] for source in status["sources"]}
    assert {"blocket", "tradera", "ebay", "marketplace"} <= names
    assert status["scheduler_running"] is False


def test_watch_to_dict_parses_the_stored_json(store):
    watch_id = store.save_watch("p100", {"query": "tesla p100"}, source_options={"ebay": {}})
    data = watch_to_dict(store.get_watch(watch_id))

    assert data["criteria"] == {"query": "tesla p100"}
    assert data["source_options"] == {"ebay": {}}
    assert json.dumps(data)  # the UI has to be able to serialise it


def test_a_fresh_search_result_has_the_same_shape_as_a_stored_one():
    """The UI renders both through one row template, so the keys must match."""
    from begagnat.web.app import _listing_to_dict, _row_to_dict

    listing = make_listing()
    with Store(":memory:") as db:
        db.record(listing)
        stored = _row_to_dict(
            db.conn.execute("SELECT * FROM listings WHERE key = ?", (listing.key,)).fetchone(),
            {},
        )
    fresh = _listing_to_dict(listing, {})

    for field in ("key", "gpu", "vram_gb", "kr_per_gb", "score", "price_sek",
                  "title", "url", "source", "last_seen", "starred", "hidden", "note"):
        assert field in fresh, f"{field} missing from a fresh search result"
        assert field in stored, f"{field} missing from a stored listing"

    assert fresh["gpu"] == stored["gpu"] == "Tesla P100"
    assert fresh["vram_gb"] == stored["vram_gb"] == 16
    assert isinstance(fresh["gpu_detail"], dict), "the full match stays available"


def test_a_hidden_ad_does_not_move_the_median(store):
    """Hiding an ad says "this is not an offer for this model" - the price
    statistics have to agree, or one case listed under RTX 5090 sets the median."""
    for index, price in enumerate([1800, 1900, 2000]):
        store.record(make_listing(external_id=str(index), price_sek=float(price)))
    store.record(make_listing(external_id="junk", price_sek=149.0))
    assert store.model_price_window("Tesla P100")["min"] == 149

    store.set_flag("blocket:junk", hidden=True)

    window = store.model_price_window("Tesla P100")
    assert window["count"] == 3
    assert window["min"] == 1800


def test_a_watch_inherits_credentials_from_the_config_file(store, monkeypatch):
    """Nobody should have to type an API secret into a web form."""
    from begagnat.config import Config
    from begagnat import watchrunner

    watch_id = store.save_watch("p100", {"query": "tesla p100", "sources": ["ebay"]},
                                source_options={"ebay": {"domain": "ebay.de"}})
    seen = {}

    def fake_make_sources(names, fetcher, options):
        seen.update(options)
        return []

    monkeypatch.setattr(watchrunner, "make_sources", fake_make_sources)
    config = Config.from_dict({"source_options": {"ebay": {"client_id": "id", "client_secret": "sec"}}})

    watchrunner.run_watch(store, store.get_watch(watch_id), config=config, fetcher=object())

    assert seen["ebay"] == {"client_id": "id", "client_secret": "sec", "domain": "ebay.de"}
