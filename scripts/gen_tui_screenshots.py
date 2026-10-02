"""Capture the screenshots of ``docs/tui.md`` from the real TUI.

It builds a made-up tenant in a fake Power BI service (the one the tests use), syncs it into
a lake in a temporary folder, drives the app with Textual's test pilot and saves what is on
the screen as SVG files in ``docs/images``. Nothing here talks to a real tenant, and every
name in the pictures is invented.

    uv run python scripts/gen_tui_screenshots.py

The pictures depend on the Textual version and its fonts, so they are not part of the tests:
run the script when the TUI changes, look at the result, and commit the files.
"""

# isort: off
# The helpers of the tests (the fake service) are imported from tests/, which has to be on
# the path before the imports: isort must not move them.
import asyncio
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import fake_powerbi
from cloudpathlib.cloudpath import implementation_registry
from cloudpathlib.local import LocalS3Client, local_s3_implementation
from core_helpers import NOW, make_client, make_token
from sync_helpers import World

from pbi_cli.core.publish import plan_publish, publish
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.store import LakeStore
from pbi_cli.core.sync.engine import SyncEngine
from pbi_cli.core.sync.targets import TARGETS
from pbi_cli.tui import Backend
from pbi_cli.tui.app import PBIApp
from pbi_cli.tui.backend import AccountInfo

# isort: on

OUT = ROOT / "docs" / "images"
SIZE = (124, 36)
TENANT = "0b6e7f5a-3c1d-4f7e-9a52-7d3c1e8b2f40"

WORKSPACES = [
    "Finance",
    "Sales EMEA",
    "Sales APAC",
    "Marketing",
    "People Analytics",
    "Operations",
    "Supply Chain",
    "Executive Dashboards",
    "Data Platform",
    "Customer Success",
    "Procurement",
    "IT Service Desk",
    "Ann Lee's workspace",
    "Raj Patel's workspace",
]
REPORTS = [
    "Monthly P&L",
    "Pipeline EMEA",
    "Pipeline APAC",
    "Campaign ROI",
    "Headcount",
    "Plant output",
    "Inventory turns",
    "Board pack",
    "Data quality",
]
DATASETS = [
    "Finance model",
    "Sales model EMEA",
    "Sales model APAC",
    "Marketing model",
    "People model",
    "Operations model",
]
FLAGS = ScanFlags(lineage=True, datasource_details=True, get_artifact_users=True)


PEOPLE = [
    ("Ann Lee", "ann.lee@contoso.com", "User"),
    ("Raj Patel", "raj.patel@contoso.com", "User"),
    ("Finance viewers", "finance-viewers@contoso.com", "Group"),
]


def patch_users() -> None:
    """The users of a report: people and a group that a company would have."""

    def users(self: Any, match: Any, query: Any, body: Any, origin: Any) -> Any:
        return 200, {
            "value": [
                {
                    "displayName": name,
                    "emailAddress": email,
                    "reportUserAccessRight": ("Owner", "Reshare", "Read")[n],
                    "principalType": kind,
                }
                for n, (name, email, kind) in enumerate(PEOPLE)
            ]
        }

    fake_powerbi.FakePowerBI._users = users  # type: ignore[method-assign]


