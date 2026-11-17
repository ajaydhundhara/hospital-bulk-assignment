"""Upstream client retry rules (D22) and 429 Retry-After handling."""
import httpx
import pytest

from app import client as client_module
from app.client import HospitalDirectoryClient, UpstreamError


def _client(handler, retries=2):
    return HospitalDirectoryClient(
        "http://u", retries=retries, backoff=0, transport=httpx.MockTransport(handler)
    )


def test_429_waits_for_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})])
    _client(lambda req: next(responses)).activate_batch("b")
    assert sleeps == [7.0]


def test_retry_after_is_capped(monkeypatch):
    sleeps = []
    monkeypatch.setattr(client_module.time, "sleep", sleeps.append)
    responses = iter([httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200, json={})])
    _client(lambda req: next(responses)).activate_batch("b")
    assert sleeps == [client_module.MAX_RETRY_AFTER_SECONDS]


def test_read_timeout_on_idempotent_call_is_retried():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json=[])

    assert _client(handler).get_batch("b") == []
    assert len(calls) == 2


def test_read_timeout_on_create_is_not_retried():
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(UpstreamError) as err:
        _client(handler).create_hospital("A", "B", None, "b")
    assert len(calls) == 1
    assert err.value.retryable and err.value.outcome_unknown


def test_5xx_on_create_is_retried_and_retryable():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503, json={"detail": "down"})

    with pytest.raises(UpstreamError) as err:
        _client(handler, retries=1).create_hospital("A", "B", None, "b")
    assert len(calls) == 2
    assert err.value.retryable and not err.value.outcome_unknown


@pytest.mark.parametrize("code", [502, 504])
def test_gateway_error_on_create_is_not_retried(code):
    """A proxy 502/504 can arrive after the create was applied: don't re-send it blindly."""
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(code, text="Bad Gateway")

    with pytest.raises(UpstreamError) as err:
        _client(handler).create_hospital("A", "B", None, "b")
    assert len(calls) == 1
    assert err.value.retryable and err.value.outcome_unknown and err.value.status_code == code


def test_gateway_error_on_idempotent_call_is_retried():
    responses = iter([httpx.Response(502), httpx.Response(200, json=[])])
    assert _client(lambda req: next(responses)).get_batch("b") == []


def test_4xx_is_final():
    with pytest.raises(UpstreamError) as err:
        _client(lambda req: httpx.Response(422, json={"detail": "bad"})).create_hospital(
            "A", "B", None, "b"
        )
    assert err.value.status_code == 422 and not err.value.retryable
