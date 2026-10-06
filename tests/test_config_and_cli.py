import json

import pytest

from begagnat import cli
from begagnat.config import Config

CONFIG_YAML = """
defaults:
  sources: [blocket, tradera]
  min_vram: 16
  max_kr_per_gb: 150
notify:
  console: true
  ntfy:
    topic: my-gpu-alerts
fx:
  USD: 11.0
source_options:
  ebay:
    domain: ebay.de
profiles:
  - name: p100-billigt
    query: tesla p100
    max_price: 1400
    per_model_max_price:
      Tesla P100: 1400
  - name: 24gb-kort
    query: tesla p40
    min_vram: 24
    sources: [blocket]
    unknown_key_is_ignored: true
"""


def test_config_merges_defaults_into_profiles(tmp_path):
    path = tmp_path / "begagnat.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    config = Config.load(path)

    assert [p.name for p in config.profiles] == ["p100-billigt", "24gb-kort"]

    first = config.profiles[0].criteria
    assert first.query == "tesla p100"
    assert first.max_price == 1400
    assert first.min_vram == 16                     # from defaults
    assert first.max_kr_per_gb == 150               # from defaults
    assert first.sources == ["blocket", "tradera"]  # from defaults
    assert first.per_model_max_price == {"Tesla P100": 1400}

    second = config.profiles[1].criteria
    assert second.min_vram == 24                    # profile overrides defaults
    assert second.sources == ["blocket"]

    assert config.notify["ntfy"]["topic"] == "my-gpu-alerts"
    assert config.fx == {"USD": 11.0}
    assert config.profiles[0].source_options["ebay"]["domain"] == "ebay.de"


def test_missing_config_is_not_fatal_without_an_explicit_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("begagnat.config.DEFAULT_CONFIG_PATHS", [tmp_path / "nope.yaml"])
    assert Config.load().profiles == []


def test_explicit_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------- CLI
def test_search_end_to_end(tmp_path, capsys, monkeypatch, fixture_text, fake_fetcher):
    """The whole pipeline: fetch -> parse -> filter -> rank -> print -> store."""
    fetcher = fake_fetcher(
        {
            "refresh-token": json.dumps({"bearerToken": "tok"}),
            "search_bff": fixture_text("blocket_search.json"),
            "tradera.com": fixture_text("tradera_search.html"),
        }
    )
    monkeypatch.setattr(cli, "make_fetcher", lambda args: fetcher)

    db = tmp_path / "db.sqlite"
    code = cli.main(
        [
            "--db", str(db), "search", "tesla",
            "--source", "blocket", "--source", "tradera",
            "--min-vram", "16", "--max-price", "5000", "--format", "json",
        ]
    )
    assert code == 0

    hits = json.loads(capsys.readouterr().out)
    titles = {hit["title"] for hit in hits}
    # Kept: P100 16GB (1250 kr), 2x P40 (4800 kr).
    assert "Nvidia Tesla P100 16GB HBM2 servergrafikkort" in titles
    assert "2 st Tesla P40 24GB" in titles
    # Dropped: the cooler accessory, the broken M40, the 12GB P100, the 6100 kr 3090.
    assert not any("Kylare" in title for title in titles)
    assert not any("Trasig" in title for title in titles)
    assert not any("12GB" in title for title in titles)

    best = next(hit for hit in hits if hit["gpu"]["name"] == "Tesla P100")
    assert best["price_sek"] == 1250
    assert best["kr_per_gb"] == pytest.approx(78.1, abs=0.1)

    # Results are ranked, and the run was recorded.
    assert [hit["score"] for hit in hits] == sorted((h["score"] for h in hits), reverse=True)
    assert db.exists()


def test_identify_command(capsys):
    assert cli.main(["identify", "2 st Tesla P100 16GB"]) == 0
    out = capsys.readouterr().out
    assert "Tesla P100" in out and "32 GB" in out


def test_search_without_terms_is_an_error():
    with pytest.raises(SystemExit):
        cli.main(["search"])


def test_bad_fx_override_is_an_error(monkeypatch, fake_fetcher):
    monkeypatch.setattr(cli, "make_fetcher", lambda args: fake_fetcher({}))
    with pytest.raises(SystemExit):
        cli.main(["search", "p100", "--fx", "USD"])


def test_watch_once_notifies_only_new_hits(tmp_path, capsys, monkeypatch, fixture_text, fake_fetcher):
    fetcher = fake_fetcher(
        {
            "refresh-token": json.dumps({"bearerToken": "tok"}),
            "search_bff": fixture_text("blocket_search.json"),
        }
    )
    monkeypatch.setattr(cli, "make_fetcher", lambda args: fetcher)
    db = tmp_path / "watch.db"
    argv = ["--db", str(db), "watch", "tesla", "--source", "blocket", "--once", "--min-vram", "16"]

    assert cli.main(argv) == 0
    first = capsys.readouterr().out
    assert "Nvidia Tesla P100 16GB HBM2 servergrafikkort" in first

    # A second pass over unchanged listings must stay quiet.
    assert cli.main(argv) == 0
    second = capsys.readouterr().out
    assert "Tesla P100" not in second
    assert "0 to report" in second


def test_ai_flag_filters_and_reports_cost(tmp_path, capsys, monkeypatch, fixture_text, fake_fetcher):
    """--ai drops a want-to-buy ad the rule engine could not see."""
    from tests.test_ai import StubClient, full, verdict_payload

    fetcher = fake_fetcher({"SEARCH_ID_BAP_COMMON": fixture_text("blocket_recommerce.json")})
    monkeypatch.setattr(cli, "make_fetcher", lambda args: fetcher)

    client = StubClient(
        [verdict_payload(full(0), full(1, keep=False, for_sale=False, reason="Köpes-annons."))]
    )
    real_make_validator = cli.make_validator

    def patched(args, config):
        validator = real_make_validator(args, config)
        if validator is not None:
            validator._client = client
        return validator

    monkeypatch.setattr(cli, "make_validator", patched)

    code = cli.main(
        ["--db", str(tmp_path / "ai.db"), "search", "tesla", "--source", "blocket", "--ai"]
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "Tesla P100" in out
    assert "RTX 3090" not in out       # rejected by the AI
    assert "AI: 1 calls" in out        # the cost line


def test_no_ai_flag_means_no_validator(tmp_path, monkeypatch, fixture_text, fake_fetcher):
    fetcher = fake_fetcher({"SEARCH_ID_BAP_COMMON": fixture_text("blocket_recommerce.json")})
    monkeypatch.setattr(cli, "make_fetcher", lambda args: fetcher)
    args = cli.build_parser().parse_args(["search", "tesla"])
    assert cli.make_validator(args, Config()) is None
