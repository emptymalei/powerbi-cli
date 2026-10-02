"""The Explorer: a tree of the tenant, a table of what is in the selected node, and a detail
pane for the selected row.

It reads the data lake only (through `pbi_cli.core.catalog`), so it works offline. Reading
can take a moment for a big lake, so what a selection needs is loaded by a worker thread
that a newer selection replaces.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple, cast

from rich.console import Group
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.tree import TreeNode
from textual.worker import get_current_worker

from pbi_cli.core.catalog import Catalog, Workspace, label
from pbi_cli.core.registry import Scope
from pbi_cli.core.scan import ScanFlags
from pbi_cli.core.sync.plan import Plan, SyncOptions
from pbi_cli.tui import render
from pbi_cli.tui.modals import ConfirmModal
from pbi_cli.tui.render import Entry, Subject
from pbi_cli.tui.status import StatusBar


@dataclass(frozen=True)
class NodeRef:
    """What a node of the tree stands for.

    :param kind: ``root``, ``workspaces``, ``personal``, ``workspace``, ``apps``,
        ``capacities`` or ``activity``
    :param id: the workspace id, for a ``workspace``
    """

    kind: str
    id: str = ""


#: The columns of the table, for each kind of node.
COLUMNS: Dict[str, Tuple[str, ...]] = {
    "root": render.OVERVIEW_COLUMNS,
    "workspaces": render.WORKSPACE_COLUMNS,
    "personal": render.WORKSPACE_COLUMNS,
    "workspace": render.ITEM_COLUMNS,
    "apps": render.APP_COLUMNS,
    "capacities": render.CAPACITY_COLUMNS,
    "activity": render.DAY_COLUMNS,
    "day": render.EVENT_COLUMNS,
}

#: The id of the pane of each tab.
TAB_IDS = {name: f"tab-{name}" for name in render.TABS}

#: The widget that shows the text of each tab that is not a table.
TEXT_WIDGETS = {"info": "#info", "lineage": "#lineage", "json": "#json"}


@dataclass
class Loaded:
    """What a node needs to be shown, loaded by a worker.

    :param ref: the node
    :param title: the line above the table
    :param entries: the rows of the table, before the filter
    :param subject: what the detail pane shows for the node itself, if anything
    """

    ref: NodeRef
    title: str
    entries: List[Entry]
    subject: Optional[Subject] = None


class ExplorerScreen(Screen):
    """Browse the tenant: the tree, the table and the detail pane."""

    BINDINGS = [
        Binding("/", "filter", "Filter"),
        Binding("r", "refresh", "Refresh"),
        Binding("l", "reload", "Reload"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("1", "tab('info')", "Info", show=False),
        Binding("2", "tab('users')", "Users", show=False),
        Binding("3", "tab('lineage')", "Lineage", show=False),
        Binding("4", "tab('json')", "JSON", show=False),
        Binding("5", "tab('versions')", "Versions", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._ref = NodeRef("root")
        self._loaded: Optional[Loaded] = None
        self._entries: List[Entry] = []
        self._by_key: Dict[str, Entry] = {}
        self._subject: Optional[Subject] = None
        self._serial = 0
        self._shown: Dict[str, int] = {}
        self._tree_filter = ""
        self._table_filter = ""
        self._pending: Optional[Tuple[NodeRef, str]] = None
        self._tree_nodes: Dict[NodeRef, TreeNode] = {}

    # -- layout --------------------------------------------------------------------------

    @property
    def pbi(self) -> Any:
        """The app (typed loosely: it imports this module)."""
        return self.app

    def compose(self) -> ComposeResult:
        yield StatusBar()
        with Horizontal(id="explorer"):
            with Vertical(id="tree-pane"):
                yield Input(
                    placeholder="filter workspaces", id="tree-filter", classes="filter"
                )
                yield Tree("Lake", id="tree")
            with Vertical(id="right"):
                with Vertical(id="table-pane"):
                    yield Static("", id="table-title")
                    yield Input(
                        placeholder="filter rows", id="table-filter", classes="filter"
                    )
                    yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                with TabbedContent(initial=TAB_IDS["info"], id="detail"):
                    with TabPane("Info", id=TAB_IDS["info"]):
                        with VerticalScroll():
                            yield Static(id="info")
                    with TabPane("Users", id=TAB_IDS["users"]):
                        yield Static(id="users-note")
                        yield DataTable(
                            id="users", cursor_type="row", zebra_stripes=True
                        )
                    with TabPane("Lineage", id=TAB_IDS["lineage"]):
                        with VerticalScroll():
                            yield Static(id="lineage")
                    with TabPane("JSON", id=TAB_IDS["json"]):
                        with VerticalScroll():
                            yield Static(id="json")
                    with TabPane("Versions", id=TAB_IDS["versions"]):
                        yield Static(id="versions-note")
                        yield DataTable(
                            id="versions", cursor_type="row", zebra_stripes=True
                        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tree", Tree).focus()
        self.rebuild_tree()

    # -- the tree ----------------------------------------------------------------------

    @property
    def catalog(self) -> Optional[Catalog]:
        return cast(Optional[Catalog], self.pbi.catalog)

    def _node_label(
        self, text: str, count: Optional[int] = None, dot: Optional[Text] = None
    ) -> Text:
        label_text = Text()
        if dot is not None:
            label_text.append_text(dot)
            label_text.append(" ")
        label_text.append(text)
        if count is not None:
            label_text.append(f"  {count}", style="grey62")
        return label_text

    def rebuild_tree(self) -> None:
        """Build the tree again from the catalog, keeping the selection where it can."""
        tree = self.query_one("#tree", Tree)
        kept = self._ref
        self._tree_nodes = {}
        catalog = self.catalog
        tenant = self.pbi.tenant
        title = f"Lake · {render.short_id(tenant)}" if tenant else "Lake"
        tree.reset(title, NodeRef("root"))
        self._tree_nodes[NodeRef("root")] = tree.root
        if catalog is None:
            tree.root.add_leaf(Text("reading the lake ...", style="grey62"))
            tree.root.expand()
            return

        tree.root.expand()
        text = self._tree_filter.lower().strip()

        def workspaces(kind: str, title_text: str, personal: bool) -> None:
            found = catalog.workspaces(personal=personal)
            if not found:
                return
            wanted = [w for w in found if text in w.name.lower()] if text else found
            ref = NodeRef(kind)
            level = catalog.listing_freshness("workspace")
            node = tree.root.add(
                self._node_label(title_text, len(found), render.dot(level)), data=ref
            )
            self._tree_nodes[ref] = node
            for workspace in wanted[: render.MAX_NODES]:
                leaf_ref = NodeRef("workspace", workspace.id)
                leaf = node.add_leaf(
                    self._node_label(
                        workspace.name,
                        dot=render.dot(catalog.workspace_freshness(workspace.id)),
                    ),
                    data=leaf_ref,
                )
                self._tree_nodes[leaf_ref] = leaf
            if len(wanted) > render.MAX_NODES:
                node.add_leaf(
                    Text(
                        f"... {len(wanted) - render.MAX_NODES} more: narrow them with /",
                        style="grey62",
                    )
                )
            if text or kind == "workspaces":
                node.expand()

        workspaces("workspaces", "Workspaces", False)
        workspaces("personal", "Personal workspaces", True)
        if catalog.has_apps():
            ref = NodeRef("apps")
            self._tree_nodes[ref] = tree.root.add_leaf(
                self._node_label(
                    "Apps",
                    len(catalog.all_items("app")),
                    render.dot(catalog.apps_freshness()),
                ),
                data=ref,
            )
        if catalog.listing("capacity") is not None:
            ref = NodeRef("capacities")
            self._tree_nodes[ref] = tree.root.add_leaf(
                self._node_label(
                    "Capacities",
                    len(catalog.capacities()),
                    render.dot(catalog.listing_freshness("capacity")),
                ),
                data=ref,
            )
        days = catalog.event_days()
        if days:
            ref = NodeRef("activity")
            self._tree_nodes[ref] = tree.root.add(
                self._node_label("Activity", len(days)), data=ref, expand=False
            )
            for day in days:
                day_ref = NodeRef("activity", day.day.isoformat())
                self._tree_nodes[day_ref] = self._tree_nodes[ref].add_leaf(
                    self._node_label(day.day.isoformat(), day.rows), data=day_ref
                )

        target = self._tree_nodes.get(kept) or tree.root
        self._select(target)

    def _select(self, node: TreeNode) -> None:
        """Move the cursor to a node, opening the branches above it."""
        tree = self.query_one("#tree", Tree)
        parent = node.parent
        while parent is not None:
            parent.expand()
            parent = parent.parent
        tree.get_node_at_line(0)  # builds the lines, so that the node knows where it is
        tree.move_cursor(node)
        data = node.data
        if isinstance(data, NodeRef):
            self._load_node(data)

    @on(Tree.NodeHighlighted)
    def _node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if isinstance(event.node.data, NodeRef):
            if self._pending and self._pending[0] != event.node.data:
                self._pending = None  # the user went elsewhere
            self._load_node(event.node.data)

    # -- loading a node ------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="node")
    def _load_node(self, ref: NodeRef) -> None:
        worker = get_current_worker()
        catalog = self.catalog
        if catalog is None:
            return
        loaded = self._compute_node(catalog, ref)
        if not worker.is_cancelled:
            self.app.call_from_thread(self._show_node, loaded)

    @staticmethod
    def _compute_node(catalog: Catalog, ref: NodeRef) -> Loaded:
        """What the table and the detail pane show for a node (runs in a worker)."""
        if ref.kind == "root":
            entries = render.overview_entries(catalog)
            return Loaded(
                ref, "What the lake holds", entries, render.lake_subject(catalog)
            )
        if ref.kind in ("workspaces", "personal"):
            found = catalog.workspaces(personal=ref.kind == "personal")
            return Loaded(
                ref,
                f"{label('workspace', True)}: {len(found)}",
                render.workspace_entries(catalog, found),
            )
        if ref.kind == "workspace":
            workspace = catalog.workspace(ref.id)
            if workspace is None:
                return Loaded(ref, "This workspace is not in the lake any more", [])
            items = catalog.items(ref.id)
            title = f"{workspace.name}: {len(items)} item(s)"
            if not items:
                title += f"  ·  {render.empty_hint(catalog, ref.id).short}"
            return Loaded(ref, title, render.item_entries(items), workspace)
        if ref.kind == "apps":
            entries = render.app_entries(catalog)
            return Loaded(ref, f"Apps: {len(entries)}", entries)
        if ref.kind == "capacities":
            entries = render.capacity_entries(catalog)
            return Loaded(ref, f"Capacities: {len(entries)}", entries)
        if ref.kind == "activity" and not ref.id:
            entries = render.day_entries(catalog)
            return Loaded(ref, f"Days of audit events: {len(entries)}", entries)
        if ref.kind == "activity":
            day = date.fromisoformat(ref.id)
            events = catalog.events(day)
            stored = next((d for d in catalog.event_days() if d.day == day), None)
            return Loaded(
                NodeRef("day", ref.id),
                f"Events of {ref.id}: {len(events)}",
                render.event_entries(events),
                stored,
            )
        return Loaded(ref, "", [])

    def _show_node(self, loaded: Loaded) -> None:
        self._ref = (
            NodeRef("activity", loaded.ref.id)
            if loaded.ref.kind == "day"
            else loaded.ref
        )
        self._loaded = loaded
        self._entries = loaded.entries
        self._apply_table_filter()

    # -- the table -----------------------------------------------------------------------

    def _apply_table_filter(self) -> None:
        loaded = self._loaded
        if loaded is None:
            return
        table = self.query_one("#table", DataTable)
        wanted = render.filter_entries(self._entries, self._table_filter)
        shown = wanted[: render.MAX_ROWS]
        self._by_key = {entry.key: entry for entry in shown}

        table.clear(columns=True)
        table.add_columns(*COLUMNS.get(loaded.ref.kind, ()))
        for entry in shown:
            table.add_row(*entry.cells, key=entry.key)

        title = loaded.title
        if self._table_filter:
            title += f"  ·  {len(wanted)} match(es) for '{self._table_filter}'"
        if len(wanted) > len(shown):
            title += f"  ·  the first {len(shown)} are shown: narrow them with /"
        self.query_one("#table-title", Static).update(title)

        if self._pending and self._pending[0] == self._ref:
            key = self._pending[1]
            if key in self._by_key:
                table.move_cursor(row=table.get_row_index(key))
                self._show_subject(self._by_key[key].subject)
                return
        if loaded.subject is not None:
            self._show_subject(loaded.subject)
        elif shown:
            self._show_subject(shown[0].subject)
        else:
            self._show_subject(None)

    @on(DataTable.RowHighlighted, "#table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        entry = self._by_key.get(str(event.row_key.value))
        if entry is not None and self.query_one("#table", DataTable).has_focus:
            if self._pending and self._pending[1] != entry.key:
                self._pending = None  # the user picked another row
            self._show_subject(entry.subject)

    @on(DescendantFocus)
    def _focused(self, event: DescendantFocus) -> None:
        if event.widget.id == "table":
            row = self._cursor_entry()
            if row is not None:
                self._show_subject(row.subject)

    def _cursor_entry(self) -> Optional[Entry]:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return self._by_key.get(str(key)) if key is not None else None

    # -- the detail pane ------------------------------------------------------------------

    def _show_subject(self, subject: Optional[Subject]) -> None:
        self._subject = subject
        self._serial += 1
        self._render_tab(self.query_one("#detail", TabbedContent).active)

    @on(TabbedContent.TabActivated)
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._render_tab(event.pane.id or "")

    def _render_tab(self, pane_id: str) -> None:
        name = next((n for n, i in TAB_IDS.items() if i == pane_id), None)
        if name is None or self._shown.get(pane_id) == self._serial:
            return
        self._compute_tab(name, self._subject, self._serial)

    @work(thread=True, exclusive=True, group="tab")
    def _compute_tab(self, name: str, subject: Optional[Subject], serial: int) -> None:
        worker = get_current_worker()
        catalog = self.catalog
        if catalog is None:
            return
        try:
            detail = render.detail(catalog, name, subject)
        except Exception as error:  # a broken lake file must not end the UI
            detail = render.Detail(
                body=Text(f"Could not read this: {error}", style="red")
            )
        if not worker.is_cancelled:
            self.app.call_from_thread(self._apply_tab, name, serial, detail)

    def _apply_tab(self, name: str, serial: int, detail: render.Detail) -> None:
        if serial != self._serial:
            return
        self._shown[TAB_IDS[name]] = serial
        if name in TEXT_WIDGETS:
            self.query_one(TEXT_WIDGETS[name], Static).update(detail.body or "")
        else:
            note_id = "#users-note" if name == "users" else "#versions-note"
            table = self.query_one(
                "#users" if name == "users" else "#versions", DataTable
            )
            table.clear(columns=True)
            if detail.rows is None:
                self.query_one(note_id, Static).update(detail.body or "")
                return
            self.query_one(note_id, Static).update(detail.note)
            table.add_columns(*detail.columns)
            for row in detail.rows[: render.MAX_ROWS]:
                table.add_row(*row)

    def action_tab(self, name: str) -> None:
        self.query_one("#detail", TabbedContent).active = TAB_IDS[name]

    # -- filtering -----------------------------------------------------------------------

    def action_filter(self) -> None:
        tree = self.query_one("#tree", Tree)
        target = "#tree-filter" if tree.has_focus else "#table-filter"
        box = self.query_one(target, Input)
        box.display = True
        box.focus()

    def action_clear_filter(self) -> None:
        focused = self.focused
        for box_id, widget_id in (
            ("#tree-filter", "#tree"),
            ("#table-filter", "#table"),
        ):
            box = self.query_one(box_id, Input)
            if box.value or box.display:
                box.value = ""
                box.display = False
                if focused is box:
                    self.query_one(widget_id).focus()

    @on(Input.Changed, "#tree-filter")
    def _tree_filter_changed(self, event: Input.Changed) -> None:
        self._tree_filter = event.value
        self.rebuild_tree()

    @on(Input.Changed, "#table-filter")
    def _table_filter_changed(self, event: Input.Changed) -> None:
        self._table_filter = event.value
        self._apply_table_filter()

    @on(Input.Submitted, ".filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#tree" if event.input.id == "tree-filter" else "#table").focus()

    # -- going somewhere ------------------------------------------------------------------

    def goto(self, kind: str, item_id: str, workspace_id: Optional[str]) -> None:
        """Select a workspace, or an item in its workspace (for the command palette)."""
        for box_id in ("#tree-filter", "#table-filter"):
            self.query_one(box_id, Input).value = ""
        if self._tree_filter or self._table_filter:
            self._tree_filter = self._table_filter = ""
            self.rebuild_tree()
        target = item_id if kind == "workspace" else workspace_id
        if not target:
            self.notify(
                "The lake does not say which workspace it is in.", severity="warning"
            )
            return
        node = self._tree_nodes.get(NodeRef("workspace", target))
        if node is None:
            self.notify("That workspace is not in the tree.", severity="warning")
            return
        ref = NodeRef("workspace", target)
        self._pending = None if kind == "workspace" else (ref, f"{kind}:{item_id}")
        self._select(node)
        self.query_one("#tree", Tree).scroll_to_node(node)

    # -- actions on the lake --------------------------------------------------------------

    def action_reload(self) -> None:
        self.pbi.reload_catalog(announce=True)

    def refresh_choice(self) -> Optional[Tuple[str, SyncOptions]]:
        """What ``r`` would refresh: a label for it, and the sync that does it.

        With an administrator account that is a scan of a workspace, or the lists of the
        tenant. Without one (a user can scan nothing and list only what they can see) it is
        the lists of the workspaces that account can see.
        """
        available = self.pbi.backend.available_scopes()
        administrator = not available or Scope.ADMIN in available
        subject = self._subject
        workspace_id: Optional[str] = None
        if self._ref.kind == "workspace":
            workspace_id = (
                self._ref.id
            )  # whatever row is selected, it is in this workspace
        elif (
            self._ref.kind in ("workspaces", "personal")
            and isinstance(subject, Workspace)
            and self.query_one("#table", DataTable).has_focus
        ):
            workspace_id = subject.id  # the user picked a workspace from the list
        if workspace_id is not None and not administrator:
            return (
                "Fetch what you can see",
                SyncOptions(targets=("default",), force=True),
            )
        if workspace_id is not None:
            catalog = self.catalog
            workspace = catalog.workspace(workspace_id) if catalog else None
            view = catalog.scan_of(workspace_id) if catalog else None
            flags = view.flags if view else ScanFlags(lineage=True)
            name = workspace.name if workspace else workspace_id
            return (
                f"Scan {name}",
                SyncOptions(
                    targets=("scan",),
                    workspace_ids=(workspace_id,),
                    force=True,
                    scan_flags=flags,
                ),
            )
        kind = self._ref.kind
        if kind in ("root", "workspaces", "personal"):
            return (
                (
                    "Fetch the lists of the tenant"
                    if administrator
                    else "Fetch what you can see"
                ),
                SyncOptions(targets=("default",), force=True),
            )
        if kind == "apps" and not administrator:
            return ("Fetch your apps", SyncOptions(targets=("user-apps",), force=True))
        if kind == "apps":
            return ("Fetch the apps", SyncOptions(targets=("apps",), force=True))
        if kind in ("capacities", "activity") and not administrator:
            return None  # only an administrator can list those
        if kind == "capacities":
            return (
                "Fetch the capacities",
                SyncOptions(targets=("capacities",), force=True),
            )
        if kind == "activity":
            return ("Fetch the audit events", SyncOptions(targets=("activity",)))
        return None

    def action_refresh(self) -> None:
        if self.pbi.refuse_when_view_only():
            return
        choice = self.refresh_choice()
        if choice is None:
            available = self.pbi.backend.available_scopes()
            only_administrators = self._ref.kind in ("capacities", "activity")
            if only_administrators and available and Scope.ADMIN not in available:
                self.notify(
                    "Only an administrator account can fetch this: store one with "
                    "`pbi auth -t <token> -g admin` (press a).",
                    severity="warning",
                )
                return
            self.notify("There is nothing here to fetch again.", severity="warning")
            return
        title, options = choice
        self._plan_then_confirm(title, options)

    @work(thread=True, exclusive=True, group="plan")
    def _plan_then_confirm(self, title: str, options: SyncOptions) -> None:
        try:
            plan = self.pbi.backend.engine().plan(options)
        except Exception as error:
            self.app.call_from_thread(self.pbi.explain_sync_problem, error, None)
            return
        self.app.call_from_thread(self._confirm, title, options, plan)

    def _confirm(self, title: str, options: SyncOptions, plan: Plan) -> None:
        lines = [Text(title, style="bold"), Text()]
        for name, _, _, _, todo, requests in render.plan_rows(plan):
            lines.append(Text(f"{name}: {todo} to do, {requests} request(s)"))
        lines.append(Text())
        for operation, needed, quota, left, _ in render.quota_rows(plan):
            lines.append(
                Text(
                    f"{operation}: {needed} request(s) of the quota ({quota}); {left} fit now",
                    style="grey62",
                )
            )
        lines.append(Text())
        lines.extend(Text(f"· {note}") for note in render.plan_notes(plan))

        def answered(confirmed: Optional[bool]) -> None:
            if confirmed:
                self.pbi.start_sync(options, title)

        self.app.push_screen(
            ConfirmModal("Fetch from Power BI?", Group(*lines), "Fetch"), answered
        )
