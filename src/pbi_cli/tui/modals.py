"""The dialogs of the TUI: sign in, confirm, choose, open a lake."""

from typing import List, Optional, Sequence, Tuple, TypeVar

from rich.console import RenderableType
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
)
from textual.widgets.option_list import Option

from pbi_cli.tui.backend import Backend

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
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, backend: Backend, group: str = "admin", reason: str = ""):
        super().__init__()
        self._backend = backend
        self._group = group if group in GROUPS else "admin"
        self._reason = reason

    def _profile_for(self, group: str) -> str:
        return self._backend.active_profile(group) or "default"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Sign in", id="dialog-title")
            if self._reason:
                yield Static(self._reason, id="dialog-reason")
            yield Static(
                "Paste a fresh bearer token (the Authentication page of the documentation "
                "says how to get one) and press Enter. It is stored as `pbi auth` stores "
                "it, in the keyring, and replaces the token of the profile."
            )
            yield Label("Kind of token")
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
            yield Label("Profile")
            yield Input(value=self._profile_for(self._group), id="profile")
            yield Label("Token")
            yield Input(password=True, placeholder="paste the token here", id="token")
            yield Static("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Sign in", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#token", Input).focus()

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
        group = self._selected_group()
        try:
            self._backend.sign_in(token, profile, group)
        except Exception as problem:  # shown here, never with the token in it
            error.update(f"Could not store the token: {problem}")
            return
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
            yield Label(self._title, id="dialog-title")
            with VerticalScroll(id="dialog-body"):
                yield Static(self._body)
            with Horizontal(id="buttons"):
                yield Button(self._action, variant="primary", id="yes")
                yield Button("Cancel", id="no")

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
            yield Label(self._title, id="dialog-title")
            if self._note:
                yield Static(self._note)
            yield OptionList(
                *[Option(text, id=value) for value, text in self._choices], id="choices"
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")

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
            yield Label("Open a lake", id="dialog-title")
            yield Static(
                "A lake is a folder, or a URL such as s3://bucket/folder. One that is opened "
                "here is only read: nothing is fetched into it, and no account is needed. "
                f"Open now: {self._current}"
            )
            choices = []
            if self._work:
                choices.append(Option(f"Work lake   {self._work}", id=WORK_LAKE))
            choices.extend(
                Option(f"Recent      {item}", id=item) for item in self._recent
            )
            if choices:
                yield OptionList(*choices, id="choices")
            yield Label("Or type a location")
            yield Input(
                placeholder="s3://bucket/folder or /path/to/lake", id="location"
            )
            with Horizontal(id="buttons"):
                yield Button("Open", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

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
