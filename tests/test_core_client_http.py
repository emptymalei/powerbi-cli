"""The client against a real HTTP server on localhost.

The other client tests use a fake transport; these check what only real sockets show:
the retry adapter of the default session, the headers on the wire and the answer to a
server that keeps failing. Nothing leaves the machine.
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from core_helpers import make_token

from pbi_cli.core.auth import Credentials
from pbi_cli.core.client import PowerBIClient
from pbi_cli.errors import ApiError, RateLimitError, TokenExpiredError


@pytest.fixture
def api(monkeypatch):
    """A scripted server: ``script["responses"]`` are answered in order."""
    script = {"responses": [], "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, body=None):
            script["requests"].append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                }
            )
            status, headers, payload = script["responses"].pop(0)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._answer()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self._answer(self.rfile.read(length))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # the retry adapter backs off between attempts; do not really wait for it
    monkeypatch.setattr("urllib3.util.retry.time.sleep", lambda seconds: None)
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1.0/myorg"
    try:
        yield script, base_url
    finally:
        server.shutdown()
        server.server_close()


def make_client(base_url, sleep=lambda seconds: None):
    # real sockets, real clock: the token must be valid now
    token = make_token(now=datetime.now(timezone.utc), expires_in=timedelta(hours=1))
    credentials = Credentials(token, profile="p", group="admin")
    return PowerBIClient(lambda: credentials, base_url=base_url, sleep=sleep)


def test_a_request_over_the_wire(api):
    script, base_url = api
    script["responses"] = [(200, {}, {"value": [{"id": "a1"}]})]
    with make_client(base_url) as client:
        response = client.request("admin.apps", {"$top": 5})
    assert response.data == {"value": [{"id": "a1"}]}
    sent = script["requests"][0]
    assert sent["path"] == "/v1.0/myorg/admin/apps?%24top=5"
    assert sent["headers"]["Authorization"].startswith("Bearer ")
    assert sent["headers"]["User-Agent"].startswith("pbi-cli")
    assert sent["headers"]["Accept"] == "application/json"


def test_a_read_is_retried_after_a_server_error(api):
    script, base_url = api
    script["responses"] = [(500, {}, {"error": "boom"}), (200, {}, {"value": []})]
    with make_client(base_url) as client:
        assert client.request("admin.apps").data == {"value": []}
    assert len(script["requests"]) == 2


def test_a_server_that_keeps_failing_is_an_api_error(api):
    script, base_url = api
    script["responses"] = [(500, {}, {"error": "boom"})] * 4
    with make_client(base_url) as client:
        with pytest.raises(ApiError) as excinfo:
            client.request("admin.apps")
    assert "admin.apps" in str(excinfo.value)
    assert len(script["requests"]) == 4  # the first try and three retries


def test_throttling_is_waited_out(api):
    script, base_url = api
    script["responses"] = [
        (429, {"Retry-After": "3"}, {"error": {"code": "TooManyRequests"}}),
        (200, {}, {"value": [{"id": "a"}]}),
    ]
    waits = []
    with make_client(base_url, sleep=waits.append) as client:
        assert client.request("admin.apps").data == {"value": [{"id": "a"}]}
    assert waits == [3.0]
    assert len(script["requests"]) == 2


def test_a_long_retry_after_reaches_the_client_instead_of_being_slept_out(api):
    script, base_url = api
    script["responses"] = [
        (429, {"Retry-After": "3000"}, {"error": {"code": "TooManyRequests"}})
    ]
    waits = []
    with make_client(base_url, sleep=waits.append) as client:
        with pytest.raises(RateLimitError) as excinfo:
            client.request("admin.groups", {"$top": 1})
    assert excinfo.value.retry_after == 3000
    assert (
        len(script["requests"]) == 1
    )  # the retry adapter did not retry it behind our back
    assert waits == []


def test_a_post_carries_its_body_and_is_not_retried_on_a_server_error(api):
    script, base_url = api
    script["responses"] = [(202, {}, {"id": "scan-1", "status": "NotStarted"})]
    with make_client(base_url) as client:
        response = client.request(
            "admin.scan.start", {"lineage": True}, body={"workspaces": ["w1"]}
        )
    assert response.status == 202
    sent = script["requests"][0]
    assert sent["method"] == "POST"
    assert json.loads(sent["body"]) == {"workspaces": ["w1"]}
    assert sent["headers"]["Content-Type"] == "application/json"

    script["responses"] = [(500, {}, {"error": "boom"}), (202, {}, {"id": "scan-2"})]
    script["requests"].clear()
    with make_client(base_url) as client:
        with pytest.raises(ApiError):
            client.request("admin.scan.start", body={"workspaces": ["w1"]})
    assert len(script["requests"]) == 1  # a scan must not be started twice


def test_401_over_the_wire(api):
    script, base_url = api
    script["responses"] = [(401, {}, {"error": {"code": "TokenExpired"}})]
    with make_client(base_url) as client:
        with pytest.raises(TokenExpiredError):
            client.request("admin.apps")


def test_a_continuation_link_to_another_port_is_refused(api):
    script, base_url = api
    script["responses"] = [
        (
            200,
            {},
            {
                "artifactAccessEntities": [],
                "continuationUri": "http://127.0.0.1:1/steal",
            },
        ),
    ]
    with make_client(base_url) as client:
        with pytest.raises(ApiError, match="Refusing"):
            client.fetch("admin.users.artifact_access", {"userId": "u"})
    assert len(script["requests"]) == 1
