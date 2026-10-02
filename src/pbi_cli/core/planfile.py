"""A plan file: what to keep in the data lake, for which workspaces, through which account.

A plan file is a small YAML document that `pbi sync plan --config`, `pbi sync run --config`
and `pbi tui --config` read. It names accounts by the profiles of ``pbi auth`` (never a
token), says what to keep for the whole tenant and for particular workspaces, and holds a
few settings for a terminal UI session. The file can be anywhere, a git repository for
example; a relative path in it is relative to the folder of the file.

```yaml
version: 1
accounts:
  admin: admin-nlm
  user: [svc-finance]
tenant:
  targets: [default, activity]
  activity_days: 7
workspaces:
  - name: "Finance*"
    scan: {lineage: true}
    details: [users, datasources]
    via: auto
session:
  lake: ~/PowerBI/shared-lake
  lazy: ask
```

The file is strict: a key that is not known is an error, so that a misspelled one does not
silently do nothing, and every message names the file, the line and the key.

A file *compiles* to ordinary sync runs (`Step`): the engine, the state of a sync, resuming
and the reports are the ones of ``pbi sync``. Compiling needs to know which accounts are
stored (`PlanFile.accounting`), and, for the steps of the workspaces, which workspaces the
lake knows (their names, and which user account lists them): the steps for the tenant come
first because they fetch the list of workspaces.

```python
plan = PlanFile.load("pbi-plan.yaml")
accounting = plan.accounting(engine)
steps = plan.first_steps(accounting)
later = plan.workspace_steps(accounting, catalog.workspaces())
```
"""

import codecs
import difflib
import re
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    NoReturn,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Union,
)

import yaml

from pbi_cli.core.details import TITLES
from pbi_cli.core.registry import Scope
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.sync.plan import MAX_DAYS, SyncOptions
from pbi_cli.core.sync.targets import (
    ALL,
    DEFAULT,
    TARGETS,
    Target,
    get_target,
    select_targets,
)
from pbi_cli.errors import PBIError

#: The version of the format this module reads.
VERSION = 1

#: ``via`` says which account reads a workspace: the administrator's, a user's, whichever can
#: (the default), or the profile that is named.
VIA_AUTO = "auto"
VIA_ADMIN = "admin"
VIA_USER = "user"
_VIA_WORDS = (VIA_AUTO, VIA_ADMIN, VIA_USER)

#: What a terminal UI session does about a detail that the lake does not hold.
LAZY_ASK = "ask"
LAZY_AUTO = "auto"
LAZY_OFF = "off"
LAZY_MODES = (LAZY_ASK, LAZY_AUTO, LAZY_OFF)

#: The keys of the options of a scan, as in the file of ``pbi workspaces scan batch``.
SCAN_KEYS = (
    "lineage",
    "datasource_details",
    "dataset_schema",
    "dataset_expressions",
    "get_artifact_users",
)

_TOP_KEYS = ("version", "accounts", "tenant", "workspaces", "session")
_ACCOUNT_KEYS = ("admin", "user")
_TENANT_KEYS = (
    "targets",
    "activity_days",
    "scan",
    "full_scan",
    "exclude_personal",
    "exclude_inactive",
)
_WORKSPACE_KEYS = ("id", "name", "scan", "details", "via")
_SESSION_KEYS = ("lake", "open", "lazy")

_URL = re.compile(r"^\w[\w+.-]*://")


class PlanFileError(PBIError):
    """A plan file that cannot be used. The message names the file, the line and the key."""


# -- reading the file ----------------------------------------------------------------------


class _Mapping(dict):
    """A mapping read from the file, which knows the line each key was on."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: Dict[Any, int] = {}


class _Sequence(list):
    """A list read from the file, which knows the line each entry was on."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: List[int] = []


class _Loader(yaml.SafeLoader):
    """A safe YAML loader that keeps line numbers and refuses a key that is given twice."""


def _construct_mapping(loader: _Loader, node: Any) -> _Mapping:
    loader.flatten_mapping(node)
    found = _Mapping()
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            hash(key)
        except TypeError:
            raise yaml.constructor.ConstructorError(
                None, None, "found a key that is not text", key_node.start_mark
            ) from None
        if key in found:
            raise yaml.constructor.ConstructorError(
                None, None, f"found the key {key!r} twice", key_node.start_mark
            )
        found[key] = loader.construct_object(value_node, deep=True)
        found.lines[key] = key_node.start_mark.line + 1
    return found


def _construct_sequence(loader: _Loader, node: Any) -> _Sequence:
    found = _Sequence()
    for child in node.value:
        found.append(loader.construct_object(child, deep=True))
        found.lines.append(child.start_mark.line + 1)
    return found


_Loader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)
_Loader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_SEQUENCE_TAG, _construct_sequence
)


