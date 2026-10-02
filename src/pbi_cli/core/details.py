"""What more there is to know about one item, who can fetch it, and what the lake holds.

A *detail* is something about one item that costs a request of its own: who has access to
it, its data sources, the pages of a report, how a dataset refreshes. A sync keeps them for
every item, which costs one request each (so they are fetched only when asked for); this
module is for fetching them for *one* item, when somebody looks at it.

Which operations can answer a detail comes from the targets of a sync: a target that has an
``item`` and a ``detail`` is a way to get that detail. An administrator's way comes first,
because it needs no permission on the item.

```python
from pbi_cli.core.details import providers_of

for provider in providers_of("dataset", "users", "ds-1", "ws-1"):
    print(provider.target.name, provider.scope.value, provider.params)
```
"""

from dataclasses import dataclass
from datetime import datetime
from typing import AbstractSet, Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pbi_cli.core.client import rows_of
from pbi_cli.core.registry import Endpoint, Scope, get_endpoint
from pbi_cli.core.store import LakeStore, Snapshot
from pbi_cli.core.sync.plan import SyncOptions, unit_key
from pbi_cli.core.sync.targets import TARGETS, Target

#: The details, in the order they are shown, and what each is called.
TITLES: Dict[str, str] = {
    "users": "Users",
    "datasources": "Data sources",
    "pages": "Pages",
    "refreshes": "Refreshes",
    "parameters": "Parameters",
    "tiles": "Tiles",
}


def detail_names(kind: str) -> List[str]:
    """The details an item of this kind can have, in the order they are shown.

    :param kind: ``workspace``, ``report``, ``dataset``, ``dashboard`` or ``dataflow``
    """
    found = {t.detail for t in TARGETS if t.item == kind and t.detail}
    return [name for name in TITLES if name in found]


@dataclass(frozen=True)
class Provider:
    """One way to fetch a detail of one item: a target, and the request it makes for it.

    :param target: the target of a sync that fetches it
    :param endpoint: the operation behind it
    :param params: the parameters of its request for this item, which is what the answer is
        kept under in the lake
    :param only: what to give a sync (`pbi_cli.core.sync.plan.SyncOptions.only`) so that it
        fetches this item and no other
    """

    target: Target
    endpoint: Endpoint
    params: Dict[str, str]
    only: Dict[str, List[str]]

    @property
    def scope(self) -> Scope:
        """The kind of account that fetches it."""
        return self.target.scope

    @property
    def key(self) -> str:
        """The key of its unit in a sync: how the state of a sync remembers it."""
        return unit_key(self.endpoint.id, self.endpoint.canonical_params(self.params))

    @property
    def needs(self) -> str:
        """What the account must be, in words: an administrator, or a user (with a
        permission on the item, where the documentation says so)."""
        if self.scope is Scope.ADMIN:
            return "an administrator account"
        more = f" with {self.endpoint.needs}" if self.endpoint.needs else ""
        return f"a user account{more}"


def providers_of(
    kind: str, name: str, item_id: str, workspace_id: Optional[str] = None
) -> List[Provider]:
    """The ways to fetch one detail of one item, the administrator's first.

    :param kind: the kind of item
    :param name: which detail
    :param item_id: the id of the item (of the workspace, for a workspace)
    :param workspace_id: the workspace of the item, which the operations of a user need
    """
    found: List[Provider] = []
    for target in TARGETS:
        if target.item != kind or target.detail != name:
            continue
        params: Dict[str, str] = {}
        for placeholder, (source, _) in target.bind.items():
            value = item_id if source == "row" else workspace_id
            if not value:
                break  # without the workspace a user's request cannot be made
            params[placeholder] = value
        else:
            found.append(
                Provider(
                    target,
                    get_endpoint(target.endpoint),
                    params,
                    {placeholder: [value] for placeholder, value in params.items()},
                )
            )
    return found


