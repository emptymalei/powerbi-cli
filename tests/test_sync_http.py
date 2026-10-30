"""The sync engine against a real HTTP server on localhost, with many threads.

The other sync tests use a fake transport. These check what only real sockets show: that
16 workers can share a session without the connection pool running dry, and that a whole
sync works over HTTP. The server is the fake Power BI service behind a socket; nothing
leaves the machine.
"""

import logging
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from core_helpers import make_token
from fake_powerbi import PREFIX, FakePowerBI

from pbi_cli.core.auth import Credentials
from pbi_cli.core.client import PowerBIClient
from pbi_cli.core.ratelimit import Limiter, QuotaTracker
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.engine import COMPLETED, SyncEngine
from pbi_cli.core.sync.plan import SyncOptions
from pbi_cli.core.sync.runners import DONE


@pytest.fixture
def server():
    """The fake service behind a real socket: ``(service, base url)``."""
    service = FakePowerBI(workspaces=30, reports=200, datasets=10)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep connections alive, as the real API does

        def _answer(self, body=None):
            time.sleep(
                0.02
            )  # a real API takes time: this is what makes workers overlap
            prepared = requests.Request(
                self.command,
                f"http://127.0.0.1:{self.server.server_port}{self.path}",
                data=body,
            ).prepare()
            response = service.send(prepared)
            data = response.content or b""
            self.send_response(response.status_code)
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

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield service, f"http://127.0.0.1:{httpd.server_port}{PREFIX}"
    httpd.shutdown()
    httpd.server_close()


def engine_for(base_url, tmp_path):
    token = make_token(tenant="tenant-1", expires_in=timedelta(days=3650))
    store = LakeStore(tmp_path / "lake")
    client = PowerBIClient(
        lambda: Credentials(token=token, profile="admin-nlm", group="admin"),
        store=store,
        limiter=Limiter(QuotaTracker()),
        base_url=base_url,
    )
    return SyncEngine(lambda scope: client, store), store, client


def test_sixteen_workers_share_one_session_without_running_the_pool_dry(
    server, tmp_path, caplog
):
    service, base_url = server
    engine, store, client = engine_for(base_url, tmp_path)

    with caplog.at_level(logging.WARNING, logger="urllib3"):
        report = engine.run(
            SyncOptions(targets=("report-users",), workers=16, max_wait=None)
        )

    assert report.status == COMPLETED and report.counts[DONE] == 1 + 200
    assert service.count(r"^/admin/reports/[^/]+/users$") == 200
    assert len(store.parameter_sets("tenant-1", "admin.reports.users")) == 200
    assert "pool is full" not in caplog.text, caplog.text
    client.close()


def test_a_whole_sync_works_over_http(server, tmp_path):
    service, base_url = server
    engine, store, client = engine_for(base_url, tmp_path)

    report = engine.run(
        SyncOptions(
            targets=("default", "activity", "scan"),
            workers=4,
            days=2,
            scan_interval=0.05,
        )
    )

    assert report.status == COMPLETED and not report.failures
    assert service.count(r"getInfo", "POST") == 1  # 30 workspaces are one scan
    assert store.endpoints("tenant-1") == sorted(
        [
            "admin.activityevents",
            "admin.apps",
            "admin.capacities",
            "admin.dashboards",
            "admin.dataflows",
            "admin.datasets",
            "admin.groups",
            "admin.reports",
            "admin.scan.result",
        ]
    )
    assert len(store.event_days("tenant-1", "admin.activityevents")) == 2
    client.close()
