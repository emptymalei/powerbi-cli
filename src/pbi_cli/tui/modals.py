"""The dialogs of the TUI: sign in, accounts, confirm, choose, open a lake, export."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, TypeVar

from rich.console import RenderableType
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
)
from textual.widgets.option_list import Option

from pbi_cli.core.auth import EXPIRY_LEEWAY
from pbi_cli.core.jwt import token_info
from pbi_cli.core.powershell import powershell_token
from pbi_cli.tui import exporting
from pbi_cli.tui.backend import AccountInfo, Backend
from pbi_cli.tui.exporting import Exportable
from pbi_cli.tui.plain import PlainLabel, PlainStatic, PlainTable

GROUPS = ("admin", "user")

ResultT = TypeVar("ResultT")


class Dialog(ModalScreen[ResultT]):
    """What every dialog is: a panel in the middle of the screen, over the screen behind it.

    The styles are those of ``Dialog`` in `pbi_cli.tui.styles`, so a dialog that is made from
    this class cannot come up unstyled in a corner of the screen.
    """


class SignInModal(Dialog[Optional[str]]):
    """Paste a fresh bearer token for a profile.

    The token is stored as ``pbi auth`` stores it. The modal closes with the group that was
    signed in, or with ``None`` when the user gives up.

    :param backend: what stores the token
    :param group: the kind of token to start with: ``admin`` or ``user``
    :param reason: why the user is asked, shown above the form
    :param profile: the profile to start with (default: the active one of the group)
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        backend: Backend,
        group: str = "admin",
        reason: str = "",
        profile: Optional[str] = None,
    ):
        super().__init__()
        self._backend = backend
        self._group = group if group in GROUPS else "admin"
        self._reason = reason
        self._profile = profile
        #: the profile the token was stored under, once it was
        self.stored_profile: Optional[str] = None

    def _profile_for(self, group: str) -> str:
        if self._profile and group == self._group:
            return self._profile
        return self._backend.active_profile(group) or "default"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield PlainLabel("Sign in", id="dialog-title")
            if self._reason:
                yield PlainStatic(self._reason, id="dialog-reason")
            yield PlainStatic(
                "Paste a fresh bearer token (the Authentication page of the documentation "
                "says how to get one) and press Enter. It is stored as `pbi auth` stores "
                "it, in the keyring, and replaces the token of the profile."
            )
            yield PlainLabel("Kind of token")
            with RadioSet(id="group"):
                for group in GROUPS:
                    yield RadioButton(
                        f"{group}"
                        + (
                            " (Power BI administrator APIs)"
                            if group == "admin"
                            else " (what a user can see)"
                        ),
                        value=group == self._group,
                        name=group,
                    )
            yield PlainLabel("Profile")
            yield Input(value=self._profile_for(self._group), id="profile")
            yield PlainLabel("Token")
            yield Input(password=True, placeholder="paste the token here", id="token")
            yield PlainStatic("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Sign in", variant="primary", id="ok", compact=True)
                yield Button("Sign in with PowerShell", id="login", compact=True)
                yield Button("Cancel", id="cancel", compact=True)

    def on_mount(self) -> None:
        self.query_one("#token", Input).focus()

    @on(Button.Pressed, "#login")
    def _login(self) -> None:
        self.query_one("#error", Static).update(
            "Finish the sign-in window that PowerShell opens ..."
        )
        self._powershell_login()

    @work(thread=True, exclusive=True, group="login")
    def _powershell_login(self) -> None:
        try:
            token = powershell_token()
        except Exception as problem:  # PBIError: no module, window closed, ...
            self.app.call_from_thread(self._login_failed, str(problem))
            return
        self.app.call_from_thread(self._login_done, token)

    def _login_failed(self, why: str) -> None:
        self.query_one("#error", Static).update(why)

    def _login_done(self, token: str) -> None:
        self.query_one("#token", Input).value = token
        self._sign_in()

    def _selected_group(self) -> str:
        pressed = self.query_one("#group", RadioSet).pressed_button
        return (pressed.name if pressed and pressed.name else None) or self._group

    @on(RadioSet.Changed, "#group")
    def _group_changed(self, event: RadioSet.Changed) -> None:
        group = event.pressed.name or self._group
        self.query_one("#profile", Input).value = self._profile_for(group)

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self._sign_in()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self._sign_in()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    def _sign_in(self) -> None:
        if self.stored_profile is not None:
            return  # Enter pressed twice, or Enter and a click: the token is stored already
        error = self.query_one("#error", Static)
        token = "".join(self.query_one("#token", Input).value.split())
        if token.lower().startswith("bearer"):
            token = token[len("bearer") :]
        profile = self.query_one("#profile", Input).value.strip()
        if not token:
            error.update("Paste the token first.")
            return
        if not profile:
            error.update("Give the profile a name.")
            return
        info = token_info(token)
        if info.expires_at is not None and info.is_expired(
            now=self._backend.clock(), leeway=EXPIRY_LEEWAY
        ):
            # storing it would only make the next request ask again
            error.update(
                f"That token expired at {info.expires_at:%Y-%m-%d %H:%M} UTC. "
                "Paste a fresh one."
            )
            return
        group = self._selected_group()
        try:
            self._backend.sign_in(token, profile, group)
        except Exception as problem:  # shown here, never with the token in it
            error.update(f"Could not store the token: {problem}")
            return
        self.stored_profile = profile
        self.dismiss(group)


