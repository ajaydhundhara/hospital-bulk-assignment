import random
import time
from typing import Any, Dict, List, Optional

import httpx

# The request never reached the server, so sending it again cannot create a duplicate.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
MAX_RETRY_AFTER_SECONDS = 30.0
# Gateway errors: a proxy (Render's) answered, but the app behind it may have finished
# the request. For a create that means "maybe applied", like a timeout.
_GATEWAY_ERRORS = (502, 504)


class UpstreamError(Exception):
    """A call to the Hospital Directory API failed (after any retries).

    retryable:       trying again later may succeed (5xx, 429, network problems).
                     False for other 4xx: the data or request itself was rejected.
    outcome_unknown: the request may have been applied upstream even though we got no
                     response (e.g. a read timeout on a create). Resume checks upstream
                     before sending such a row again.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        retryable: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(
            f"{'.'.join(str(p) for p in d.get('loc', []))}: {d.get('msg')}" for d in detail
        )
    return str(detail or resp.text[:200] or resp.reason_phrase)


def _retry_after(resp: httpx.Response) -> Optional[float]:
    try:
        return min(max(float(resp.headers["Retry-After"]), 0.0), MAX_RETRY_AFTER_SECONDS)
    except (KeyError, ValueError):
        return None  # absent, or an HTTP date (rare); fall back to backoff


class HospitalDirectoryClient:
    """Thin wrapper over the upstream API with retry on transient failures.

    Thread-safe (one pooled `httpx.Client`), so one instance can serve all the
    concurrent creates of a batch.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        retries: int = 2,
        backoff: float = 0.5,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._retries = retries
        self._backoff = backoff

    def close(self) -> None:
        self._http.close()

    def wake(self, timeout: float) -> None:
        """Make sure upstream is up before sending creates (`GET /`, one attempt).

        A free Render instance sleeps when idle and takes ~30-60 s to answer the first
        request. Waiting once here, with a long timeout, is better than sending 10
        creates that all time out with an unknown outcome. Any answer below 500 counts
        as awake.
        """
        try:
            resp = self._http.get("/", timeout=timeout)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Upstream did not respond within {timeout:g} s ({exc.__class__.__name__})",
                retryable=True,
            )
        if resp.status_code >= 500:
            raise UpstreamError(
                f"Upstream is not ready ({resp.status_code})", resp.status_code, retryable=True
            )

    def _backoff_delay(self, attempt: int) -> float:
        # Exponential backoff with jitter, so concurrent rows don't retry in lockstep.
        return self._backoff * 2 ** (attempt - 1) * random.uniform(0.5, 1.5)

    def _request(
        self, method: str, url: str, idempotent: bool = True, **kwargs: Any
    ) -> httpx.Response:
        """Send a request, retrying transient failures.

        A non-idempotent request (create) is retried only when we know it was not
        applied: it never left (connection refused / pool timeout) or the app answered
        500 / 503 / 429. If it may have been applied (timeout, connection dropped after
        sending, or a 502 / 504 from the gateway in front of the app) it is NOT retried;
        the error is marked `outcome_unknown` instead and resume checks upstream (D22).
        """
        last: Optional[UpstreamError] = None
        delay: Optional[float] = None
        for attempt in range(self._retries + 1):
            if attempt:
                time.sleep(delay if delay is not None else self._backoff_delay(attempt))
                delay = None
            try:
                resp = self._http.request(method, url, **kwargs)
            except _NOT_SENT as exc:
                last = UpstreamError(
                    f"Upstream connection error: {exc.__class__.__name__}", retryable=True
                )
                continue
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                what = (
                    "Upstream request timed out"
                    if isinstance(exc, httpx.TimeoutException)
                    else f"Upstream connection error: {exc.__class__.__name__}"
                )
                if not idempotent:
                    raise UpstreamError(
                        f"{what}; the request may have been applied (outcome unknown)",
                        retryable=True,
                        outcome_unknown=True,
                    )
                last = UpstreamError(what, retryable=True)
                continue
            if not idempotent and resp.status_code in _GATEWAY_ERRORS:
                raise UpstreamError(
                    f"Upstream gateway error {resp.status_code}; the request may have been "
                    "applied (outcome unknown)",
                    resp.status_code,
                    retryable=True,
                    outcome_unknown=True,
                )
            if resp.status_code >= 500 or resp.status_code == 429:
                last = UpstreamError(
                    f"Upstream error {resp.status_code}: {_detail(resp)}",
                    resp.status_code,
                    retryable=True,
                )
                if resp.status_code == 429:
                    delay = _retry_after(resp)
                continue
            if resp.status_code >= 400:  # client errors are not retryable
                raise UpstreamError(
                    f"Upstream rejected request ({resp.status_code}): {_detail(resp)}",
                    resp.status_code,
                )
            return resp
        assert last is not None
        raise last

    def create_hospital(
        self, name: str, address: str, phone: Optional[str], batch_id: str
    ) -> Dict[str, Any]:
        payload = {
            "name": name,
            "address": address,
            "phone": phone,
            "creation_batch_id": batch_id,
        }
        return self._request("POST", "/hospitals/", idempotent=False, json=payload).json()

    def get_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        """Every hospital upstream that carries this batch id."""
        data = self._request("GET", f"/hospitals/batch/{batch_id}").json()
        return data if isinstance(data, list) else []

    def activate_batch(self, batch_id: str) -> None:
        self._request("PATCH", f"/hospitals/batch/{batch_id}/activate")

    def delete_batch(self, batch_id: str) -> int:
        """Delete every hospital in the batch upstream; returns how many were deleted."""
        resp = self._request("DELETE", f"/hospitals/batch/{batch_id}")
        try:
            return int(resp.json().get("deleted_count", 0))
        except (ValueError, AttributeError, TypeError):
            return 0
