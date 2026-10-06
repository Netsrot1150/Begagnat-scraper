import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def json(self):
        import json
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeFetcher:
    """Serves canned responses by URL substring, and records what was asked for.

    A route's body may be a callable taking the request params, which is how a
    paginated endpoint is faked: the same URL answers differently per page.
    """

    def __init__(self, routes: dict[str, str]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        params = kwargs.get("params") or {}
        self.calls.append((url, params))
        for needle, body in self.routes.items():
            if needle in url:
                return FakeResponse(body(params) if callable(body) else body)
        raise AssertionError(f"no fixture route matches {url}")

    request = get


@pytest.fixture
def fixture_text():
    return lambda name: (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def fake_fetcher():
    return FakeFetcher
