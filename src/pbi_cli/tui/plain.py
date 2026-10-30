"""Widgets that show a ``str`` as the text it is.

Textual reads a ``str`` that it is given to show as *markup*: in ``[Confidential]Sales`` the
bracket is a style tag, so what is shown is ``Sales``, and ``Sales [/Q4]`` is a closing tag
that matches nothing, which ends the whole app with a ``MarkupError``. The names in Power BI
are free text, and many start with such a tag, so the screens of the TUI show everything
through these widgets, and tell the app not to read the notifications as markup either
(`PBIApp.notify`). Nothing in the TUI is written in markup: styled text is a `rich.text.Text`.
"""

from typing import Any, Optional

from rich.text import Text
from textual.widgets import DataTable, Label, Static
from textual.widgets.data_table import RowKey


class PlainStatic(Static):
    """A `Static` whose ``str`` content is shown as it is, not read as markup."""

    def __init__(self, content: Any = "", *, markup: bool = False, **kwargs: Any):
        super().__init__(content, markup=markup, **kwargs)


class PlainLabel(Label):
    """A `Label` whose ``str`` content is shown as it is, not read as markup."""

    def __init__(self, content: Any = "", *, markup: bool = False, **kwargs: Any):
        super().__init__(content, markup=markup, **kwargs)


def plain_cell(cell: Any) -> Any:
    """What a table shows for a cell: a ``str`` as the text it is, anything else as it is."""
    return Text(cell) if isinstance(cell, str) else cell


class PlainTable(DataTable):
    """A `DataTable` whose ``str`` cells are shown as they are, not read as markup."""

    def add_row(
        self,
        *cells: Any,
        height: Optional[int] = 1,
        key: Optional[str] = None,
        label: Any = None,
    ) -> RowKey:
        return super().add_row(
            *[plain_cell(cell) for cell in cells], height=height, key=key, label=label
        )
