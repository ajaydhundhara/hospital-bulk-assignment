import itertools
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.client import HospitalDirectoryClient
from app.main import app, get_client_factory, get_repository
from app.repository import JsonFileRepository


class FakeUpstream:
    """In-memory stand-in for the Hospital Directory API."""

    def __init__(self):
        self.ids = itertools.count(1)
        self.created = []
        self.activated = []
        self.fail_names = {}  # name -> status code to return
        self.crash_on = None  # name -> raise a non-HTTP exception (simulates a bug)
        self.activate_status = 200
        self.delete_queue = []  # status codes returned by successive batch DELETEs, then 200
        self.deleted_batches = []
        self.calls = []
        # D22 / concurrency scenarios
        self.raise_on = {}  # name -> exception raised *before* creating (request never applied)
        self.lost_response = set()  # names created upstream, but the response is lost (timeout)
        self.gateway_error_after_create = set()  # names created upstream, then a proxy 504
        self.asleep = False  # GET / times out, like a sleeping free-tier instance
        self.get_batch_status = 200
        self.delay = 0.0  # seconds per create, to observe concurrency
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/":
            if self.asleep:
                raise httpx.ReadTimeout("still waking up", request=request)
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "POST" and request.url.path == "/hospitals/":
            with self._lock:
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                return self._create(request)
            finally:
                with self._lock:
                    self.in_flight -= 1
        if request.method == "GET" and request.url.path.startswith("/hospitals/batch/"):
            if self.get_batch_status != 200:
                return httpx.Response(self.get_batch_status, json={"detail": "lookup failed"})
            batch_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json=[h for h in self.created if h["creation_batch_id"] == batch_id]
            )
        if request.method == "PATCH" and request.url.path.endswith("/activate"):
            if self.activate_status != 200:
                return httpx.Response(self.activate_status, json={"detail": "boom"})
            batch_id = request.url.path.split("/")[-2]
            self.activated.append(batch_id)
            for h in self.created:
                if h["creation_batch_id"] == batch_id:
                    h["active"] = True
            return httpx.Response(200, json={"activated": True})
        if request.method == "DELETE" and "/hospitals/batch/" in request.url.path:
            if self.delete_queue:
                code = self.delete_queue.pop(0)
                if code != 200:
                    return httpx.Response(code, json={"detail": "delete refused"})
            batch_id = request.url.path.rsplit("/", 1)[-1]
            mine = [h for h in self.created if h["creation_batch_id"] == batch_id]
            self.created = [h for h in self.created if h["creation_batch_id"] != batch_id]
            self.deleted_batches.append(batch_id)
            return httpx.Response(200, json={"deleted_count": len(mine)})
        return httpx.Response(404)

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if self.delay:
            time.sleep(self.delay)
        if body["name"] == self.crash_on:
            raise RuntimeError("kaboom")
        if body["name"] in self.raise_on:
            raise self.raise_on[body["name"]]
        if body["name"] in self.fail_names:
            code = self.fail_names[body["name"]]
            return httpx.Response(code, json={"detail": "simulated failure"})
        hospital = {"id": next(self.ids), "active": False, **body}
        self.created.append(hospital)
        if body["name"] in self.lost_response:
            raise httpx.ReadTimeout("response lost", request=request)
        if body["name"] in self.gateway_error_after_create:
            return httpx.Response(504, text="Gateway Timeout")
        return httpx.Response(201, json=hospital)


@pytest.fixture
def upstream():
    return FakeUpstream()


@pytest.fixture
def repo(tmp_path):
    return JsonFileRepository(tmp_path)


@pytest.fixture
def client(upstream, repo):
    def _factory():
        return HospitalDirectoryClient(
            "http://upstream.test",
            retries=1,
            backoff=0,
            transport=httpx.MockTransport(upstream),
        )

    app.dependency_overrides[get_client_factory] = lambda: _factory
    app.dependency_overrides[get_repository] = lambda: repo
    yield TestClient(app)
    app.dependency_overrides.clear()


def csv_file(text: str, name: str = "hospitals.csv"):
    return {"file": (name, text.encode(), "text/csv")}