def patch_scan_result() -> None:
    """Give the scan names of data sources and people that look like a company's."""
    original = fake_powerbi.FakePowerBI._scan_result
    databases = {
        "dsi-ds-0001": ("finance-sql.contoso.com", "LedgerDW"),
        "dsi-ds-0002": ("sales-sql.contoso.com", "SalesEMEA"),
        "dsi-ds-0003": ("sales-sql.contoso.com", "SalesAPAC"),
        "dsi-ds-0004": ("mkt-sql.contoso.com", "Campaigns"),
        "dsi-ds-0005": ("hr-sql.contoso.com", "PeopleDW"),
        "dsi-ds-0006": ("ops-sql.contoso.com", "PlantData"),
    }

    def scan_result(self: Any, match: Any, query: Any, body: Any, origin: Any) -> Any:
        found = original(self, match, query, body, origin)
        if isinstance(found, tuple) and found[0] == 200:
            for workspace in found[1]["workspaces"]:
                workspace["users"] = [
                    {
                        "displayName": name,
                        "emailAddress": email,
                        "groupUserAccessRight": ("Admin", "Member", "Viewer")[n],
                        "principalType": kind,
                    }
                    for n, (name, email, kind) in enumerate(PEOPLE)
                ]
                for report in workspace["reports"]:
                    if "users" in report:
                        report["users"] = [
                            {
                                "displayName": name,
                                "emailAddress": email,
                                "reportUserAccessRight": ("Owner", "Reshare", "Read")[
                                    n
                                ],
                                "principalType": kind,
                            }
                            for n, (name, email, kind) in enumerate(PEOPLE)
                        ]
            for instance in found[1]["datasourceInstances"]:
                server, database = databases.get(
                    instance["datasourceId"], ("sql", "db")
                )
                instance["connectionDetails"] = {"server": server, "database": database}
        return found

    fake_powerbi.FakePowerBI._scan_result = scan_result  # type: ignore[method-assign]


ADMIN = "admin-nlm"
SERVICE = "svc-finance"


def new_token(world: World) -> None:
    """Tokens with some time left: the header counts them down."""
    now = world.clock.now()
    admin = make_token(
        tenant=TENANT,
        expires_in=timedelta(minutes=47),
        now=now,
        oid="oid-ann",
        upn="ann.lee@contoso.com",
    )
    world.admin = make_client(
        world.fake, clock=world.clock, store=world.store, token=admin, profile=ADMIN
    )[0]
    service = make_token(
        tenant=TENANT,
        expires_in=timedelta(minutes=21),
        now=now,
        oid="oid-svc-finance",
        upn="svc-finance@contoso.com",
    )
    world.user = make_client(
        world.fake,
        clock=world.clock,
        store=world.store,
        token=service,
        group="user",
        profile=SERVICE,
    )[0]


def make_world(cache: Path) -> World:
    """A made-up tenant of 14 workspaces, with the names and the people of a company."""
    world = World(
        cache,
        workspaces=len(WORKSPACES),
        reports=len(REPORTS),
        datasets=len(DATASETS),
        event_days=5,
        events_per_day=40,
    )
    fake = world.fake
    for workspace, name in zip(fake.workspaces, WORKSPACES):
        workspace["name"] = name
    for workspace in fake.workspaces[-2:]:
        workspace["type"] = "PersonalGroup"
    for report, name in zip(fake.reports, REPORTS):
        report["name"] = name
    fake.reports[6]["datasetId"] = "ds-0001"  # Inventory turns
    fake.reports[7]["datasetId"] = "ds-0001"  # Board pack
    for dataset, name in zip(fake.datasets, DATASETS):
        dataset["name"] = name
    fake.dashboards[0]["displayName"] = "CFO overview"
    fake.dataflows[0]["name"] = "Ledger staging"
    for app, name in zip(fake.apps, ("Finance hub", "Sales hub", "People insights")):
        app["name"] = name
    fake.capacities[0]["displayName"] = "Contoso Premium P1"
    fake.capacities[0]["region"] = "West Europe"
    for events in fake._events.values():
        for number, event in enumerate(events):
            event["Activity"] = (
                "ViewReport",
                "ExportReport",
                "ViewDashboard",
                "CreateDataset",
            )[number % 4]
            event["ReportName"] = REPORTS[number % len(REPORTS)]
            event["WorkSpaceName"] = WORKSPACES[number % 9]
            event["UserId"] = ("ann.lee", "raj.patel", "mia.chen", "tom.berg")[
                number % 4
            ] + "@contoso.com"
    for number, report in enumerate(fake.reports):
        report["createdBy"] = "ann.lee@contoso.com"
        report["modifiedDateTime"] = f"2026-09-{10 + number:02d}T09:{number:02d}:00Z"
    for dataset in fake.datasets:
        dataset["configuredBy"] = "mia.chen@contoso.com"

    new_token(world)
    return world