def _line(container: Any, key: Any) -> Optional[int]:
    """The line of a key of a mapping, or of an entry of a list, when it is known."""
    lines = getattr(container, "lines", None)
    if isinstance(lines, dict):
        return lines.get(key)
    if isinstance(lines, list) and isinstance(key, int) and 0 <= key < len(lines):
        return lines[key]
    return None


class _Reader:
    """Checks the pieces of a file and says where a piece is wrong.

    :param name: how the file is called in messages
    """

    def __init__(self, name: str):
        self.name = name

    def fail(self, where: str, problem: str, line: Optional[int] = None) -> NoReturn:
        place = f"{self.name}:{line}" if line else self.name
        raise PlanFileError(
            f"{place}: {where}: {problem}" if where else f"{place}: {problem}"
        )

    @staticmethod
    def join(path: str, key: Any) -> str:
        if isinstance(key, int):
            return f"{path}[{key}]"
        return f"{path}.{key}" if path else str(key)

    def mapping(
        self,
        value: Any,
        path: str,
        allowed: Sequence[str],
        line: Optional[int] = None,
    ) -> Mapping[str, Any]:
        """A mapping that has no key besides the allowed ones."""
        if not isinstance(value, dict):
            self.fail(path, "must be a mapping (key: value)", line)
        for key in value:
            if key not in allowed:
                where = self.join(path, key)
                close = difflib.get_close_matches(str(key), allowed, n=1)
                hint = f" Did you mean '{close[0]}'?" if close else ""
                self.fail(
                    where,
                    f"unknown key.{hint} The keys here are: {', '.join(allowed)}.",
                    _line(value, key),
                )
        return value

    def text(
        self, box: Mapping[str, Any], key: str, path: str, default: Optional[str] = None
    ) -> Optional[str]:
        if key not in box:
            return default
        value = box[key]
        if not isinstance(value, str) or not value.strip():
            quote = " (put it in quotes)" if isinstance(value, (int, float)) else ""
            self.fail(self.join(path, key), f"must be text{quote}", _line(box, key))
        return value.strip()

    def boolean(
        self, box: Mapping[str, Any], key: str, path: str, default: bool = False
    ) -> bool:
        if key not in box:
            return default
        if not isinstance(box[key], bool):
            self.fail(self.join(path, key), "must be true or false", _line(box, key))
        return bool(box[key])

    def integer(
        self,
        box: Mapping[str, Any],
        key: str,
        path: str,
        low: int,
        high: int,
    ) -> Optional[int]:
        if key not in box:
            return None
        value = box[key]
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(self.join(path, key), "must be a whole number", _line(box, key))
        if not low <= value <= high:
            self.fail(
                self.join(path, key),
                f"must be from {low} to {high}",
                _line(box, key),
            )
        return int(value)

    def strings(self, box: Mapping[str, Any], key: str, path: str) -> Tuple[str, ...]:
        """A list of texts; nothing for a key that is not there."""
        if key not in box:
            return ()
        value = box[key]
        where = self.join(path, key)
        if not isinstance(value, list):
            self.fail(where, "must be a list, for example [a, b]", _line(box, key))
        found: List[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                self.fail(
                    self.join(where, index),
                    "must be text",
                    _line(value, index) or _line(box, key),
                )
            if item.strip() not in found:
                found.append(item.strip())
        return tuple(found)


# -- the pieces of a plan --------------------------------------------------------------------


@dataclass(frozen=True)
class AccountsPlan:
    """The accounts a plan uses, by the profile names of ``pbi auth``.

    :param admin: the profile of the administrator account (default: the active one)
    :param user: the profiles of the user accounts that may read the workspaces (default: the
        active one)
    """

    admin: Optional[str] = None
    user: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TenantPlan:
    """What to keep for the whole tenant.

    :param targets: the targets of ``pbi sync`` (none: the plain sync)
    :param activity_days: days of audit events, today included
    :param scan: what the scan of every workspace includes
    :param full_scan: scan every workspace, not only those that changed
    :param exclude_personal: leave personal workspaces out of the scan
    :param exclude_inactive: leave inactive workspaces out of the scan
    """

    targets: Tuple[str, ...] = ()
    activity_days: Optional[int] = None
    scan: ScanFlags = ScanFlags()
    full_scan: bool = False
    exclude_personal: bool = False
    exclude_inactive: bool = False


@dataclass(frozen=True)
class WorkspaceEntry:
    """What to keep for some workspaces.

    :param id: the id of a workspace (or ``name``)
    :param name: a pattern for the names of workspaces (``*`` is any text, ``?`` one
        character), looked up in the list of workspaces in the lake
    :param scan: a metadata scan of them with these options (``None``: no scan)
    :param details: details of every item in them (`pbi_cli.core.details.TITLES`)
    :param via: whose account reads them: ``auto``, ``admin``, ``user`` or a profile name
    :param index: its place in the list of workspaces of the file, from 0
    :param line: its line in the file, when known
    """

    id: str = ""
    name: str = ""
    scan: Optional[ScanFlags] = None
    details: Tuple[str, ...] = ()
    via: str = VIA_AUTO
    index: int = 0
    line: int = 0

    @property
    def label(self) -> str:
        """What the entry is called in a message."""
        return self.id or self.name

    @property
    def where(self) -> str:
        """Where it is in the file, for a message."""
        return f"workspaces[{self.index}] ({self.label})"

    @property
    def names_a_profile(self) -> bool:
        return self.via not in _VIA_WORDS


@dataclass(frozen=True)
class SessionPlan:
    """Settings for a terminal UI session (``pbi tui --config``).

    :param lake: the lake to look at (a folder or a URL; relative to the file)
    :param open: a workspace to select at the start: an id, or a name pattern
    :param lazy: what to do about a detail the lake does not hold: ``ask`` (press ``f``),
        ``auto`` (fetch the harmless ones after you stay on an item) or ``off``
    """

    lake: Optional[str] = None
    open: Optional[str] = None
    lazy: str = LAZY_ASK


@dataclass(frozen=True)
class Step:
    """One ordinary run that a plan is made of.

    :param title: what it is for, in words
    :param options: what the run does
    """

    title: str
    options: SyncOptions


@dataclass(frozen=True)
class Accounting:
    """Which accounts a plan uses, once it is known which are stored.

    :param admin: the profile of the administrator account, ``None`` when there is none
    :param users: the profiles of the user accounts, in the order of the file
    """

    admin: Optional[str] = None
    users: Tuple[str, ...] = ()

    def labels(self) -> List[str]:
        """The accounts in words, each as ``profile (kind)``: the administrator's first."""
        found = [f"{self.admin} (admin)"] if self.admin else []
        return found + [f"{profile} (user)" for profile in self.users]

    @property
    def available(self) -> FrozenSet[Scope]:
        """The kinds of account the plan has."""
        found = set()
        if self.admin:
            found.add(Scope.ADMIN)
        if self.users:
            found.add(Scope.USER)
        return frozenset(found)


@dataclass
class Compiled:
    """The steps for the workspaces of a plan.

    :param steps: the runs
    :param notes: what the reader should know (a detail that no account can fetch, ...)
    :param unmatched: the entries that matched no workspace: where each is in the file, and
        what is wrong with it in words
    """

    steps: List[Step] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    unmatched: List[Tuple[str, str]] = field(default_factory=list)


class Accounts(Protocol):
    """What a plan asks of the sync engine about the stored accounts."""

    def has_account(self, scope: Scope, profile: Optional[str] = None) -> bool: ...

    def profile_of(
        self, scope: Scope, profile: Optional[str] = None
    ) -> Optional[str]: ...


def _decode(data: bytes) -> str:
    """The text of a file: UTF-16 when its byte order mark says so (it is what PowerShell
    writes by default), else UTF-8 (a byte order mark in front of it is skipped by the YAML
    reader)."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8")


class _Listed:
    """A workspace that a plan names by id and the lake has no list entry for."""

    def __init__(self, workspace_id: str):
        self.id = workspace_id
        self.name = workspace_id
        self.visible_to: List[str] = []


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def describe_scan(flags: ScanFlags) -> str:
    """The options of a scan in words (``lineage, datasource details``)."""
    chosen = [name.replace("_", " ") for name in SCAN_KEYS if getattr(flags, name)]
    return ", ".join(chosen) if chosen else "no options"


def _providers(detail: str) -> Dict[str, List[Target]]:
    """The targets that fetch a detail, by the kind of item they fetch it for."""
    found: Dict[str, List[Target]] = {}
    for target in TARGETS:
        if target.detail == detail and target.item:
            found.setdefault(target.item, []).append(target)
    return found


def _pattern(text: str) -> "re.Pattern[str]":
    """A name pattern as a regular expression: ``*`` is any text, ``?`` one character."""
    parts = []
    for char in text:
        parts.append(".*" if char == "*" else "." if char == "?" else re.escape(char))
    return re.compile("".join(parts) + r"\Z", re.IGNORECASE | re.DOTALL)


def find_workspace(wanted: str, workspaces: Sequence[Any]) -> Optional[Any]:
    """The workspace that ``session.open`` of a plan file means.

    :param wanted: an id, or a name pattern (as in `WorkspaceEntry.name`)
    :param workspaces: the workspaces the lake knows, in the order to look at them
    :return: the one with this id, else the first whose name matches (an active one before
        another), or ``None``
    """
    for workspace in workspaces:
        if workspace.id == wanted:
            return workspace
    pattern = _pattern(wanted)
    found = [w for w in workspaces if pattern.match(w.name)]
    # active ones first; the sort is stable, so the names stay in the order they came in
    found.sort(key=lambda w: not getattr(w, "active", True))
    return found[0] if found else None


class PlanFile:
    """A plan file, read and checked.

    :param version: the version of the format
    :param accounts: the accounts it uses
    :param tenant: what to keep for the whole tenant (``None``: nothing)
    :param workspaces: what to keep for particular workspaces
    :param session: settings for a terminal UI session
    :param path: where the file is (``None`` when it was not read from a file)
    """

    def __init__(
        self,
        *,
        version: int = VERSION,
        accounts: AccountsPlan = AccountsPlan(),
        tenant: Optional[TenantPlan] = None,
        workspaces: Sequence[WorkspaceEntry] = (),
        session: SessionPlan = SessionPlan(),
        path: Optional[Path] = None,
    ):
        self.version = version
        self.accounts = accounts
        self.tenant = tenant
        self.workspaces = tuple(workspaces)
        self.session = session
        self.path = path

    @property
    def name(self) -> str:
        """How the file is called: its name, or a word for a plan that is not from a file."""
        return self.path.name if self.path is not None else "the plan"

    # -- reading ----------------------------------------------------------------------------

    @classmethod
    def load(cls, path: Union[str, Path]) -> "PlanFile":
        """Read and check a plan file.

        :raises PlanFileError: when the file cannot be read, is not valid YAML or does not
            say what a plan says; the message names the file, the line and the key
        """
        place = Path(path).expanduser()
        try:
            text = _decode(place.read_bytes())
        except (OSError, UnicodeDecodeError) as error:
            raise PlanFileError(
                f"Cannot read the plan file {place}: {error}"
            ) from error
        return cls.parse(text, place.resolve())

    @classmethod
    def parse(cls, text: str, path: Optional[Path] = None) -> "PlanFile":
        """Check the text of a plan file.

        :param text: the YAML
        :param path: where it came from, for messages and for relative paths
        :raises PlanFileError: as `load` does
        """
        reader = _Reader(path.name if path is not None else "the plan")
        try:
            raw = yaml.load(text, Loader=_Loader)  # a safe loader, see _Loader
        except yaml.YAMLError as error:
            mark = getattr(error, "problem_mark", None)
            problem = getattr(error, "problem", None) or str(error).splitlines()[0]
            reader.fail(
                "", f"not valid YAML: {problem}", mark.line + 1 if mark else None
            )
        if raw is None:
            reader.fail("", "the file is empty: a plan starts with `version: 1`")
        root = reader.mapping(raw, "", _TOP_KEYS)

        if "version" not in root:
            reader.fail("version", "missing: a plan starts with `version: 1`")
        version = reader.integer(root, "version", "", 0, 10**6)
        if version != VERSION:
            reader.fail(
                "version",
                f"{version} is not a version this pbi reads (it reads {VERSION})",
                _line(root, "version"),
            )

        return cls(
            version=VERSION,
            accounts=cls._accounts(reader, root),
            tenant=cls._tenant(reader, root),
            workspaces=cls._workspaces(reader, root),
            session=cls._session(reader, root),
            path=path,
        )

    @staticmethod
    def _accounts(reader: _Reader, root: Mapping[str, Any]) -> AccountsPlan:
        if "accounts" not in root or root["accounts"] is None:
            return AccountsPlan()
        box = reader.mapping(
            root["accounts"], "accounts", _ACCOUNT_KEYS, _line(root, "accounts")
        )
        return AccountsPlan(
            admin=reader.text(box, "admin", "accounts"),
            user=reader.strings(box, "user", "accounts"),
        )

    @staticmethod
    def _flags(reader: _Reader, box: Mapping[str, Any], key: str, path: str) -> Any:
        """The options of a scan: a mapping of flags, or ``true`` for a scan with none."""
        value = box[key]
        where = reader.join(path, key)
        if isinstance(value, bool):
            return ScanFlags() if value else None
        inner = reader.mapping(value, where, SCAN_KEYS, _line(box, key))
        return ScanFlags(
            **{name: reader.boolean(inner, name, where) for name in SCAN_KEYS}
        )

    @classmethod
    def _tenant(cls, reader: _Reader, root: Mapping[str, Any]) -> Optional[TenantPlan]:
        if "tenant" not in root:
            return None
        box = root["tenant"]
        if box is None:  # `tenant:` with nothing under it: the plain sync
            box = {}
        box = reader.mapping(box, "tenant", _TENANT_KEYS, _line(root, "tenant"))

        targets = reader.strings(box, "targets", "tenant")
        for index, name in enumerate(targets):
            if name in (DEFAULT, ALL):
                continue
            try:
                get_target(name)
            except PBIError:
                close = difflib.get_close_matches(name, [t.name for t in TARGETS], n=1)
                hint = f" Did you mean '{close[0]}'?" if close else ""
                reader.fail(
                    f"tenant.targets[{index}]",
                    f"'{name}' is not a target.{hint} The targets are: "
                    f"{', '.join(t.name for t in TARGETS)}; or '{DEFAULT}' and '{ALL}'.",
                    _line(box.get("targets"), index) or _line(box, "targets"),
                )
        chosen = {t.name for t in select_targets(targets).targets}

        def needs(key: str, target: str) -> None:
            if key in box and target not in chosen:
                reader.fail(
                    f"tenant.{key}",
                    f"has no effect: '{target}' is not among tenant.targets",
                    _line(box, key),
                )

        needs("activity_days", "activity")
        for key in ("scan", "full_scan", "exclude_personal", "exclude_inactive"):
            needs(key, "scan")

        scan = ScanFlags()
        if "scan" in box:
            found = cls._flags(reader, box, "scan", "tenant")
            scan = found if found is not None else ScanFlags()
        return TenantPlan(
            targets=targets,
            activity_days=reader.integer(box, "activity_days", "tenant", 1, MAX_DAYS),
            scan=scan,
            full_scan=reader.boolean(box, "full_scan", "tenant"),
            exclude_personal=reader.boolean(box, "exclude_personal", "tenant"),
            exclude_inactive=reader.boolean(box, "exclude_inactive", "tenant"),
        )

    @classmethod
    def _workspaces(
        cls, reader: _Reader, root: Mapping[str, Any]
    ) -> Tuple[WorkspaceEntry, ...]:
        if "workspaces" not in root:
            return ()
        listing = root["workspaces"]
        if listing is None:
            return ()
        if not isinstance(listing, list):
            reader.fail(
                "workspaces",
                "must be a list of entries, each starting with `- id:` or `- name:`",
                _line(root, "workspaces"),
            )
        found = []
        for index, item in enumerate(listing):
            path = f"workspaces[{index}]"
            line = _line(listing, index)
            box = reader.mapping(item, path, _WORKSPACE_KEYS, line)
            if ("id" in box) == ("name" in box):
                reader.fail(
                    path,
                    "needs either `id:` (one workspace) or `name:` (a pattern), not both "
                    "and not neither",
                    line,
                )
            details = reader.strings(box, "details", path)
            for number, name in enumerate(details):
                if name not in TITLES:
                    close = difflib.get_close_matches(name, list(TITLES), n=1)
                    hint = f" Did you mean '{close[0]}'?" if close else ""
                    reader.fail(
                        f"{path}.details[{number}]",
                        f"'{name}' is not a detail.{hint} The details are: "
                        f"{', '.join(TITLES)}.",
                        _line(box.get("details"), number) or _line(box, "details"),
                    )
            scan = None
            if "scan" in box:
                scan = cls._flags(reader, box, "scan", path)
            if scan is None and not details:
                reader.fail(
                    path,
                    "says nothing to keep: add `scan:` (a metadata scan) or `details:`",
                    line,
                )
            found.append(
                WorkspaceEntry(
                    id=reader.text(box, "id", path) or "",
                    name=reader.text(box, "name", path) or "",
                    scan=scan,
                    details=details,
                    via=reader.text(box, "via", path, VIA_AUTO) or VIA_AUTO,
                    index=index,
                    line=line or 0,
                )
            )
        return tuple(found)

    @staticmethod
    def _session(reader: _Reader, root: Mapping[str, Any]) -> SessionPlan:
        if "session" not in root or root["session"] is None:
            return SessionPlan()
        box = reader.mapping(
            root["session"], "session", _SESSION_KEYS, _line(root, "session")
        )
        lazy = LAZY_ASK
        if "lazy" in box:
            if box["lazy"] is False:  # YAML reads a bare `off` as false
                lazy = LAZY_OFF
            else:
                lazy = reader.text(box, "lazy", "session") or LAZY_ASK
        if lazy not in LAZY_MODES:
            reader.fail(
                "session.lazy",
                f"'{lazy}' is not a mode. The modes are: {', '.join(LAZY_MODES)}.",
                _line(box, "lazy"),
            )
        return SessionPlan(
            lake=reader.text(box, "lake", "session"),
            open=reader.text(box, "open", "session"),
            lazy=lazy,
        )

    # -- what the file says about itself ------------------------------------------------------

    @property
    def lake(self) -> Optional[str]:
        """The lake of the session, as a place to open: a URL as it is, a folder made
        absolute (``~`` is the home folder, and a relative folder is relative to the file).
        """
        wanted = self.session.lake
        if not wanted or _URL.match(wanted):
            return wanted
        place = Path(wanted).expanduser()
        if not place.is_absolute() and self.path is not None:
            place = self.path.parent / place
        return str(place)

    @property
    def profiles(self) -> List[str]:
        """The profiles the file names, in the order it names them."""
        found: List[str] = []
        names = [self.accounts.admin or "", *self.accounts.user]
        names += [e.via for e in self.workspaces if e.names_a_profile]
        for name in names:
            if name and name not in found:
                found.append(name)
        return found

    # -- accounts -----------------------------------------------------------------------------

    def accounting(self, accounts: Accounts) -> Accounting:
        """Which accounts the plan uses: the profiles it names, else the active ones.

        :param accounts: tells which accounts are stored (the sync engine does)
        :raises PlanFileError: when the plan names a profile under which no token is stored
        """
        admin = self.accounts.admin
        if admin is not None:
            if not accounts.has_account(Scope.ADMIN, admin):
                raise PlanFileError(
                    f"{self.name}: accounts.admin: no token is stored under the profile "
                    f"'{admin}'. Store one with `pbi auth -t <token> -p {admin} -g admin`."
                )
        else:
            admin = accounts.profile_of(Scope.ADMIN)

        users = list(self.accounts.user)
        for index, profile in enumerate(users):
            if not accounts.has_account(Scope.USER, profile):
                raise PlanFileError(
                    f"{self.name}: accounts.user[{index}]: no token is stored under the "
                    f"profile '{profile}'. Store one with "
                    f"`pbi auth -t <token> -p {profile} -g user`."
                )
        if not users:
            active = accounts.profile_of(Scope.USER)
            users = [active] if active else []

        for entry in self.workspaces:
            if entry.names_a_profile and not accounts.has_account(
                Scope.USER, entry.via
            ):
                raise PlanFileError(
                    f"{self.name}:{entry.line}: {entry.where}.via: no token is stored under "
                    f"the profile '{entry.via}'. Store one with "
                    f"`pbi auth -t <token> -p {entry.via} -g user`."
                )
        return Accounting(admin=admin, users=tuple(users))

    # -- the steps of a plan ------------------------------------------------------------------

    def _may_need_a_user(self, accounting: Accounting) -> bool:
        """Whether an entry may need to know which user account lists a workspace."""
        for entry in self.workspaces:
            if entry.via == VIA_USER:
                return True
            if entry.via != VIA_AUTO:
                continue
            for detail in entry.details:
                for candidates in _providers(detail).values():
                    by_admin = any(t.scope is Scope.ADMIN for t in candidates)
                    if not (by_admin and accounting.admin):
                        return True
        return False

    def _tenant_steps(self, accounting: Accounting) -> List[Step]:
        tenant = self.tenant
        if tenant is None:
            return []
        try:
            selection = select_targets(tenant.targets, accounting.available)
        except PBIError as error:
            raise PlanFileError(f"{self.name}: tenant.targets: {error}") from error
        steps = []
        for scope in (Scope.ADMIN, Scope.USER):
            names = tuple(t.name for t in selection.targets if t.scope is scope)
            if not names:
                continue
            profiles: Sequence[Optional[str]] = (
                [accounting.admin] if scope is Scope.ADMIN else accounting.users
            )
            for profile in profiles:
                steps.append(
                    Step(
                        f"the tenant ({profile})",
                        SyncOptions(
                            targets=names,
                            days=tenant.activity_days or MAX_DAYS,
                            scan_flags=tenant.scan,
                            full_scan=tenant.full_scan,
                            exclude_personal=tenant.exclude_personal,
                            exclude_inactive=tenant.exclude_inactive,
                            admin_profile=profile if scope is Scope.ADMIN else None,
                            user_profile=profile if scope is Scope.USER else None,
                        ),
                    )
                )
        return steps

    def first_steps(self, accounting: Accounting) -> List[Step]:
        """The steps that need nothing from the lake: the tenant, and what tells which user
        account lists which workspace (the list of workspaces of each user account, one
        request each, when an entry may need it).

        :raises PlanFileError: when the targets of the tenant need an account that is not
            stored
        """
        steps = self._tenant_steps(accounting)
        if self._may_need_a_user(accounting):
            done = {
                s.options.user_profile
                for s in steps
                if "user-groups" in s.options.targets  # a parent is among the names
            }
            for profile in accounting.users:
                if profile not in done:
                    steps.append(
                        Step(
                            f"the workspaces of {profile}",
                            SyncOptions(targets=("user-groups",), user_profile=profile),
                        )
                    )
        return steps

    def _check(self, entry: WorkspaceEntry, accounting: Accounting) -> List[str]:
        """Refuse an entry that can never be carried out, and say what in it can not be.

        :return: notes about the details that some kinds of item cannot give
        :raises PlanFileError: when the entry can fetch nothing it asks for
        """
        if entry.scan is not None and not accounting.admin:
            raise PlanFileError(
                f"{self.name}:{entry.line}: {entry.where}.scan: a scan needs an "
                "administrator account, and none is stored. Store one with "
                "`pbi auth -t <token> -g admin`."
            )
        notes: List[str] = []
        for detail in entry.details:
            by_kind = _providers(detail)
            served, unserved = [], []
            for kind, candidates in by_kind.items():
                admin = any(t.scope is Scope.ADMIN for t in candidates)
                user = any(t.scope is Scope.USER for t in candidates)
                if entry.via == VIA_ADMIN:
                    ok = admin and bool(accounting.admin)
                elif entry.via == VIA_AUTO:
                    ok = (admin and bool(accounting.admin)) or (
                        user and bool(accounting.users)
                    )
                else:  # a user's account: user, or a profile that is named
                    ok = user and (bool(accounting.users) or entry.names_a_profile)
                (served if ok else unserved).append(kind)
            if not served:
                raise PlanFileError(
                    f"{self.name}:{entry.line}: {entry.where}.details: {self._cannot(entry, detail, accounting)}"
                )
            if unserved:
                notes.append(
                    f"{entry.where}: the {detail} of {', '.join(sorted(unserved))} are not "
                    f"fetched: {self._reason(entry, detail, accounting)}"
                )
        return notes

    @staticmethod
    def _scopes_of(detail: str) -> Set[Scope]:
        return {t.scope for cands in _providers(detail).values() for t in cands}

    def _reason(
        self, entry: WorkspaceEntry, detail: str, accounting: Accounting
    ) -> str:
        """Why some kinds of item cannot give a detail through the entry's account."""
        scopes = self._scopes_of(detail)
        if entry.via == VIA_ADMIN or (entry.via == VIA_AUTO and not accounting.users):
            return "they need a user's account (the administrator's API has no operation for them)"
        if Scope.ADMIN in scopes:
            return "they need an administrator's account (a user's API has no operation for them)"
        return "no stored account can"

    def _cannot(
        self, entry: WorkspaceEntry, detail: str, accounting: Accounting
    ) -> str:
        """Why an entry can fetch a detail for no kind of item at all."""
        scopes = self._scopes_of(detail)
        if entry.via == VIA_ADMIN:
            if Scope.ADMIN not in scopes:
                return (
                    f"'{detail}' cannot be fetched with the administrator's account: "
                    "only a user's account has an operation for it. Use via: auto, via: "
                    "user, or a profile name."
                )
            return (
                f"'{detail}' needs an administrator account, and none is stored. Store "
                "one with `pbi auth -t <token> -g admin`."
            )
        if entry.via != VIA_AUTO:
            if Scope.USER not in scopes:
                return (
                    f"'{detail}' cannot be fetched with a user's account: only an "
                    "administrator's account has an operation for it. Use via: auto or "
                    "via: admin."
                )
            return (
                f"'{detail}' needs a user account, and none is stored. Store one with "
                "`pbi auth -t <token> -g user`."
            )
        return (
            f"'{detail}' needs an account that is not stored: "
            + (
                "an administrator's (`pbi auth -t <token> -g admin`)"
                if Scope.ADMIN in scopes
                else "a user's (`pbi auth -t <token> -g user`)"
            )
            + "."
        )

    @staticmethod
    def _match(entry: WorkspaceEntry, workspaces: Sequence[Any]) -> List[Any]:
        """The workspaces an entry is about: the one with its id (the plan is trusted when the
        lake has no such workspace), or those whose names match its pattern (active ones that
        are not personal)."""
        if entry.id:
            known = [w for w in workspaces if w.id == entry.id]
            return known or [_Listed(entry.id)]
        pattern = _pattern(entry.name)
        return [
            w
            for w in workspaces
            if pattern.match(w.name)
            and getattr(w, "active", True)
            and not getattr(w, "personal", False)
        ]

    def workspace_steps(
        self, accounting: Accounting, workspaces: Sequence[Any]
    ) -> Compiled:
        """The steps for the workspaces of the plan.

        :param accounting: which accounts the plan uses
        :param workspaces: the workspaces the lake knows (`pbi_cli.core.catalog.Workspace`):
            names are looked up in them, and ``visible_to`` says which user account lists a
            workspace
        :raises PlanFileError: for an entry that can never be carried out
        """
        compiled = Compiled()
        needs: Dict[str, Dict[Tuple[Scope, Optional[str]], Set[str]]] = {}
        scans: Dict[ScanFlags, Set[str]] = {}

        for entry in self.workspaces:
            compiled.notes.extend(self._check(entry, accounting))
            found = self._match(entry, workspaces)
            if not found:
                compiled.unmatched.append(
                    (
                        entry.where,
                        f"no workspace is called '{entry.name}'"
                        + (
                            ""
                            if workspaces
                            else " (the lake holds no list of workspaces yet: the steps "
                            "for the tenant fetch it)"
                        ),
                    )
                )
                continue
            orphans: List[str] = []
            for workspace in found:
                if entry.scan is not None:
                    scans.setdefault(entry.scan, set()).add(workspace.id)
                for detail in entry.details:
                    for candidates in _providers(detail).values():
                        picked = self._pick(entry, workspace, candidates, accounting)
                        if picked is None:
                            continue
                        target, profile = picked
                        if profile is None:
                            if workspace.name not in orphans:
                                orphans.append(workspace.name)
                            continue
                        needs.setdefault(workspace.id, {}).setdefault(
                            (target.scope, profile), set()
                        ).add(target.name)
            if orphans:
                shown = ", ".join(orphans[:3]) + (
                    f" and {len(orphans) - 3} more" if len(orphans) > 3 else ""
                )
                compiled.notes.append(
                    f"{entry.where}: no user account of the plan lists {shown} "
                    f"({', '.join(accounting.users) or 'none stored'}), so what only a "
                    "user can read is not fetched for it; name an account with via: "
                    "<profile>"
                )

        compiled.steps.extend(self._scan_steps(scans, accounting))
        compiled.steps.extend(self._detail_steps(needs, accounting))
        return compiled

    def _pick(
        self,
        entry: WorkspaceEntry,
        workspace: Any,
        candidates: List[Target],
        accounting: Accounting,
    ) -> Optional[Tuple[Target, Optional[str]]]:
        """The target and the profile that fetch one kind of detail for a workspace; the
        profile is ``None`` when it takes a user's account and none lists the workspace.
        """
        admin = next((t for t in candidates if t.scope is Scope.ADMIN), None)
        user = next((t for t in candidates if t.scope is Scope.USER), None)
        if entry.via == VIA_ADMIN:
            return (admin, accounting.admin) if admin and accounting.admin else None
        if entry.via == VIA_AUTO and admin and accounting.admin:
            return admin, accounting.admin
        if user is None:
            return None
        if entry.names_a_profile:
            return user, entry.via
        holder = next((p for p in accounting.users if p in workspace.visible_to), None)
        return user, holder

    @staticmethod
    def _scan_steps(
        scans: Dict[ScanFlags, Set[str]], accounting: Accounting
    ) -> List[Step]:
        steps = []
        for flags, ids in sorted(
            scans.items(),
            # the plainer scans first, then by name, so that the order never varies
            key=lambda item: (
                sum(1 for name in SCAN_KEYS if getattr(item[0], name)),
                describe_scan(item[0]),
            ),
        ):
            steps.append(
                Step(
                    f"scan of {_plural(len(ids), 'workspace')} "
                    f"({describe_scan(flags)})",
                    SyncOptions(
                        targets=("scan",),
                        workspace_ids=tuple(sorted(ids)),
                        scan_flags=flags,
                        admin_profile=accounting.admin,
                    ),
                )
            )
        return steps

    @staticmethod
    def _detail_steps(
        needs: Dict[str, Dict[Tuple[Scope, Optional[str]], Set[str]]],
        accounting: Accounting,
    ) -> List[Step]:
        """One step for each account and set of targets, for the workspaces that need them:
        the administrator's first, then each user in the order of the file."""
        buckets: Dict[Tuple[str, str, Tuple[str, ...]], Set[str]] = {}
        for workspace_id, accounts in needs.items():
            for (scope, profile), names in accounts.items():
                key = (scope.value, profile or "", tuple(sorted(names)))
                buckets.setdefault(key, set()).add(workspace_id)
        steps = []

        def order(item: Tuple[Tuple[str, str, Tuple[str, ...]], Set[str]]) -> Tuple:
            (scope, profile, names), _ = item
            place = (
                accounting.users.index(profile) if profile in accounting.users else 0
            )
            return (scope != "admin", place, profile, names)

        for (scope, profile, names), ids in sorted(buckets.items(), key=order):
            steps.append(
                Step(
                    f"{', '.join(names)} for {_plural(len(ids), 'workspace')} "
                    f"({profile})",
                    SyncOptions(
                        targets=names,
                        workspace_ids=tuple(sorted(ids)),
                        admin_profile=profile if scope == "admin" else None,
                        user_profile=profile if scope == "user" else None,
                    ),
                )
            )
        return steps


# -- what the command line changes -------------------------------------------------------------


@dataclass(frozen=True)
class Overrides:
    """What a flag of the command line changes in every step of a plan.

    :param force: fetch again what the lake holds fresh
    :param max_age: a stored answer younger than this is fresh
    :param days: days of audit events (instead of ``tenant.activity_days``)
    :param workers: requests worked on at the same time
    :param wait: seconds a request may wait for quota before its unit is held back
    :param scan_interval: seconds between two status checks of a scan
    :param scan_timeout: seconds to wait for one scan to succeed
    """

    force: bool = False
    max_age: Optional[timedelta] = None
    days: Optional[int] = None
    workers: Optional[int] = None
    wait: Optional[float] = None
    scan_interval: Optional[float] = None
    scan_timeout: Optional[float] = None

    def apply(self, options: SyncOptions) -> SyncOptions:
        """The options of a step with the flags of the command line put over them."""
        changes: Dict[str, Any] = {}
        if self.force:
            changes["force"] = True
        if self.max_age is not None:
            changes["max_age"] = self.max_age
        if self.days is not None:
            changes["days"] = self.days
        if self.workers is not None:
            changes["workers"] = self.workers
        if self.wait is not None:
            changes["max_wait"] = self.wait
        if self.scan_interval is not None:
            changes["scan_interval"] = self.scan_interval
        if self.scan_timeout is not None:
            changes["scan_timeout"] = self.scan_timeout
        return replace(options, **changes) if changes else options
