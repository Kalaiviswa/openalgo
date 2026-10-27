"""An order whose response is lost must not be reported as failed (#2067, #2293).

On Windows a read on the shared HTTP/2 connection can fail with
``httpx.ReadError: [WinError 10035]`` after Kite has already accepted the
order. The service turned that into "Failed to place order due to internal
error", the caller placed the order again, and the position doubled.
"""

import httpx
import pytest

from broker.zerodha.api import data as zerodha_data
from broker.zerodha.api import order_api
from broker.zerodha.api.transport import get_with_retry
from broker.zerodha.mapping import transform_data as zerodha_transform

ORDER = {
    "symbol": "CRUDEOIL18NOV265800PE",
    "exchange": "NFO",
    "action": "SELL",
    "pricetype": "MARKET",
    "product": "NRML",
    "quantity": "75",
}

WIN_10035 = "[WinError 10035] A non-blocking socket operation could not be completed immediately"


class FakeKite:
    """Routes Kite calls through httpx.MockTransport and records what was sent."""

    def __init__(self, post_error=None, list_order=True, book_errors=0):
        self.post_error = post_error
        self.list_order = list_order
        self.book_errors = book_errors
        self.posts = []
        self.book_reads = 0
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        if request.method == "POST" and request.url.path == "/orders/regular":
            form = dict(httpx.QueryParams(request.content.decode()))
            self.posts.append(form)
            if self.post_error:
                raise self.post_error("lost", request=request)
            return httpx.Response(200, json={"status": "success", "data": {"order_id": "1"}})
        if request.method == "GET" and request.url.path == "/orders":
            self.book_reads += 1
            if self.book_reads <= self.book_errors:
                raise httpx.ReadError(WIN_10035, request=request)
            rows = [{"order_id": "999", "tag": "openalgoffffffffffff", "tags": []}]
            if self.list_order and self.posts:
                tag = self.posts[-1]["tag"]
                rows.append({"order_id": "260", "tag": tag, "tags": [tag]})
            return httpx.Response(200, json={"status": "success", "data": rows})
        raise AssertionError(f"unexpected call {request.method} {request.url}")


@pytest.fixture
def kite(monkeypatch):
    def install(**kwargs):
        fake = FakeKite(**kwargs)
        monkeypatch.setattr(order_api, "get_httpx_client", lambda: fake.client)
        monkeypatch.setattr(zerodha_transform, "get_br_symbol", lambda s, e: s)
        monkeypatch.setattr(order_api, "_RECONCILE_DELAY_SECONDS", 0)
        return fake

    return install


def test_every_order_carries_its_own_kite_tag(monkeypatch):
    monkeypatch.setattr(zerodha_transform, "get_br_symbol", lambda s, e: s)
    tags = {zerodha_transform.transform_data(dict(ORDER))["tag"] for _ in range(50)}
    assert len(tags) == 50
    for tag in tags:
        assert tag.startswith("openalgo") and tag.isalnum() and len(tag) == 20


def test_a_lost_response_is_answered_with_the_order_kite_holds(kite):
    fake = kite(post_error=httpx.ReadError)
    res, data, order_id = order_api.place_order_api(dict(ORDER), "token")

    assert (res.status, order_id) == (200, "260")
    assert data == {"status": "success", "data": {"order_id": "260"}}
    assert len(fake.posts) == 1, "the order must never be sent twice"


def test_the_lookup_survives_its_own_broken_read(kite):
    fake = kite(post_error=httpx.RemoteProtocolError, book_errors=1)
    res, _, order_id = order_api.place_order_api(dict(ORDER), "token")

    assert (res.status, order_id) == (200, "260")
    assert len(fake.posts) == 1


def test_an_order_kite_does_not_list_is_reported_as_unknown_not_failed(kite):
    fake = kite(post_error=httpx.ReadError, list_order=False)
    res, data, order_id = order_api.place_order_api(dict(ORDER), "token")

    assert order_id is None
    assert res.status == 502
    assert "may have been placed" in data["message"]
    assert "order book" in data["message"]
    assert fake.book_reads == order_api._RECONCILE_ATTEMPTS
    assert len(fake.posts) == 1


def test_an_order_that_never_left_still_raises(kite):
    fake = kite(post_error=httpx.ConnectError)
    with pytest.raises(httpx.ConnectError):
        order_api.place_order_api(dict(ORDER), "token")
    assert fake.book_reads == 0


def test_a_normal_order_is_untouched(kite):
    fake = kite()
    res, data, order_id = order_api.place_order_api(dict(ORDER), "token")
    assert (res.status, order_id) == (200, "1")
    assert fake.book_reads == 0


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.RemoteProtocolError])
def test_a_read_is_sent_again_once(error):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise error(WIN_10035, request=request)
        return httpx.Response(200, json={"status": "success", "data": []})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    assert get_with_retry(client, "https://api.kite.trade/orders", {}).status_code == 200
    assert len(calls) == 2


def test_a_read_that_fails_twice_still_raises():
    def handle(request):
        raise httpx.ReadError(WIN_10035, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with pytest.raises(httpx.ReadError):
        get_with_retry(client, "https://api.kite.trade/orders", {})


def test_a_timeout_is_not_retried():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with pytest.raises(httpx.ReadTimeout):
        get_with_retry(client, "https://api.kite.trade/orders", {})
    assert len(calls) == 1


def test_market_data_reads_use_the_retry(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadError(WIN_10035, request=request)
        return httpx.Response(200, json={"status": "success", "data": {}})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(zerodha_data, "get_httpx_client", lambda: client)
    assert zerodha_data.get_api_response("/quote?i=NSE:INFY", "token")["status"] == "success"
    assert len(calls) == 2
