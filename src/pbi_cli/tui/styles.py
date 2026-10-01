"""The look of the TUI: a theme with the Power BI yellow, and the layout rules."""

from textual.theme import Theme

THEME = Theme(
    name="powerbi",
    primary="#F2C811",
    secondary="#117865",
    accent="#E66C37",
    warning="#F2C811",
    error="#D64550",
    success="#4CAF50",
    foreground="#E6E6E6",
    background="#17171B",
    surface="#202026",
    panel="#2A2A32",
    dark=True,
)

CSS = """
Screen {
    background: $background;
}

StatusBar {
    height: 1;
    background: $panel;
}
StatusBar #who {
    width: 1fr;
}
StatusBar #busy {
    width: auto;
}

#explorer, #sync {
    height: 1fr;
}

/* the Explorer */
#tree-pane {
    width: 36;
    border-right: tall $panel;
}
#tree {
    background: $background;
}
#right {
    width: 1fr;
}
#table-pane {
    height: 45%;
    border-bottom: tall $panel;
}
#table-title {
    height: 1;
    padding: 0 1;
    color: $text-muted;
}
#detail {
    height: 1fr;
}
#detail VerticalScroll, #sync-tabs VerticalScroll {
    padding: 0 1;
}
#info, #lineage, #json, #users-note, #versions-note, #plan-head, #plan-notes, #lake-lines {
    width: 100%;
    padding: 0 1;
}
#users, #versions {
    height: 1fr;
}
.filter {
    display: none;
    height: 3;
}
DataTable {
    background: $background;
}

/* the Sync screen */
#sync-left {
    width: 52;
    padding: 0 1;
    border-right: tall $panel;
}
#sync-right {
    width: 1fr;
}
#targets {
    height: auto;
    max-height: 16;
    border: none;
    padding: 0;
    background: $background;
}
#targets-note {
    height: auto;
    min-height: 3;
    padding: 0 1;
    color: $text-muted;
}
.heading {
    margin-top: 1;
    text-style: bold;
    color: $primary;
}
.number {
    height: 3;
    margin-top: 1;
}
.number Label {
    width: auto;
    padding: 1 1 0 0;
}
.number Input {
    width: 8;
    margin-right: 2;
}
Checkbox {
    background: $background;
    border: none;
    padding: 0;
    height: 1;
}
#sync-buttons {
    height: 3;
    padding: 0 1;
}
#sync-buttons Button {
    margin-right: 2;
}
#run-line {
    width: 1fr;
    padding: 1 0 0 0;
    color: $text-muted;
}
#plan, #quota, #holdings, #used {
    height: auto;
    max-height: 14;
}
#progress {
    padding: 0 1;
}
#log-scroll {
    height: 1fr;
    background: $background;
}
#log {
    width: 100%;
    padding: 0 1;
}

/* dialogs */
SignInModal, ConfirmModal, ChoiceModal {
    align: center middle;
    background: $background 70%;
}
#dialog {
    width: 82;
    height: auto;
    max-height: 90%;
    background: $panel;
    border: thick $primary;
    padding: 1 2;
}
#dialog-title {
    text-style: bold;
    color: $primary;
    margin-bottom: 1;
}
#dialog-reason {
    color: $warning;
    margin-bottom: 1;
}
#dialog-body {
    height: auto;
    max-height: 24;
}
#dialog Label {
    margin-top: 1;
}
#dialog-title {
    margin-top: 0;
}
#error {
    color: $error;
    height: auto;
}
#buttons {
    height: 5;
    margin-top: 1;
    align-horizontal: right;
}
#buttons Button {
    margin-left: 2;
}
#choices {
    height: auto;
    max-height: 12;
}
"""
