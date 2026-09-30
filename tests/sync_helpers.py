"""A fake service, clients, a lake and an engine that share one fake clock."""

from datetime import timedelta
from typing import Any, Callable, Dict, Optional

from core_helpers import Time, make_client, make_token
from fake_powerbi import FakePowerBI

from pbi_cli.core.registry import Scope
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.engine import Event, RunReport, SyncEngine
from pbi_cli.core.sync.plan import Plan, SyncOptions
from pbi_cli.core.sync.state import STATE_NAME

TENANT = "tenant-1"


class World:
    """Everything a sync needs, on one clock that only moves when a test moves it.

    :param tmp_path: where the lake goes
    :param sleep: waits for some seconds (default: advance the fake clock)
    :param fake: what the fake service is built with (see `FakePowerBI`)
    """

    def __init__(
        self,
        tmp_path,
        *,
        sleep: Optional[Callable[[float], None]] = None,
        **fake: Any,
    ):
        self.clock = Time()
        self.fake = FakePowerBI(clock=self.clock.now, **fake)
        self.store = LakeStore(tmp_path / "lake")
        token = make_token(tenant=TENANT, expires_in=timedelta(days=3650))
        self.admin = make_client(
            self.fake, clock=self.clock, store=self.store, token=token, group="admin"
        )[0]
        self.user = make_client(
            self.fake,
            clock=self.clock,
            store=self.store,
            token=token,
            group="user",
            profile="user-nlm",
        )[0]
        self.engine = SyncEngine(
            self.client_for,
            self.store,
            clock=self.clock.now,
            sleep=sleep or self.clock.sleep,
            monotonic=self.clock.time,
        )

    def client_for(self, scope: Scope):
        return self.admin if scope is Scope.ADMIN else self.user

    @staticmethod
    def options(*targets: str, **options: Any) -> SyncOptions:
        """Options for a test: one worker unless asked, for a predictable order."""
        options.setdefault("workers", 1)
        return SyncOptions(targets=targets, **options)

    def run(
        self,
        *targets: str,
        on_event: Optional[Callable[[Event], None]] = None,
        **options: Any,
    ) -> RunReport:
        return self.engine.run(self.options(*targets, **options), on_event=on_event)

    def plan(self, *targets: str, **options: Any) -> Plan:
        return self.engine.plan(self.options(*targets, **options))

    def state(self) -> Dict[str, Any]:
        return self.store.read_state(TENANT, STATE_NAME) or {}

    def stored(self, endpoint: str, params: Optional[Dict[str, str]] = None):
        """The newest stored answer of a request, or ``None``."""
        return self.store.latest(TENANT, endpoint, params or {})
