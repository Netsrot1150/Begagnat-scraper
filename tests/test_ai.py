"""AI validation tests.

The Anthropic client is stubbed, so these tests exercise our prompt assembly,
batching, caching, schema handling and verdict application - never the network.
"""

import json

import pytest

from begagnat import ai as ai_mod
from begagnat.ai import Validator, Verdict, apply, build_system_prompt
from begagnat.engine import run_search
from begagnat.fx import Fx
from begagnat.models import Listing
from begagnat.query import Criteria, analyse
from begagnat.sources.base import Source
from begagnat.storage import Store

FX = Fx.default()


class StubBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class StubUsage:
    def __init__(self, input_tokens=1000, output_tokens=200):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class StubResponse:
    def __init__(self, payload):
        self.content = [StubBlock(json.dumps(payload))]
        self.usage = StubUsage()


class StubClient:
    """Records requests and replays queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("StubClient ran out of queued responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return StubResponse(response)


def listing(title, price=1250, ext="1", description=""):
    item = Listing(
        source="test", external_id=ext, title=title, url=f"http://x/{ext}",
        price=price, currency="SEK", description=description, raw={"country": "SE"},
    )
    return analyse(item, Criteria(), FX)


def verdict_payload(*items):
    return {"verdicts": list(items)}


def full(index, **overrides):
    base = {
        "index": index, "keep": True, "for_sale": True, "whole_system": False,
        "gpu_model": "Tesla P100", "vram_gb": 16, "quantity": 1,
        "condition": "working", "mining_risk": False, "risk_flags": [],
        "confidence": 0.95, "reason": "Ser ut som ett riktigt kort.",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------- prompt
def test_system_prompt_carries_the_catalogue_and_the_key_rules():
    prompt = build_system_prompt()
    assert "Tesla P100" in prompt and "RTX 3090" in prompt
    assert "köpes" in prompt          # want-to-buy ads
    assert "whole_system" in prompt   # complete computers
    # Listing text is data, not instructions.
    assert "ignore them" in prompt


def test_request_uses_structured_output_and_caches_the_system_prompt():
    client = StubClient([verdict_payload(full(0))])
    Validator(client=client).validate([listing("Tesla P100 16GB")])

    request = client.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The volatile part (the listings) must come after the cached prefix.
    assert "Tesla P100 16GB" in request["messages"][0]["content"]
    assert "Tesla P100 16GB" not in request["system"][0]["text"]


# --------------------------------------------------------------- batching
def test_listings_are_split_into_batches():
    client = StubClient(
        [verdict_payload(full(0), full(1)), verdict_payload(full(0))]
    )
    listings = [listing(f"Tesla P100 16GB nr {i}", ext=str(i)) for i in range(3)]
    verdicts = Validator(client=client, batch_size=2).validate(listings)

    assert len(client.requests) == 2
    assert len(verdicts) == 3


def test_indices_map_back_to_the_right_listing():
    client = StubClient(
        [verdict_payload(full(0, keep=True), full(1, keep=False, for_sale=False))]
    )
    a, b = listing("Tesla P100 16GB", ext="a"), listing("Tesla P100 16GB", ext="b")
    verdicts = Validator(client=client).validate([a, b])

    assert verdicts[a.key].keep is True
    assert verdicts[b.key].keep is False


def test_out_of_range_index_is_ignored_not_crashing():
    client = StubClient([verdict_payload(full(0), full(7))])
    verdicts = Validator(client=client).validate([listing("Tesla P100 16GB")])
    assert len(verdicts) == 1


def test_a_failed_batch_does_not_raise():
    client = StubClient([RuntimeError("API down"), verdict_payload(full(0))])
    listings = [listing("Tesla P100 16GB", ext=str(i)) for i in range(2)]
    verdicts = Validator(client=client, batch_size=1).validate(listings)

    # First batch lost, second survived.
    assert len(verdicts) == 1


# --------------------------------------------------------------- caching
def test_verdicts_are_cached_and_not_paid_for_twice(tmp_path):
    with Store(tmp_path / "ai.db") as store:
        client = StubClient([verdict_payload(full(0))])
        item = listing("Tesla P100 16GB")

        first = Validator(client=client, store=store)
        first.validate([item])
        assert len(client.requests) == 1

        second = Validator(client=client, store=store)
        verdicts = second.validate([item])
        assert len(client.requests) == 1        # no second call
        assert second.cached_verdicts == 1
        assert verdicts[item.key].gpu_model == "Tesla P100"


def test_an_edited_listing_is_judged_again(tmp_path):
    with Store(tmp_path / "ai.db") as store:
        client = StubClient([verdict_payload(full(0)), verdict_payload(full(0))])
        validator = Validator(client=client, store=store)

        validator.validate([listing("Tesla P100 16GB", price=1250)])
        validator.validate([listing("Tesla P100 16GB", price=900)])  # price changed
        assert len(client.requests) == 2


# --------------------------------------------------------------- applying
def test_want_to_buy_ad_is_rejected():
    item = listing("Tesla P40 24GB sökes")
    apply(item, Verdict(keep=False, for_sale=False, reason="Detta är en köpes-annons."))
    assert "köpes" in item.rejected_reason


def test_whole_computer_is_rejected():
    item = listing("Gamingdator med RTX 3090", price=15000)
    apply(item, Verdict(keep=False, for_sale=True, whole_system=True))
    assert "hel dator" in item.rejected_reason


def test_ai_corrects_the_vram_and_the_quantity():
    item = listing("Tesla V100 säljes")          # catalogue default is 16 GB
    assert item.gpu.vram_gb == 16
    apply(item, Verdict(keep=True, vram_gb=32, quantity=2, confidence=0.9))
    assert item.gpu.vram_gb == 32
    assert item.gpu.total_vram_gb == 64


def test_low_confidence_verdict_does_not_overwrite_the_catalogue():
    item = listing("Tesla P100 16GB")
    apply(item, Verdict(keep=True, vram_gb=12, confidence=0.3))
    assert item.gpu.vram_gb == 16


def test_rescued_listing_gets_catalogue_figures_when_the_model_is_known():
    item = Listing(source="test", external_id="r", title="Nvidia grafikkort 24GB",
                   url="http://x", price=2000, currency="SEK", raw={"country": "SE"})
    apply(item, Verdict(keep=True, gpu_model="Tesla P40", vram_gb=24, confidence=0.8))

    assert item.gpu.name == "Tesla P40"
    assert item.gpu.bandwidth_gbs == 346     # from the catalogue, not from Claude
    assert item.gpu.ai_tier == "good"


def test_rescued_unknown_model_is_kept_but_marked():
    item = Listing(source="test", external_id="r", title="Moore Threads MTT S80 16GB",
                   url="http://x", price=2000, currency="SEK", raw={"country": "SE"})
    apply(item, Verdict(keep=True, gpu_model="Moore Threads MTT S80", vram_gb=16, confidence=0.8))

    assert item.gpu.name == "Moore Threads MTT S80"
    assert item.gpu.vram_gb == 16
    assert "katalogen" in item.gpu.notes
    assert item.gpu.confidence <= 0.7


# --------------------------------------------------------------- engine
class StubSource(Source):
    name = "stub"

    def __init__(self, listings):
        super().__init__(fetcher=None)
        self._listings = listings

    def search(self, criteria):
        return list(self._listings)


def raw(title, price, ext, description=""):
    return Listing(
        source="stub", external_id=ext, title=title, url=f"http://x/{ext}",
        price=price, currency="SEK", description=description, raw={"country": "SE"},
    )


def test_engine_moves_ai_rejected_listings_out_of_hits(tmp_path):
    client = StubClient(
        [verdict_payload(full(0), full(1, keep=False, for_sale=False, reason="Köpes."))]
    )
    source = StubSource(
        [raw("Tesla P100 16GB", 1250, "1"), raw("Tesla P100 16GB köpes", 1250, "2")]
    )
    with Store(tmp_path / "e.db") as store:
        result = run_search(
            Criteria(), [source], FX, store, validator=Validator(client=client)
        )

    assert [l.external_id for l in result.hits] == ["1"]
    assert any("köpes" in l.rejected_reason for l in result.rejected)
    assert "AI:" in result.ai_summary


def test_rescue_promotes_an_unrecognised_card(tmp_path):
    client = StubClient(
        [verdict_payload(full(0, gpu_model="Tesla P40", vram_gb=24, confidence=0.85))]
    )
    source = StubSource([raw("Nvidia servergrafikkort 24GB HBM", 2000, "1")])
    with Store(tmp_path / "r.db") as store:
        result = run_search(
            Criteria(), [source], FX, store,
            validator=Validator(client=client), rescue=True,
        )

    assert len(result.hits) == 1
    assert result.hits[0].gpu.name == "Tesla P40"
    assert result.hits[0].score > 0


def test_rescue_does_not_resurrect_an_over_budget_listing(tmp_path):
    client = StubClient([verdict_payload(full(0))])
    source = StubSource([raw("Tesla P100 16GB", 9000, "1")])
    with Store(tmp_path / "b.db") as store:
        result = run_search(
            Criteria(max_price=2000), [source], FX, store,
            validator=Validator(client=client), rescue=True,
        )

    assert result.hits == []
    assert "over budget" in result.rejected[0].rejected_reason
    # The over-budget listing was never sent to the API.
    assert client.requests == []


def test_a_dead_api_leaves_the_rule_engine_verdict_standing(tmp_path):
    client = StubClient([RuntimeError("no credit")])
    source = StubSource([raw("Tesla P100 16GB", 1250, "1")])
    with Store(tmp_path / "d.db") as store:
        result = run_search(
            Criteria(), [source], FX, store, validator=Validator(client=client)
        )

    assert len(result.hits) == 1
    assert result.hits[0].ai is None


def test_cost_estimate_uses_the_model_price():
    validator = Validator(client=StubClient([verdict_payload(full(0))]))
    validator.validate([listing("Tesla P100 16GB")])
    # 1000 input @ $5/M + 200 output @ $25/M
    assert validator.cost_usd() == pytest.approx(0.010, abs=1e-4)


def test_verdict_appears_in_the_serialised_listing():
    item = listing("Tesla P100 16GB")
    apply(item, Verdict(keep=True, risk_flags=["passiv kylning"], mining_risk=True))
    payload = item.to_dict()
    assert payload["ai"]["risk_flags"] == ["passiv kylning"]
    assert payload["ai"]["mining_risk"] is True