@dataclass(frozen=True)
class Held:
    """The answer to a request of a provider that is in the lake.

    :param provider: whose request it answers
    :param snapshot: the newest stored answer
    :param rows: how many rows of it are about the item
    """

    provider: Provider
    snapshot: Snapshot
    rows: int

    @property
    def fetched_at(self) -> datetime:
        return self.snapshot.fetched_at


@dataclass(frozen=True)
class Detail:
    """One detail of one item, as the lake knows it.

    :param name: ``users``, ``datasources``, ...
    :param title: what it is called
    :param providers: the ways to fetch it
    :param held: the newest answer the lake holds, from whichever provider made it
    :param problems: what the last syncs said about the providers whose request failed
        (the account may lack a permission), one line each
    :param refused: the keys of those providers
    """

    name: str
    title: str
    providers: Tuple[Provider, ...]
    held: Optional[Held]
    problems: Tuple[str, ...] = ()
    refused: Tuple[str, ...] = ()


def _rows_about(provider: Provider, snapshot: Snapshot, item_id: str) -> int:
    """How many rows of a stored answer are about the item."""
    match = provider.target.match
    if not match:
        stated = snapshot.manifest.get("rows")
        if isinstance(stated, int):
            return stated
        return len(rows_of(provider.endpoint, snapshot.load()))
    rows = rows_of(provider.endpoint, snapshot.load())
    return sum(
        1 for row in rows if isinstance(row, dict) and str(row.get(match)) == item_id
    )


def collect(
    store: LakeStore,
    tenant: str,
    kind: str,
    item_id: str,
    workspace_id: Optional[str],
    units: Mapping[str, Mapping[str, Any]],
) -> List[Detail]:
    """The details of an item, each with what the lake holds of it.

    :param store: the lake
    :param tenant: the tenant
    :param kind: the kind of item
    :param item_id: its id
    :param workspace_id: its workspace, if it has one
    :param units: the units that failed or were held back in the last syncs, by key (the
        ``units`` of the state of a sync)
    """
    found: List[Detail] = []
    for name in detail_names(kind):
        providers = providers_of(kind, name, item_id, workspace_id)
        held: Optional[Held] = None
        problems: List[str] = []
        refused: List[str] = []
        for provider in providers:
            snapshot = store.latest(
                tenant,
                provider.endpoint.id,
                provider.endpoint.canonical_params(provider.params),
            )
            if snapshot is not None and (
                held is None or snapshot.fetched_at > held.snapshot.fetched_at
            ):
                held = Held(
                    provider, snapshot, _rows_about(provider, snapshot, item_id)
                )
            mark = units.get(provider.key)
            if mark and mark.get("status") == "failed" and mark.get("error"):
                problems.append(str(mark["error"]))
                refused.append(provider.key)
        found.append(
            Detail(
                name,
                TITLES[name],
                tuple(providers),
                held,
                tuple(problems),
                tuple(refused),
            )
        )
    return found


def choose(
    detail: Detail, available: Optional[AbstractSet[Scope]] = None
) -> Optional[Provider]:
    """The provider that would fetch a detail now: the first whose kind of account is
    stored, preferring one that did not fail the last time.

    :param detail: the detail
    :param available: the kinds of account that are stored (``None``: any is assumed)
    :return: ``None`` when no stored account can fetch it
    """
    usable = [p for p in detail.providers if available is None or p.scope in available]
    refused = set(detail.refused)
    return next(
        (p for p in usable if p.key not in refused), usable[0] if usable else None
    )


def fetch_options(providers: Sequence[Provider]) -> SyncOptions:
    """The sync that fetches what these providers fetch, for the items they are about and
    no other (`pbi_cli.core.sync.plan.SyncOptions.only`).

    :param providers: ways to fetch details of one item (each for the same item)
    """
    targets = tuple(dict.fromkeys(p.target.name for p in providers))
    only: Dict[str, List[str]] = {}
    for provider in providers:
        for placeholder, ids in provider.only.items():
            wanted = only.setdefault(placeholder, [])
            wanted.extend(i for i in ids if i not in wanted)
    return SyncOptions(targets=targets, only=only)