class ConfirmModal(Dialog[bool]):
    """Ask before something that costs requests.

    :param title: what is asked
    :param body: what will happen, as text or a Rich renderable
    :param action: the label of the button that goes ahead
    """

    BINDINGS = [
        Binding("escape", "no", "Cancel"),
        Binding("n", "no", "Cancel", show=False),
        Binding("y", "yes", "Go ahead", show=False),
    ]

    def __init__(self, title: str, body: RenderableType, action: str = "Go ahead"):
        super().__init__()
        self._title = title
        self._body = body
        self._action = action

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield PlainLabel(self._title, id="dialog-title")
            with VerticalScroll(id="dialog-body"):
                yield PlainStatic(self._body)
            with Horizontal(id="buttons"):
                yield Button(self._action, variant="primary", id="yes", compact=True)
                yield Button("Cancel", id="no", compact=True)

    def on_mount(self) -> None:
        self.query_one("#yes", Button).focus()

    @on(Button.Pressed, "#yes")
    def action_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self) -> None:
        self.dismiss(False)


class ChoiceModal(Dialog[Optional[str]]):
    """Choose one of a few things.

    :param title: what is asked
    :param choices: ``(value, text)`` pairs
    :param note: a line under the title
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self, title: str, choices: Sequence[Tuple[str, str]], note: str = ""
    ) -> None:
        super().__init__()
        self._title = title
        self._choices: List[Tuple[str, str]] = list(choices)
        self._note = note

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield PlainLabel(self._title, id="dialog-title")
            if self._note:
                yield PlainStatic(self._note)
            yield OptionList(
                *[Option(Text(text), id=value) for value, text in self._choices],
                id="choices",
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel", compact=True)

    def on_mount(self) -> None:
        self.query_one("#choices", OptionList).focus()

    @on(OptionList.OptionSelected)
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


#: The value of the choice that stands for the work lake.
WORK_LAKE = "<work lake>"


class OpenLakeModal(Dialog[Optional[str]]):
    """Choose a lake to look at: the work lake, a recent one, or a location typed in.

    The modal closes with the location (a folder or a URL such as ``s3://bucket/folder``),
    with `WORK_LAKE` for the work lake, or with ``None`` when the user gives up.

    :param work: where the work lake is, if there is one
    :param recent: the lakes opened lately, newest first
    :param current: the lake that is open now
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self, work: Optional[str], recent: Sequence[str], current: str
    ) -> None:
        super().__init__()
        self._work = work
        self._recent = [item for item in recent if item != work]
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield PlainLabel("Open a lake", id="dialog-title")
            yield PlainStatic(
                "A lake is a folder, or a URL such as s3://bucket/folder. One that is opened "
                "here is only read: nothing is fetched into it, and no account is needed. "
                f"Open now: {self._current}"
            )
            choices = []
            if self._work:
                choices.append(Option(Text(f"Work lake   {self._work}"), id=WORK_LAKE))
            choices.extend(
                Option(Text(f"Recent      {item}"), id=item) for item in self._recent
            )
            if choices:
                yield OptionList(*choices, id="choices")
            yield PlainLabel("Or type a location")
            yield Input(
                placeholder="s3://bucket/folder or /path/to/lake", id="location"
            )
            with Horizontal(id="buttons"):
                yield Button("Open", variant="primary", id="ok", compact=True)
                yield Button("Cancel", id="cancel", compact=True)

    def on_mount(self) -> None:
        self.query_one("#location", Input).focus()

    @on(OptionList.OptionSelected)
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _typed(self) -> None:
        text = self.query_one("#location", Input).value.strip()
        if text:
            self.dismiss(text)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class AccountsModal(Dialog[Optional[Tuple[str, str, str]]]):
    """The stored profiles of both groups: make one active, or sign in again as one.

    The modal closes with ``(action, group, profile)`` where the action is ``activate`` or
    ``sign_in``, or with ``None`` when the user gives up.

    :param accounts: the profiles to list
    :param now: the current time, for how long each token lasts
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "sign_in", "New token", show=False),
    ]

    def __init__(self, accounts: Sequence[AccountInfo], now: datetime) -> None:
        super().__init__()
        self._accounts = list(accounts)
        self._now = now

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield PlainLabel("Accounts", id="dialog-title")
            yield PlainStatic(
                "The profiles stored with `pbi auth`. Enter makes the highlighted one the "
                "active profile of its group, `n` stores a new token for it. The sync uses "
                "the active profile of each group."
            )
            yield PlainTable(id="accounts", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="buttons"):
                yield Button(
                    "Make active", variant="primary", id="activate", compact=True
                )
                yield Button("New token", id="token", compact=True)
                yield Button("Close", id="cancel", compact=True)

    def on_mount(self) -> None:
        table = self.query_one("#accounts", DataTable)
        table.add_columns("Group", "Profile", "Active", "Who", "Tenant", "Token")
        for number, account in enumerate(self._accounts):
            if not account.has_token:
                token = "none stored"
            elif account.expires_at is None:
                token = "no expiry"
            else:
                left = (account.expires_at - self._now).total_seconds()
                token = "expired" if left <= 0 else f"{format_left(left)} left"
            table.add_row(
                account.group,
                account.profile,
                "yes" if account.active else "",
                account.name or "-",
                (account.tenant or "-")[:13],
                token,
                key=str(number),
            )
        table.focus()

    def _highlighted(self) -> Optional[AccountInfo]:
        table = self.query_one("#accounts", DataTable)
        if not self._accounts or table.row_count == 0:
            return None
        key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return self._accounts[int(str(key.value))]

    def _finish(self, action: str) -> None:
        account = self._highlighted()
        if account is not None:
            self.dismiss((action, account.group, account.profile))

    @on(DataTable.RowSelected)
    @on(Button.Pressed, "#activate")
    def _activate(self) -> None:
        self._finish("activate")

    @on(Button.Pressed, "#token")
    def action_sign_in(self) -> None:
        self._finish("sign_in")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


#: What the Export dialog did: wrote a file, or asked for the text to be copied.
SAVED = "saved"
COPIED = "copied"

#: The formats, in a word each (the dialog is not wide enough for more).
FORMAT_WORDS = {
    exporting.CSV: "CSV",
    exporting.XLSX: "Excel",
    exporting.JSON: "JSON",
    exporting.MARKDOWN: "Markdown",
    exporting.TSV: "TSV",
}

#: What to know about a format, shown under the formats.
FORMAT_NOTES = {
    exporting.CSV: "Opens in Excel (letters such as accents are kept).",
    exporting.XLSX: "An Excel workbook with a header row you can filter.",
    exporting.JSON: "A list of objects, one for each row.",
    exporting.MARKDOWN: "A table to paste into a document or an issue.",
    exporting.TSV: "Tab-separated text: pastes into the cells of a spreadsheet.",
}


@dataclass(frozen=True)
class Exported:
    """What the Export dialog decided.

    :param action: `SAVED` (the file was written) or `COPIED` (the text is to go on the
        clipboard)
    :param item: what is exported
    :param fmt: in which format
    :param path: the file that was written
    """

    action: str
    item: Exportable
    fmt: str
    path: Optional[Path] = None


class ExportModal(Dialog[Optional[Exported]]):
    """Choose what to take out of the screen, in which format, and where to put it.

    :param choices: what can be exported, each with where it is on the screen (``The Users
        tab``, ``The table``); the first is chosen at first
    :param folder: where a file is suggested to go
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, choices: Sequence[Tuple[str, Exportable]], folder: str) -> None:
        super().__init__()
        self._choices = list(choices)
        self._folder = folder
        self._chosen = 0
        self._format = self._choices[0][1].formats[0]
        self._suggested = ""
        self._edited = False
        self._replace: Optional[Path] = (
            None  # an existing file that the person was told of
        )
        self._done = False

    @property
    def item(self) -> Exportable:
        return self._choices[self._chosen][1]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield PlainLabel("Export what is shown", id="dialog-title")
            if len(self._choices) > 1:
                yield PlainLabel("What")
                with RadioSet(id="what"):
                    for index, (where, item) in enumerate(self._choices):
                        yield RadioButton(
                            f"{where}: {item.describe()}",
                            value=index == 0,
                            name=str(index),
                        )
            else:
                where, item = self._choices[0]
                yield PlainStatic(f"{where}: {item.describe()}", id="what-one")
            yield PlainLabel("Format")
            with Vertical(id="format-box"):
                yield self._format_buttons()
            yield PlainStatic(FORMAT_NOTES[self._format], id="format-note")
            yield PlainLabel("File")
            yield Input(id="path")
            yield PlainStatic("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Save", variant="primary", id="save", compact=True)
                yield Button("Copy", id="copy", compact=True)
                yield Button("Cancel", id="cancel", compact=True)

    def on_mount(self) -> None:
        self._suggest()
        self.query_one("#path", Input).focus()

    # -- what is offered -----------------------------------------------------------------

    def _format_buttons(self) -> RadioSet:
        """The formats that the chosen export can have, to pick from. A set is made anew for
        each export: a button that a handler of this very set presses is not heard by the set
        (Textual keeps such changes from the handler that they cause), so it would stay
        pressed beside the new one."""
        formats = self.item.formats
        if self._format not in formats:
            self._format = formats[0]
        return RadioSet(
            *[
                RadioButton(FORMAT_WORDS[fmt], value=fmt == self._format, name=fmt)
                for fmt in formats
            ],
            id="format",
        )

    def _suggest(self) -> None:
        """Suggest a file that does not exist yet, in the folder; a name the person typed is
        kept, with the extension of the format."""
        field = self.query_one("#path", Input)
        extension = exporting.EXTENSIONS[self._format]
        if self._edited and field.value:
            known = set(exporting.EXTENSIONS.values())
            path = Path(field.value)
            if path.suffix.lower() in known and path.suffix.lower() != extension:
                field.value = str(path.with_suffix(extension))
            return
        self._suggested = str(
            exporting.free_path(
                self._folder, exporting.file_name(self.item, self._format)
            )
        )
        field.value = self._suggested

    @on(RadioSet.Changed, "#what")
    async def _what_changed(self, event: RadioSet.Changed) -> None:
        self._chosen = int(event.pressed.name or 0)
        box = self.query_one("#format-box", Vertical)
        await box.remove_children()
        await box.mount(self._format_buttons())
        self.query_one("#format-note", Static).update(FORMAT_NOTES[self._format])
        self._suggest()

    @on(RadioSet.Changed, "#format")
    def _format_changed(self, event: RadioSet.Changed) -> None:
        picked = event.pressed.name or self._format
        if picked not in self.item.formats or picked == self._format:
            return
        self._format = picked
        self.query_one("#format-note", Static).update(FORMAT_NOTES[picked])
        self._suggest()

    @on(Input.Changed, "#path")
    def _path_changed(self, event: Input.Changed) -> None:
        self._edited = event.value != self._suggested
        self.query_one("#error", Static).update("")

    # -- what is done --------------------------------------------------------------------

    def _say(self, text: str) -> None:
        self.query_one("#error", Static).update(text)

    def _finish(self, result: Optional[Exported]) -> None:
        if not self._done:  # a dialog may be answered twice (Enter, then the button)
            self._done = True
            self.dismiss(result)

    @on(Input.Submitted, "#path")
    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        typed = self.query_one("#path", Input).value.strip().strip('"')
        if not typed:
            self._say("Give the file a name.")
            return
        path = Path(typed).expanduser()
        if path.is_dir():
            self._say("That is a folder: add the name of the file to it.")
            return
        if path.exists() and self._replace != path:
            self._replace = path
            self._say(
                "That file exists already: press Save again to replace it, or choose "
                "another name."
            )
            return
        try:
            written = exporting.write(self.item, self._format, path)
        except (OSError, ValueError) as error:
            self._say(f"Could not write the file: {error}")
            return
        self._finish(Exported(SAVED, self.item, self._format, written))

    @on(Button.Pressed, "#copy")
    def _copy(self) -> None:
        fmt = self._format if self._format != exporting.XLSX else exporting.TSV
        self._finish(Exported(COPIED, self.item, fmt))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self._finish(None)


def format_left(seconds: float) -> str:
    """How long a token lasts, in the largest unit that keeps it short."""
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{max(minutes, 1)} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} d"