def build_world(tmp: Path) -> World:
    """The tenant synced by an administrator, at three different times so that the dots
    differ. The lake lives where a user would keep it: ~/PowerBI/cache/lake."""
    world = make_world(tmp / "home" / "PowerBI" / "cache")
    # scans of different ages: the dots of the tree go from green to red
    clock = world.clock
    clock.t = NOW.timestamp() - 9 * 86400
    world.run(
        "scan",
        workspace_ids=tuple(f"ws-{n:04d}" for n in range(12, 15)),
        scan_flags=FLAGS,
    )
    clock.t = NOW.timestamp() - 3 * 86400
    world.run(
        "scan",
        workspace_ids=tuple(f"ws-{n:04d}" for n in range(7, 12)),
        scan_flags=FLAGS,
    )
    clock.t = NOW.timestamp()
    world.run(
        "scan",
        workspace_ids=tuple(f"ws-{n:04d}" for n in range(1, 7)),
        scan_flags=FLAGS,
    )
    world.run("default", "report-users", "activity", days=5)
    return world


def build_user_world(tmp: Path) -> World:
    """The same tenant as a service account sees it: no administrator, so the plain sync is
    the workspaces of the account and what is in them, in a lake of its own."""
    world = make_world(tmp / "home" / "PowerBI" / "finance-bot")
    world.only_user()
    world.run()
    return world


def backend_of(world: World, **replace: Any) -> Backend:
    settings: dict = dict(
        store=world.store,
        client_for=world.client_for,
        sign_in=lambda token, profile, group: None,
        active_profile=lambda group: ADMIN if group == "admin" else SERVICE,
        make_engine=lambda: world.engine,
        clock=world.clock.now,
    )
    if "client_for" in replace and "make_engine" not in replace:
        # an engine that signs in the way the backend does
        replace["make_engine"] = lambda: SyncEngine(
            replace["client_for"],
            world.store,
            clock=world.clock.now,
            sleep=world.clock.sleep,
            monotonic=world.clock.time,
        )
    settings.update(replace)
    return Backend(**settings)


async def settle(app: PBIApp, pilot: Any) -> None:
    for _ in range(6):
        for worker in list(app.workers):
            try:
                await worker.wait()
            except Exception:  # a replaced worker: not an error here
                pass
        await pilot.pause(0.05)


