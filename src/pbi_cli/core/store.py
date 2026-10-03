"""The data lake: versioned snapshots and event logs on a local folder or in S3.

Everything the client fetches is kept as it came from the API, in folders that tools
such as Athena, DuckDB or Spark read as partitions:

```text
<root>/tenant=<tenant>/endpoint=<endpoint>/params=<hash>/dt=<day>/v=<time>/
    data.json        the response (all pages merged)
    manifest.json    what was asked, when, by whom, and a checksum

<root>/tenant=<tenant>/endpoint=<endpoint>/dt=<day>/
    part-0000.jsonl  events, one JSON object per line, never the same `Id` twice
    manifest.json    parts, row count, resume cursor, whether the day is complete
```

Progress that belongs to a tenant but is not data, such as the state of a sync, is kept
in `<root>/tenant=<tenant>/_state/<name>.json`.

A lake that was published (`pbi_cli.core.publish`) has a `<root>/publish.json` that says by
whom and when. A lake with that file is protected: nothing but the publisher writes to it.
A store can also be opened read-only, and then every write raises `ReadOnlyLake`.

The manifest is written last: a snapshot without one is an interrupted write and is
ignored. Local files are created readable by the owner only, because the lake holds
names, e-mail addresses, IP addresses and query definitions. Tokens are never stored.
"""

import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from cloudpathlib import AnyPath, CloudPath
from loguru import logger

from pbi_cli.core.fsutil import replace_file
from pbi_cli.errors import ReadOnlyLake

#: Version of the manifest layout.
SCHEMA = 1
MANIFEST = "manifest.json"
DATA = "data.json"

#: The file at the root of a lake that marks it as published (and so as protected).
PUBLISH_FILE = "publish.json"

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_FORBIDDEN_REQUEST_KEYS = {"headers", "authorization", "token", "cookies"}


def safe_name(value: str) -> str:
    """Make a value usable as the value of a folder such as ``tenant=<value>``."""
    return _UNSAFE.sub("_", value) or "_"


