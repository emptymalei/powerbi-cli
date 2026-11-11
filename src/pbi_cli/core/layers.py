"""A shared lake with the work lake on top: both looked at as one, only the work lake written.

Someone who opens a lake that a colleague published (`pbi_cli.core.publish`) and has an
account of their own, a service account for example, can look further into it without
copying anything:

```python
lake = LayeredStore(work=LakeStore("~/pbi-cache/lake"), shared=LakeStore(shared, readonly=True))
catalog = Catalog(lake, tenant)   # what both lakes hold, the newest answer of each request
```

- **Reads** take what both lakes hold. Where both hold an answer to the same request, the
  one fetched last is taken; a day of activity events is taken whole from one lake (the
  one that holds more of it). Answers that depend on who asked (a user's own workspaces,
  for example) are kept per account, so another account's answers are kept beside the
  publisher's, never over them.
- **Writes** go to the work lake, and only there. The shared lake is never written: what
  is fetched, synced or worked out is the work lake's.
- **Where an answer came from** is known (`LayeredStore.layer_of`): the explorer says
  whether something is from the shared lake or from the work lake.
- **Tables worked out from both** (who has access to what) are kept in the work lake,
  beside its own (``<work>/_layers/<shared lake>/tenant=<tenant>/_tables``), and are taken
  only while neither lake has changed since they were built.

Publishing reads the work lake alone: a lake looked at this way never publishes what the
shared lake holds.
"""

import hashlib
import json
from datetime import date
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from loguru import logger

from pbi_cli.core.fsutil import inside_place, location_of
from pbi_cli.core.store import (
    EventDay,
    LakeStore,
    ParameterSet,
    PublishInfo,
    Snapshot,
    safe_name,
)

#: What `LayeredStore.layer_of` says.
SHARED, WORK = "shared", "work"

#: The folder of the work lake that keeps the tables worked out from it and a shared lake.
LAYERS = "_layers"
#: The file beside those tables that says which state of both lakes they were built from.
_STAMP = "layers.json"
#: The file of the access table that is written last (`pbi_cli.core.accessreport`): until
#: it is there, the table is not taken.
_TABLE_MANIFEST = "access.json"


def _newer(first: Optional[Snapshot], second: Optional[Snapshot]) -> Optional[Snapshot]:
    """The snapshot fetched last of two (the first when they tie)."""
    if first is None or second is None:
        return first or second
    return second if second.fetched_at > first.fetched_at else first


def _fuller(
    first: Optional[EventDay], second: Optional[EventDay]
) -> Optional[EventDay]:
    """The day of events that holds more: a complete day, else the one read further, else
    the one with more events (the first when they tie)."""
    if first is None or second is None:
        return first or second

    def comparable(day: EventDay) -> Tuple[bool, str, int]:
        until = day.covered_until
        return (day.sealed, until.isoformat() if until else "", day.rows)

    return second if comparable(second) > comparable(first) else first