async def until(condition: Callable[[], Any], seconds: float = 10.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise RuntimeError("gave up waiting")


async def shoot(
    world: World,
    name: str,
    scenario: Callable[[PBIApp, Any], Awaitable[None]],
    backend: Optional[Backend] = None,
) -> None:
    app = PBIApp(backend or backend_of(world))
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await scenario(app, pilot)
        await settle(app, pilot)
        svg = app.export_screenshot(title="pbi tui")
    clean = "\n".join(line.rstrip() for line in svg.splitlines()) + "\n"
    (OUT / name).write_text(clean, encoding="utf-8")
    print(f"wrote docs/images/{name}")


def explorer(app: PBIApp) -> Any:
    return app.get_screen("explorer")


async def pick(app: PBIApp, pilot: Any, workspace: str, row: str = "") -> None:
    from pbi_cli.tui.explorer import NodeRef

    screen = explorer(app)
    screen._select(screen._tree_nodes[NodeRef("workspace", workspace)])
    await settle(app, pilot)
    if row:
        table = screen.query_one("#table")
        table.focus()
        table.move_cursor(row=table.get_row_index(row))
        await settle(app, pilot)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    patch_scan_result()
    patch_users()
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        Path.home = classmethod(lambda cls: home)  # type: ignore[method-assign]
        world = build_world(Path(tmp))

        async def workspace(app: PBIApp, pilot: Any) -> None:
            await pick(app, pilot, "ws-0001")

        async def lineage(app: PBIApp, pilot: Any) -> None:
            await pick(app, pilot, "ws-0001", "dataset:ds-0001")
            await pilot.press("3")

        async def users(app: PBIApp, pilot: Any) -> None:
            await pick(app, pilot, "ws-0001", "report:rep-0001")
            await pilot.press("2")

        async def sign_in(app: PBIApp, pilot: Any) -> None:
            await pick(app, pilot, "ws-0001")
            app.action_sign_in(
                reason=(
                    f"The token for profile '{ADMIN}' expired at 2026-09-30 12:02 UTC. "
                    "The sync keeps what it did and goes on when you have signed in."
                )
            )

        def stored_accounts() -> list:
            """The profiles of an administrator who also keeps two service accounts."""
            now = world.clock.now()
            return [
                AccountInfo(
                    "admin",
                    ADMIN,
                    True,
                    "ann.lee@contoso.com",
                    TENANT,
                    now + timedelta(minutes=47),
                ),
                AccountInfo(
                    "user",
                    SERVICE,
                    True,
                    "svc-finance@contoso.com",
                    TENANT,
                    now + timedelta(minutes=21),
                ),
                AccountInfo(
                    "user",
                    "svc-sales",
                    False,
                    "svc-sales@contoso.com",
                    TENANT,
                    now - timedelta(hours=3),
                ),
            ]

        async def accounts(app: PBIApp, pilot: Any) -> None:
            await pick(app, pilot, "ws-0001")
            await pilot.press("p")

        for name, scenario in (
            ("tui-explorer.svg", workspace),
            ("tui-lineage.svg", lineage),
            ("tui-users.svg", users),
            ("tui-signin.svg", sign_in),
        ):
            asyncio.run(shoot(world, name, scenario))
        asyncio.run(
            shoot(
                world,
                "tui-accounts.svg",
                accounts,
                backend_of(world, accounts=stored_accounts),
            )
        )

        # a day later the lists are stale, so the plan has something to fetch
        world.clock.advance(hours=26)
        new_token(world)

        async def plan(app: PBIApp, pilot: Any) -> None:
            await pilot.press("s")
            screen = app.get_screen("sync")
            await until(lambda: screen.plans > 0)
            targets = screen.query_one("#targets")
            targets.select("scan")
            targets.select("activity")
            targets.highlighted = [t.name for t in TARGETS].index("scan")
            await until(lambda: screen.plans > 1)

        async def run(app: PBIApp, pilot: Any) -> None:
            await plan(app, pilot)
            screen = app.get_screen("sync")
            await pilot.click("#run")
            await until(lambda: app.run_state is not None and not app.run_state.running)
            await until(lambda: screen._finished_state is app.run_state)

        asyncio.run(shoot(world, "tui-sync.svg", plan))
        asyncio.run(shoot(world, "tui-run.svg", run))

        # someone with only a service account: no administrator targets, a plain sync of
        # what the account can see
        service = build_user_world(Path(tmp))
        service.clock.advance(hours=26)
        new_token(service)

        async def user_only(app: PBIApp, pilot: Any) -> None:
            await pilot.press("s")
            screen = app.get_screen("sync")
            await until(lambda: screen.plans > 0)
            targets = screen.query_one("#targets")
            # the note under the list says why the highlighted target is off
            targets.highlighted = [t.name for t in TARGETS].index("scan")

        asyncio.run(
            shoot(
                service,
                "tui-useronly.svg",
                user_only,
                backend_of(service, client_for=service.engine._client_for),
            )
        )

        # a lake that somebody published to a bucket, looked at by a person with no account
        implementation_registry["s3"] = local_s3_implementation  # a bucket on this disk
        LocalS3Client.reset_default_storage_dir()
        shared = LakeStore("s3://contoso-bi/pbi-lake")
        publish(
            plan_publish(
                world.store,
                shared,
                exclude=["activity"],
                publisher="ann.lee@bi-laptop",
            ),
            now=NOW,
        )

        def refuse(scope: Any) -> Any:
            raise RuntimeError("a lake that is only looked at asks for no account")

        viewer = Backend(
            store=LakeStore("s3://contoso-bi/pbi-lake"),
            client_for=refuse,
            sign_in=lambda token, profile, group: None,
            clock=world.clock.now,
        )

        async def view_only(app: PBIApp, pilot: Any) -> None:
            await pick(app, pilot, "ws-0001", "report:rep-0001")
            await pilot.press("2")

        asyncio.run(shoot(world, "tui-viewonly.svg", view_only, viewer))


if __name__ == "__main__":
    main()