def params_hash(params: Mapping[str, str]) -> str:
    """A short, stable identifier of a set of canonical request parameters."""
    canonical = json.dumps(
        dict(params), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _utc(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    )


def cli_version() -> str:
    try:
        return _package_version("pbi_cli")
    except PackageNotFoundError:
        return "unknown"


def _dump(data: Any, indent: Optional[int] = None) -> bytes:
    separators = None if indent else (",", ":")
    return json.dumps(
        data, indent=indent, separators=separators, ensure_ascii=False
    ).encode("utf-8")


@dataclass(frozen=True)
class Snapshot:
    """One stored response.

    :param manifest: the contents of ``manifest.json``
    :param directory: the folder of the snapshot (a path on disk or in the cloud)
    """

    manifest: Dict[str, Any]
    directory: Any

    @property
    def fetched_at(self) -> datetime:
        """When the response was received (aware, UTC)."""
        return _utc(datetime.fromisoformat(self.manifest["fetched_at"]))

    @property
    def version(self) -> str:
        """The version folder name, for example ``20260930T204900123456Z``."""
        return self.directory.name[len("v=") :]

    def age(self, now: Optional[datetime] = None) -> timedelta:
        """How old the snapshot is."""
        return _utc(now or datetime.now(timezone.utc)) - self.fetched_at

    def load(self) -> Any:
        """Read the stored response."""
        path = self.directory / self.manifest.get("data_file", DATA)
        return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class ParameterSet:
    """One set of request parameters an endpoint was fetched with.

    :param hash: the ``params=<hash>`` folder name
    :param params: the canonical parameters
    :param latest: the newest snapshot of this set
    """

    hash: str
    params: Dict[str, str]
    latest: Snapshot


@dataclass(frozen=True)
class EventDay:
    """The events of one UTC day.

    :param manifest: the contents of the day's ``manifest.json``
    :param directory: the folder of the day
    """

    manifest: Dict[str, Any]
    directory: Any

    @property
    def day(self) -> date:
        return date.fromisoformat(self.manifest["day"])

    @property
    def rows(self) -> int:
        return int(self.manifest.get("rows", 0))

    @property
    def sealed(self) -> bool:
        """Whether the day is complete: nothing more will be appended."""
        return bool(self.manifest.get("sealed"))

    @property
    def cursor(self) -> Optional[str]:
        """Where to resume fetching (for example a continuation URI), if anything."""
        return self.manifest.get("cursor")

    @property
    def updated_at(self) -> Optional[datetime]:
        """When events were last added to the day, or it was sealed (aware, UTC)."""
        stamp = self.manifest.get("updated_at")
        return _utc(datetime.fromisoformat(stamp)) if stamp else None


@dataclass(frozen=True)
class PublishInfo:
    """What a published lake says about itself (its ``publish.json``).

    :param published_at: when the last complete publish finished (aware, UTC)
    :param published_by: who published it
    :param tenants: the tenants it holds
    :param excluded: the categories that were left out (``activity``, ``users``, ...)
    :param history: whether every stored version was copied, not only the newest
    :param files: how many files the publish copied
    :param size: how many bytes they hold
    :param cli_version: the version of pbi-cli that published it
    :param complete: ``False`` while the first publish is under way, or when it stopped half
        way: the lake is protected already, and publishing again finishes it
    """

    published_at: datetime
    published_by: str
    tenants: List[str] = field(default_factory=list)
    excluded: List[str] = field(default_factory=list)
    history: bool = False
    files: int = 0
    size: int = 0
    cli_version: str = "unknown"
    complete: bool = True

    def to_json(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "protected": True,
            "published_at": _utc(self.published_at).isoformat(),
            "published_by": self.published_by,
            "tenants": list(self.tenants),
            "excluded": list(self.excluded),
            "history": self.history,
            "files": self.files,
            "bytes": self.size,
            "cli_version": self.cli_version,
            "complete": self.complete,
        }

    @classmethod
    def from_json(cls, data: Any) -> "PublishInfo":
        """Read a ``publish.json``; a damaged one still protects the lake."""
        data = data if isinstance(data, dict) else {}
        try:
            when = _utc(datetime.fromisoformat(str(data["published_at"])))
        except (KeyError, ValueError):
            when = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return cls(
            published_at=when,
            published_by=str(data.get("published_by") or "someone"),
            tenants=[str(t) for t in data.get("tenants") or []],
            excluded=[str(c) for c in data.get("excluded") or []],
            history=bool(data.get("history")),
            files=int(data.get("files") or 0),
            size=int(data.get("bytes") or 0),
            cli_version=str(data.get("cli_version") or "unknown"),
            complete=data.get("complete", True) is not False,
        )


class StoreError(Exception):
    """The lake cannot do what was asked (for example appending to a sealed day)."""


class LakeStore:
    """Reads and writes the data lake.

    :param root: folder of the lake: a local path or a cloud URL such as
        ``s3://bucket/lake``. It is created on the first write.
    :param readonly: refuse every write (`ReadOnlyLake`), for a lake that is only looked at
    :param reason: why it is read-only, for the message
    :param publishing: the store of `pbi_cli.core.publish` writes to a published lake; it
        is the only one that may
    """

    def __init__(
        self,
        root: Union[str, os.PathLike, CloudPath],
        *,
        readonly: bool = False,
        reason: str = "",
        publishing: bool = False,
    ):
        path = AnyPath(root)
        if not isinstance(path, CloudPath):
            path = path.expanduser()  # "~/lake" means the home folder, not a folder "~"
        self.root = path
        self.readonly = readonly
        self.reason = reason
        self._publishing = publishing
        self._event_lock = threading.Lock()
        self._marker_lock = threading.Lock()
        self._marker: Optional[PublishInfo] = None
        self._marker_read = False

    # -- published lakes, and lakes that are only read -----------------------------

    def published(self) -> Optional[PublishInfo]:
        """What ``publish.json`` says, or ``None`` when the lake is not a published one.

        The file is looked for once per store (a published lake is a snapshot); call
        `refresh_marker` to look again.
        """
        with self._marker_lock:
            if not self._marker_read:
                self._marker = self._read_marker()
                self._marker_read = True
            return self._marker

    def refresh_marker(self) -> Optional[PublishInfo]:
        """Look for ``publish.json`` again."""
        with self._marker_lock:
            self._marker_read = False
        return self.published()

    def _read_marker(self) -> Optional[PublishInfo]:
        path = self.root / PUBLISH_FILE
        try:
            if not path.exists():
                return None
            return PublishInfo.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # something is there that cannot be read: it still protects the lake
            return PublishInfo.from_json(None)

    @property
    def writable(self) -> bool:
        """Whether a write would be accepted."""
        try:
            self._check_writable()
        except ReadOnlyLake:
            return False
        return True

    def why_read_only(self) -> str:
        """Why writes are refused, or an empty text when they are accepted."""
        try:
            self._check_writable()
        except ReadOnlyLake as error:
            return str(error)
        return ""

    def _check_writable(self) -> None:
        if self._publishing:
            return
        if self.readonly:
            raise ReadOnlyLake(self.reason or f"The lake {self.root} is read-only.")
        info = self.published()
        if info is not None:
            raise ReadOnlyLake(
                f"The lake {self.root} was published by {info.published_by} on "
                f"{info.published_at:%Y-%m-%d %H:%M} UTC, and a published lake is read-only. "
                "Sync into your own lake and publish it again."
            )

    def publishable(
        self, tenant: str, history: bool = False
    ) -> Iterator[Tuple[str, Any, bool]]:
        """The folders of a tenant to publish, each to be copied whole.

        A request gives its newest complete version (every complete version with
        ``history``), an event log gives every day. A version never changes; a day of events
        does until it is complete.

        :param tenant: the tenant folder value
        :param history: every version, not only the newest of each request
        :return: ``(endpoint folder value, folder, mutable)`` for each folder
        """
        for endpoint_dir in self._children(self._tenant_dir(tenant), "endpoint="):
            value = self._value(endpoint_dir, "endpoint=")
            for params_dir in self._children(endpoint_dir, "params="):
                for version in self._version_dirs(params_dir):
                    if not (version / MANIFEST).exists():
                        continue  # an interrupted write
                    yield value, version, False
                    if not history:
                        break
            for day_dir in self._children(endpoint_dir, "dt="):  # event logs
                if (day_dir / MANIFEST).exists():
                    yield value, day_dir, True

    def write_file(self, path: Any, payload: bytes) -> None:
        """Write a file of the lake (``path`` below the root) the way every file is written:
        atomically, readable by the owner only. Copying a lake (`pbi_cli.core.publish`) uses
        it."""
        self._write_bytes(path, payload)

    def write_marker(self, info: PublishInfo) -> None:
        """Mark the lake as published (only the store of the publisher may)."""
        self._write_bytes(self.root / PUBLISH_FILE, _dump(info.to_json(), indent=2))
        self.refresh_marker()

    # -- paths ---------------------------------------------------------------------

    def _tenant_dir(self, tenant: str) -> Any:
        return self.root / f"tenant={safe_name(tenant)}"

    def _endpoint_dir(self, tenant: str, endpoint_id: str) -> Any:
        return self._tenant_dir(tenant) / f"endpoint={safe_name(endpoint_id)}"

    def _params_dir(
        self, tenant: str, endpoint_id: str, params: Mapping[str, str]
    ) -> Any:
        return self._endpoint_dir(tenant, endpoint_id) / f"params={params_hash(params)}"

    @staticmethod
    def _children(path: Any, prefix: str) -> List[Any]:
        """Sub-folders of ``path`` whose name starts with ``prefix``, in name order."""
        if not path.exists():
            return []
        return sorted(
            (p for p in path.iterdir() if p.name.startswith(prefix) and p.is_dir()),
            key=lambda p: p.name,
        )

    # -- writing -------------------------------------------------------------------

    def _write_bytes(self, path: Any, payload: bytes) -> None:
        """Write a file atomically (a readable file is never half written)."""
        self._check_writable()
        if isinstance(path, CloudPath):
            path.write_bytes(payload)
            return
        self._ensure_root()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        # O_BINARY: on Windows a file made with os.open is a text file unless it is asked
        # not to be, and every line break would be written as CRLF
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        replace_file(tmp, path)

    def _ensure_root(self) -> None:
        """Create the lake folder, readable by the owner only."""
        if isinstance(self.root, CloudPath) or self.root.exists():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:  # not supported on every platform or file system
            pass

    @staticmethod
    def _check_request(request: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        request = dict(request or {})
        leaked = _FORBIDDEN_REQUEST_KEYS & {key.lower() for key in request}
        if leaked:
            raise ValueError(f"the request record must not hold {sorted(leaked)}")
        return request

    def write_snapshot(
        self,
        tenant: str,
        endpoint_id: str,
        params: Mapping[str, str],
        data: Any,
        *,
        request: Optional[Mapping[str, Any]] = None,
        profile: Optional[str] = None,
        pages: int = 1,
        status: int = 200,
        rows: Optional[int] = None,
        extra: Optional[Mapping[str, Any]] = None,
        fetched_at: Optional[datetime] = None,
        kind: str = "snapshot",
    ) -> Snapshot:
        """Store a response as a new version.

        :param tenant: tenant the data belongs to
        :param endpoint_id: registry id of the endpoint
        :param params: canonical request parameters (they identify the request)
        :param data: the response, JSON serializable
        :param request: what was asked (method, path, params, body hash): never headers
        :param profile: name of the profile used (not the token)
        :param pages: how many API pages the response was merged from
        :param status: HTTP status of the response
        :param rows: number of rows in the response, when it is a list
        :param extra: more fields for the manifest
        :param fetched_at: when the response was received (default: now)
        :param kind: ``snapshot``, or ``job`` for the result of an asynchronous job
        :raises ValueError: if ``request`` holds headers or a token
        """
        request = self._check_request(request)
        fetched_at = _utc(fetched_at or datetime.now(timezone.utc))
        payload = _dump(data)

        base = self._params_dir(tenant, endpoint_id, params)
        while True:
            directory = (
                base / f"dt={fetched_at:%Y-%m-%d}" / f"v={fetched_at:%Y%m%dT%H%M%S%fZ}"
            )
            if not (directory / MANIFEST).exists():
                break
            fetched_at += timedelta(microseconds=1)

        manifest: Dict[str, Any] = {
            "schema": SCHEMA,
            "kind": kind,
            "endpoint": endpoint_id,
            "tenant": tenant,
            "profile": profile,
            "fetched_at": fetched_at.isoformat(),
            "request": request,
            "params": dict(params),
            "params_hash": params_hash(params),
            "status": status,
            "pages": pages,
            "rows": rows,
            "data_file": DATA,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "cli_version": cli_version(),
        }
        manifest.update(extra or {})

        self._write_bytes(directory / DATA, payload)
        self._write_bytes(directory / MANIFEST, _dump(manifest, indent=2))
        logger.debug(f"Stored {endpoint_id} in {directory}")
        return Snapshot(manifest, directory)

    # -- reading snapshots ---------------------------------------------------------

    @staticmethod
    def _read_snapshot(directory: Any) -> Optional[Snapshot]:
        manifest_path = directory / MANIFEST
        if not manifest_path.exists():
            return None  # an interrupted write
        try:
            return Snapshot(
                json.loads(manifest_path.read_text(encoding="utf-8")), directory
            )
        except (OSError, ValueError) as error:
            logger.warning(f"Ignoring unreadable manifest {manifest_path}: {error}")
            return None

    def _version_dirs(self, params_dir: Any) -> Iterator[Any]:
        """Version folders, newest first."""
        for day_dir in reversed(self._children(params_dir, "dt=")):
            yield from reversed(self._children(day_dir, "v="))

    def versions(
        self, tenant: str, endpoint_id: str, params: Mapping[str, str]
    ) -> List[Snapshot]:
        """All snapshots of a request, newest first."""
        found = (
            self._read_snapshot(d)
            for d in self._version_dirs(self._params_dir(tenant, endpoint_id, params))
        )
        return [snapshot for snapshot in found if snapshot]

    def latest(
        self, tenant: str, endpoint_id: str, params: Mapping[str, str]
    ) -> Optional[Snapshot]:
        """The newest snapshot of a request, if there is one."""
        for directory in self._version_dirs(
            self._params_dir(tenant, endpoint_id, params)
        ):
            snapshot = self._read_snapshot(directory)
            if snapshot:
                return snapshot
        return None

    # -- browsing ------------------------------------------------------------------

    @staticmethod
    def _value(directory: Any, prefix: str) -> str:
        return directory.name[len(prefix) :]

    def tenants(self) -> List[str]:
        """The tenants that have data, as folder values."""
        return [self._value(d, "tenant=") for d in self._children(self.root, "tenant=")]

    def endpoints(self, tenant: str) -> List[str]:
        """The endpoints that have data in a tenant (registry ids where known)."""
        names = []
        for directory in self._children(self._tenant_dir(tenant), "endpoint="):
            name = self._value(directory, "endpoint=")
            for params_dir in self._children(directory, "params="):
                snapshot = next(
                    filter(
                        None, map(self._read_snapshot, self._version_dirs(params_dir))
                    ),
                    None,
                )
                if snapshot:
                    name = snapshot.manifest.get("endpoint", name)
                    break
            else:
                days = self._children(directory, "dt=")
                if days and (days[-1] / MANIFEST).exists():
                    try:
                        manifest = json.loads(
                            (days[-1] / MANIFEST).read_text(encoding="utf-8")
                        )
                        name = manifest.get("endpoint", name)
                    except (OSError, ValueError):
                        pass
            names.append(name)
        return sorted(names)

    def parameter_sets(self, tenant: str, endpoint_id: str) -> List[ParameterSet]:
        """Every set of parameters an endpoint was fetched with, with its newest snapshot."""
        sets = []
        endpoint_dir = self._endpoint_dir(tenant, endpoint_id)
        for params_dir in self._children(endpoint_dir, "params="):
            snapshot = next(
                filter(None, map(self._read_snapshot, self._version_dirs(params_dir))),
                None,
            )
            if snapshot:
                sets.append(
                    ParameterSet(
                        hash=self._value(params_dir, "params="),
                        params=dict(snapshot.manifest.get("params", {})),
                        latest=snapshot,
                    )
                )
        return sets

    # -- cleaning up ---------------------------------------------------------------

    @staticmethod
    def _rmtree(path: Any) -> None:
        if isinstance(path, CloudPath):
            path.rmtree()
        else:
            shutil.rmtree(path, ignore_errors=True)

    def prune(
        self,
        keep: int = 1,
        tenant: Optional[str] = None,
        endpoint_id: Optional[str] = None,
    ) -> int:
        """Delete old snapshot versions, keeping the newest ``keep`` of each request.

        Event logs and interrupted writes are left alone.

        :param keep: how many versions of each request to keep (at least 1)
        :param tenant: only this tenant (default: all)
        :param endpoint_id: only this endpoint (default: all)
        :return: the number of versions deleted
        """
        if keep < 1:
            raise ValueError("keep must be at least 1")
        self._check_writable()
        removed = 0
        tenant_dirs = (
            [self._tenant_dir(tenant)]
            if tenant
            else self._children(self.root, "tenant=")
        )
        for tenant_dir in tenant_dirs:
            endpoint_dirs = (
                [tenant_dir / f"endpoint={safe_name(endpoint_id)}"]
                if endpoint_id
                else self._children(tenant_dir, "endpoint=")
            )
            for endpoint_dir in endpoint_dirs:
                for params_dir in self._children(endpoint_dir, "params="):
                    complete = [
                        d
                        for d in self._version_dirs(params_dir)
                        if (d / MANIFEST).exists()
                    ]
                    for directory in complete[keep:]:
                        self._rmtree(directory)
                        removed += 1
                    for day_dir in self._children(params_dir, "dt="):
                        if not any(True for _ in day_dir.iterdir()):
                            self._rmtree(day_dir)
        return removed

    # -- state ---------------------------------------------------------------------

    def _state_path(self, tenant: str, name: str) -> Any:
        return self._tenant_dir(tenant) / "_state" / f"{safe_name(name)}.json"

    def read_state(self, tenant: str, name: str) -> Optional[Dict[str, Any]]:
        """A small JSON document kept beside the data of a tenant, such as sync progress.

        :return: the document, or ``None`` if there is none or it cannot be read
        """
        path = self._state_path(tenant, name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.warning(f"Ignoring unreadable state {path}: {error}")
            return None
        return data if isinstance(data, dict) else None

    def write_state(self, tenant: str, name: str, data: Mapping[str, Any]) -> None:
        """Replace a state document (written atomically, like every file of the lake)."""
        self._write_bytes(self._state_path(tenant, name), _dump(dict(data), indent=2))

    # -- event logs ----------------------------------------------------------------

    def _day_dir(self, tenant: str, endpoint_id: str, day: date) -> Any:
        return self._endpoint_dir(tenant, endpoint_id) / f"dt={day.isoformat()}"

    def event_day(self, tenant: str, endpoint_id: str, day: date) -> Optional[EventDay]:
        """The state of one day of events, if any was stored."""
        directory = self._day_dir(tenant, endpoint_id, day)
        manifest_path = directory / MANIFEST
        if not manifest_path.exists():
            return None
        try:
            return EventDay(
                json.loads(manifest_path.read_text(encoding="utf-8")), directory
            )
        except (OSError, ValueError) as error:
            logger.warning(f"Ignoring unreadable manifest {manifest_path}: {error}")
            return None

    def event_days(self, tenant: str, endpoint_id: str) -> List[EventDay]:
        """All stored days of events, newest first."""
        days = []
        for directory in reversed(
            self._children(self._endpoint_dir(tenant, endpoint_id), "dt=")
        ):
            day = self.event_day(
                tenant, endpoint_id, date.fromisoformat(self._value(directory, "dt="))
            )
            if day:
                days.append(day)
        return days

    def read_events(
        self, tenant: str, endpoint_id: str, day: date
    ) -> Iterator[Dict[str, Any]]:
        """The stored events of a day, in the order they were stored."""
        stored = self.event_day(tenant, endpoint_id, day)
        if stored is None:
            return
        for part in stored.manifest.get("parts", []):
            text = (stored.directory / part).read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip():
                    yield json.loads(line)

    def append_events(
        self,
        tenant: str,
        endpoint_id: str,
        day: date,
        events: Sequence[Mapping[str, Any]],
        *,
        id_field: str = "Id",
        cursor: Optional[str] = None,
        sealed: bool = False,
        profile: Optional[str] = None,
        request: Optional[Mapping[str, Any]] = None,
        at: Optional[datetime] = None,
    ) -> int:
        """Add events to a day, skipping the ones that are already stored.

        Events are told apart by ``id_field``; events without one are always added.

        :param tenant: tenant the events belong to
        :param endpoint_id: registry id of the endpoint
        :param day: the UTC day the events happened on
        :param events: the events, in the order received
        :param id_field: the field that identifies an event
        :param cursor: where to resume fetching, saved in the manifest (``None`` keeps
            the previous cursor, an empty string clears it)
        :param sealed: mark the day as complete
        :param profile: name of the profile used (not the token)
        :param request: what was asked: never headers
        :param at: when the events were received (default: now)
        :return: the number of events that were new
        :raises StoreError: if the day is already sealed
        """
        request = self._check_request(request)
        with self._event_lock:
            previous = self.event_day(tenant, endpoint_id, day)
            if previous and previous.sealed:
                raise StoreError(
                    f"{endpoint_id} on {day} is sealed: no more events can be added"
                )

            known = {
                event[id_field]
                for event in self.read_events(tenant, endpoint_id, day)
                if id_field in event
            }
            fresh: List[Mapping[str, Any]] = []
            for event in events:
                key = event.get(id_field)
                if key is not None:
                    if key in known:
                        continue
                    known.add(key)
                fresh.append(event)

            directory = self._day_dir(tenant, endpoint_id, day)
            manifest: Dict[str, Any] = (
                dict(previous.manifest)
                if previous
                else {
                    "schema": SCHEMA,
                    "kind": "events",
                    "endpoint": endpoint_id,
                    "tenant": tenant,
                    "day": day.isoformat(),
                    "id_field": id_field,
                    "parts": [],
                    "rows": 0,
                    "sealed": False,
                    "cursor": None,
                    "request": request,
                }
            )
            if fresh:
                part = f"part-{len(manifest['parts']):04d}.jsonl"
                lines = "\n".join(_dump(dict(e)).decode("utf-8") for e in fresh) + "\n"
                self._write_bytes(directory / part, lines.encode("utf-8"))
                manifest["parts"] = [*manifest["parts"], part]
                manifest["rows"] = manifest["rows"] + len(fresh)
            if cursor is not None:
                manifest["cursor"] = cursor or None
            manifest["sealed"] = bool(sealed)
            manifest["profile"] = profile
            manifest["updated_at"] = _utc(at or datetime.now(timezone.utc)).isoformat()
            manifest["cli_version"] = cli_version()
            self._write_bytes(directory / MANIFEST, _dump(manifest, indent=2))
            return len(fresh)

    def seal_day(
        self,
        tenant: str,
        endpoint_id: str,
        day: date,
        at: Optional[datetime] = None,
    ) -> None:
        """Mark a day of events as complete (``at``: when, default now)."""
        with self._event_lock:
            stored = self.event_day(tenant, endpoint_id, day)
            if stored is None:
                raise StoreError(f"No events stored for {endpoint_id} on {day}")
            manifest = dict(stored.manifest)
            manifest["sealed"] = True
            manifest["updated_at"] = _utc(at or datetime.now(timezone.utc)).isoformat()
            self._write_bytes(stored.directory / MANIFEST, _dump(manifest, indent=2))