class LayeredStore:
    """A shared lake with the work lake on top (see the module).

    It reads like a `LakeStore` and writes like the work lake. What is not a read of
    snapshots, events or state (pruning, publishing, removing data) is not offered: do that
    with the work lake itself.

    :param work: the work lake (the lake of the cache folder): everything is written there
    :param shared: the lake looked at underneath; it is never written
    """

    def __init__(self, work: LakeStore, shared: LakeStore):
        self.work = work
        self.shared = shared
        self._layers = (work, shared)
        #: what the tables being built depend on, per tenant (`begin_tables`)
        self._building: Dict[str, Optional[Dict[str, str]]] = {}

    # -- what the lake is --------------------------------------------------------------

    @property
    def root(self) -> Any:
        """The shared lake: the one that was opened (the work lake is `work`)."""
        return self.shared.root

    @property
    def readonly(self) -> bool:
        return self.work.readonly

    @property
    def reason(self) -> str:
        return self.work.reason

    @property
    def writable(self) -> bool:
        """Whether something can be fetched: the work lake takes it."""
        return self.work.writable

    def why_read_only(self) -> str:
        return self.work.why_read_only()

    def published(self) -> Optional[PublishInfo]:
        """Who published the shared lake, and when (``None`` when it was not published)."""
        return self.shared.published()

    def refresh_marker(self) -> Optional[PublishInfo]:
        return self.shared.refresh_marker()

    def layer_of(self, place: Any) -> str:
        """Which lake a snapshot or a folder is in: `SHARED` or `WORK`.

        :param place: a `Snapshot`, an `EventDay` or a folder
        """
        directory = getattr(place, "directory", place)
        try:
            if inside_place(directory, self.work.root):
                return WORK
        except (
            Exception
        ):  # noqa: BLE001 - a path of another kind is not the work lake's
            pass
        return SHARED

    # -- snapshots ---------------------------------------------------------------------

    def tenants(self) -> List[str]:
        return sorted({t for store in self._layers for t in store.tenants()})

    def endpoint_folders(self, tenant: str) -> List[str]:
        return sorted(
            {f for store in self._layers for f in store.endpoint_folders(tenant)}
        )

    def endpoints(self, tenant: str) -> List[str]:
        return sorted({e for store in self._layers for e in store.endpoints(tenant)})

    def parameter_sets(self, tenant: str, endpoint_id: str) -> List[ParameterSet]:
        """Every set of parameters either lake holds, each with the newest snapshot of
        both."""
        found: Dict[str, ParameterSet] = {}
        for store in self._layers:
            for found_set in store.parameter_sets(tenant, endpoint_id):
                held = found.get(found_set.hash)
                if (
                    held is None
                    or _newer(held.latest, found_set.latest) is not held.latest
                ):
                    found[found_set.hash] = found_set
        return [found[key] for key in sorted(found)]

    def latest(
        self, tenant: str, endpoint_id: str, params: Mapping[str, str]
    ) -> Optional[Snapshot]:
        return _newer(
            self.work.latest(tenant, endpoint_id, params),
            self.shared.latest(tenant, endpoint_id, params),
        )

    def versions(
        self, tenant: str, endpoint_id: str, params: Mapping[str, str]
    ) -> List[Snapshot]:
        """The snapshots of a request in both lakes, newest first."""
        both = [
            snapshot
            for store in self._layers
            for snapshot in store.versions(tenant, endpoint_id, params)
        ]
        return sorted(both, key=lambda s: s.fetched_at, reverse=True)

    # -- events ------------------------------------------------------------------------

    def _day(self, tenant: str, endpoint_id: str, day: date) -> Optional[EventDay]:
        return _fuller(
            self.work.event_day(tenant, endpoint_id, day),
            self.shared.event_day(tenant, endpoint_id, day),
        )

    def event_day(self, tenant: str, endpoint_id: str, day: date) -> Optional[EventDay]:
        return self._day(tenant, endpoint_id, day)

    def event_days(self, tenant: str, endpoint_id: str) -> List[EventDay]:
        """The days of events of both lakes, newest first: each from the lake that holds
        more of it."""
        days: Dict[date, EventDay] = {}
        for store in self._layers:
            for found in store.event_days(tenant, endpoint_id):
                days[found.day] = _fuller(days.get(found.day), found)  # type: ignore[assignment]
        return [days[key] for key in sorted(days, reverse=True)]

    def read_events(
        self, tenant: str, endpoint_id: str, day: date
    ) -> Iterator[Dict[str, Any]]:
        chosen = self._day(tenant, endpoint_id, day)
        if chosen is None:
            return iter(())
        store = self.work if self.layer_of(chosen) == WORK else self.shared
        return store.read_events(tenant, endpoint_id, day)

    def event_ids(
        self, tenant: str, endpoint_id: str, day: date, id_field: str = "Id"
    ) -> set:
        return {
            event[id_field]
            for event in self.read_events(tenant, endpoint_id, day)
            if id_field in event
        }

    # -- state -------------------------------------------------------------------------

    def read_state(self, tenant: str, name: str) -> Optional[Dict[str, Any]]:
        """The work lake's state document, else the shared lake's."""
        found = self.work.read_state(tenant, name)
        return found if found is not None else self.shared.read_state(tenant, name)

    # -- writes: the work lake's ------------------------------------------------------

    def write_snapshot(self, *args: Any, **kwargs: Any) -> Any:
        return self.work.write_snapshot(*args, **kwargs)

    def write_state(self, tenant: str, name: str, data: Mapping[str, Any]) -> None:
        self.work.write_state(tenant, name, data)

    def append_events(self, *args: Any, **kwargs: Any) -> Any:
        return self.work.append_events(*args, **kwargs)

    def seal_day(self, *args: Any, **kwargs: Any) -> Any:
        return self.work.seal_day(*args, **kwargs)

    # -- tables worked out from both lakes -------------------------------------------

    def _tables_dir(self, tenant: str) -> Any:
        key = hashlib.sha256(location_of(self.shared.root).encode("utf-8")).hexdigest()
        return (
            self.work.root
            / LAYERS
            / key[:16]
            / f"tenant={safe_name(tenant)}"
            / "_tables"
        )

    def _stamp(self, tenant: str) -> Optional[Dict[str, str]]:
        """What the tables of both lakes depend on: the shared lake as it was published (or
        its own table, when it was not published), and the work lake's own table.

        ``None`` when the shared lake gives no way to tell that it changed: tables are then
        always worked out again.
        """
        info = self.shared.refresh_marker()
        if info is not None:
            shared = f"published {info.published_at.isoformat()} by {info.published_by}"
            if not info.complete:
                return None
        else:
            shared = _built_at(self.shared, tenant) or ""
            if not shared:
                return None
        return {
            "shared": shared,
            "work": _built_at(self.work, tenant) or "none",
        }

    def tables_stale(self, tenant: str) -> bool:
        """Whether the tables of both lakes are older than what either lake holds."""
        if self.work.tables_stale(tenant):
            return True
        stamp = self._stamp(tenant)
        if stamp is None:
            return True
        try:
            kept = json.loads((self._tables_dir(tenant) / _STAMP).read_text("utf-8"))
        except FileNotFoundError:
            return True
        except Exception as error:  # noqa: BLE001 - unreadable: built again
            logger.warning(f"Ignoring the stamp of the tables of both lakes: {error}")
            return True
        return kept != stamp

    def begin_tables(self, tenant: str) -> None:
        """Start building the tables of both lakes.

        The work lake's own table is brought up to date first (its build time is part of
        what these tables depend on, so a snapshot written to the work lake later makes them
        stale), then what is kept of these tables is set aside.
        """
        if self.work.tables_stale(tenant):
            from pbi_cli.core.accessreport import refresh_access_table
            from pbi_cli.core.catalog import Catalog

            refresh_access_table(Catalog(self.work, tenant))
        directory = self._tables_dir(tenant)
        for name in (_TABLE_MANIFEST, _STAMP):
            try:
                (directory / name).unlink()
            except FileNotFoundError:
                pass
        self._building[tenant] = self._stamp(tenant)

    def write_table(self, tenant: str, name: str, payload: bytes) -> None:
        directory = self._tables_dir(tenant)
        self.work.write_file(directory / name, payload)
        stamp = self._building.get(tenant)
        if name == _TABLE_MANIFEST and stamp is not None:
            self.work.write_file(
                directory / _STAMP, json.dumps(stamp, indent=2).encode("utf-8")
            )

    def read_table(self, tenant: str, name: str) -> Optional[bytes]:
        try:
            return (self._tables_dir(tenant) / name).read_bytes()
        except FileNotFoundError:
            return None
        except Exception as error:  # noqa: BLE001 - a cloud store raises its own errors
            logger.warning(f"Ignoring an unreadable table of both lakes: {error}")
            return None


def _built_at(store: LakeStore, tenant: str) -> Optional[str]:
    """When a lake's own access table was built, while it is up to date (``None`` when it
    has none, or it is stale)."""
    if store.tables_stale(tenant):
        return None
    raw = store.read_table(tenant, _TABLE_MANIFEST)
    if raw is None:
        return None
    try:
        return str(json.loads(raw)["built_at"])
    except (ValueError, KeyError, TypeError):
        return None


def work_store(store: Any) -> Any:
    """The lake that is written: the work lake of a `LayeredStore`, else the lake itself."""
    return store.work if isinstance(store, LayeredStore) else store
