"""Where a table gets its data: the sources named in a Power Query (M) expression.

A scan with ``datasetExpressions`` keeps, for each table of a dataset, the expression that
loads it. Most of what a person wants to know about where a report's data comes from is in it:
the server and database of a SQL source, the file or the URL of a workbook, the SharePoint
site, the query that is sent to the database. This module reads those out of the text:

```python
found = extract_sources('let Source = Sql.Database("db.example", "Sales") in Source')
found[0].kind, found[0].server, found[0].database   # ("SQL Server", "db.example", "Sales")
```

It is not an M parser. It reads the tokens, finds the calls of the connectors it knows (the
table `CONNECTORS`), reads their arguments, follows a name that stands for a text (a step of the
query, or a parameter of the dataset) when it can, and reports what it could not follow. It
never raises: an expression that it cannot read has no sources.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

#: What is cut from a native query when it is shown in a line.
QUERY_SHOWN = 160

#: How many steps of names are followed to find the text one stands for.
MAX_FOLLOWED = 8


@dataclass(frozen=True)
class Connector:
    """A function of M that reads from somewhere.

    :param kind: what it reads, in words
    :param roles: what its first arguments are, in order: ``server``, ``database``,
        ``location`` (a file or a URL) or ``-`` for one that says nothing we show
    :param wraps: it reads a file or a URL that its first argument names (``Excel.Workbook``
        of ``File.Contents(...)``), instead of connecting by itself
    :param location: it is what names a file or a URL (``File.Contents``, ``Web.Contents``)
    """

    kind: str
    roles: Tuple[str, ...] = ()
    wraps: bool = False
    location: bool = False


CONNECTORS: Dict[str, Connector] = {
    # databases
    "Sql.Database": Connector("SQL Server", ("server", "database")),
    "Sql.Databases": Connector("SQL Server", ("server",)),
    "AzureSynapse.Database": Connector("Azure Synapse", ("server", "database")),
    "Oracle.Database": Connector("Oracle", ("server",)),
    "PostgreSQL.Database": Connector("PostgreSQL", ("server", "database")),
    "MySQL.Database": Connector("MySQL", ("server", "database")),
    "Teradata.Database": Connector("Teradata", ("server",)),
    "SapHana.Database": Connector("SAP HANA", ("server",)),
    "SapBusinessWarehouse.Cubes": Connector("SAP BW", ("server",)),
    "Snowflake.Databases": Connector("Snowflake", ("server", "database")),
    "Databricks.Catalogs": Connector("Databricks", ("server", "location")),
    "Databricks.Contents": Connector("Databricks", ("server", "location")),
    "GoogleBigQuery.Database": Connector("Google BigQuery"),
    "AmazonRedshift.Database": Connector("Amazon Redshift", ("server", "database")),
    "AnalysisServices.Database": Connector("Analysis Services", ("server", "database")),
    "AnalysisServices.Databases": Connector("Analysis Services", ("server",)),
    "AzureDataExplorer.Contents": Connector(
        "Azure Data Explorer", ("server", "database")
    ),
    "Kusto.Contents": Connector("Azure Data Explorer", ("server", "database")),
    "Odbc.DataSource": Connector("ODBC", ("location",)),
    "Odbc.Query": Connector("ODBC", ("location",)),
    "OleDb.DataSource": Connector("OLE DB", ("location",)),
    "OleDb.Query": Connector("OLE DB", ("location",)),
    "Access.Database": Connector("Access", wraps=True),
    # services
    "Salesforce.Data": Connector("Salesforce"),
    "Salesforce.Reports": Connector("Salesforce reports"),
    "OData.Feed": Connector("OData", ("location",)),
    "SharePoint.Files": Connector("SharePoint files", ("location",)),
    "SharePoint.Contents": Connector("SharePoint", ("location",)),
    "SharePoint.Tables": Connector("SharePoint list", ("location",)),
    "AzureStorage.Blobs": Connector("Azure Blob Storage", ("location",)),
    "AzureStorage.DataLake": Connector("Azure Data Lake Storage", ("location",)),
    "AzureStorage.Tables": Connector("Azure Table Storage", ("location",)),
    "Lakehouse.Contents": Connector("Fabric lakehouse"),
    "PowerPlatform.Dataflows": Connector("Power Platform dataflows"),
    "PowerBI.Dataflows": Connector("Power BI dataflows"),
    "Dataverse.Contents": Connector("Dataverse", ("location",)),
    "Cds.Entities": Connector("Dataverse", ("location",)),
    "Web.BrowserContents": Connector("Web page", ("location",)),
    "Web.Page": Connector("Web page", wraps=True),
    # files and URLs (what the readers of a format are given)
    "File.Contents": Connector("File", ("location",), location=True),
    "Web.Contents": Connector("Web", ("location",), location=True),
    "Folder.Files": Connector("Folder", ("location",)),
    "Folder.Contents": Connector("Folder", ("location",)),
    "Excel.Workbook": Connector("Excel workbook", wraps=True),
    "Excel.CurrentWorkbook": Connector("Excel workbook of the file"),
    "Csv.Document": Connector("CSV file", wraps=True),
    "Json.Document": Connector("JSON", wraps=True),
    "Xml.Tables": Connector("XML", wraps=True),
    "Xml.Document": Connector("XML", wraps=True),
    "Pdf.Tables": Connector("PDF", wraps=True),
    "Parquet.Document": Connector("Parquet", wraps=True),
    "Lines.FromBinary": Connector("Text file", wraps=True),
}

_TOKEN = re.compile(
    r"""
      (?P<comment>//[^\n]*|/\*.*?\*/)
    | (?P<quoted>\#"(?:[^"]|"")*")
    | (?P<string>"(?:[^"]|"")*")
    | (?P<ident>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<space>\s+)
    | (?P<punct>=>|[()\[\]{},=;])
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = set(_OPEN.values())

#: What M writes for a line break or a tab inside a text: ``#(lf)``, ``#(cr,lf)``, ``#(tab)``.
_ESCAPES = re.compile(r"#\((?:cr|lf|tab)(?:\s*,\s*(?:cr|lf|tab))*\)")

_FROM = re.compile(
    r"""\b(?:from|join)\s+
        (?P<name>(?:\[[^\]]+\]|"[^"]+"|`[^`]+`|[A-Za-z_][\w$#]*)
                 (?:\s*\.\s*(?:\[[^\]]+\]|"[^"]+"|`[^`]+`|[A-Za-z_][\w$#]*))*)""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class Token:
    """One piece of an expression: ``string`` (its text without quotes), ``quoted`` (a name
    written ``#"like this"``), ``ident``, ``number`` or ``punct``/``other``."""

    kind: str
    text: str

    @property
    def is_name(self) -> bool:
        return self.kind in ("ident", "quoted")


@dataclass(frozen=True)
class Source:
    """A place a table reads from, as its query says.

    :param connector: the function of M that reads it (``Sql.Database``)
    :param kind: what it is, in words (``SQL Server``)
    :param server: the server, host or account
    :param database: the database or warehouse
    :param location: a file or a URL
    :param item: the table, view, sheet or list that is taken from it (``dbo.Sales``)
    :param query: a native query that is sent to it
    :param tables: the tables that query reads from, as far as its text says
    :param unresolved: names of parameters or steps that stand for text this reading did not
        find (``ServerName``)
    """

    connector: str
    kind: str
    server: str = ""
    database: str = ""
    location: str = ""
    item: str = ""
    query: str = ""
    tables: Tuple[str, ...] = ()
    unresolved: Tuple[str, ...] = ()

    @property
    def where(self) -> str:
        """The place, in a few words: ``server / database``, a file or a URL."""
        parts = [p for p in (self.server, self.database) if p]
        if parts and self.location:
            return f"{' / '.join(parts)}  {self.location}"
        return " / ".join(parts) or self.location

    @property
    def key(self) -> Tuple[str, str, str, str]:
        """What makes two sources the same place (the table taken from it does not)."""
        return (self.kind, self.server, self.database, self.location)

    def describe(self) -> str:
        """The source in a line: ``SQL Server  sql.example / Sales  dbo.Sales``."""
        found = [self.kind, self.where, self.item]
        line = "  ".join(part for part in found if part)
        if self.unresolved:
            line += f"  (not known: {', '.join(self.unresolved)})"
        return line


@dataclass
class _Call:
    """A call of a connector found in an expression."""

    name: str
    start: int  # index of the name in the tokens
    end: int  # index after the closing bracket
    args: List[List[Token]] = field(default_factory=list)


def tokenize(expression: str) -> List[Token]:
    """The tokens of an expression without its blanks and comments."""
    tokens = []
    for found in _TOKEN.finditer(expression):
        kind, text = found.lastgroup or "other", found.group()
        if kind in ("space", "comment"):
            continue
        if kind == "string":
            text = text[1:-1].replace('""', '"')
        elif kind == "quoted":
            text = text[2:-1].replace('""', '"')
        tokens.append(Token(kind, text))
    return tokens


def _matching(tokens: Sequence[Token], start: int) -> int:
    """The index of the bracket that closes the one at ``start`` (the last token when it is
    never closed)."""
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token.kind == "punct" and token.text in _OPEN:
            depth += 1
        elif token.kind == "punct" and token.text in _CLOSE:
            depth -= 1
            if depth == 0:
                return index
    return len(tokens) - 1


def _split(tokens: Sequence[Token]) -> List[List[Token]]:
    """Tokens cut at the commas that are not inside a bracket."""
    parts: List[List[Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.kind == "punct" and token.text in _OPEN:
            depth += 1
        elif token.kind == "punct" and token.text in _CLOSE:
            depth -= 1
        if depth == 0 and token.kind == "punct" and token.text == ",":
            parts.append([])
        else:
            parts[-1].append(token)
    return [p for p in parts if p]


def _calls(tokens: Sequence[Token]) -> Iterator[_Call]:
    """The calls of the connectors that the tokens hold, in order (calls inside the arguments
    of another follow it)."""
    for index, token in enumerate(tokens):
        if (
            token.kind == "ident"
            and token.text in CONNECTORS
            and index + 1 < len(tokens)
            and tokens[index + 1].kind == "punct"
            and tokens[index + 1].text == "("
        ):
            close = _matching(tokens, index + 1)
            yield _Call(
                token.text,
                index,
                close + 1,
                _split(tokens[index + 2 : close]),
            )


def _steps(tokens: Sequence[Token]) -> Dict[str, List[Token]]:
    """The steps of the first ``let`` of an expression, by name, with their tokens.

    A name that is defined twice keeps the last. An expression that is not a ``let`` has
    no steps.
    """
    steps: Dict[str, List[Token]] = {}
    start = next(
        (i for i, t in enumerate(tokens) if t.kind == "ident" and t.text == "let"), None
    )
    if start is None:
        return steps
    depth, lets = 0, 1
    end = len(tokens)
    for index in range(start + 1, len(tokens)):
        token = tokens[index]
        if token.kind == "punct" and token.text in _OPEN:
            depth += 1
        elif token.kind == "punct" and token.text in _CLOSE:
            depth -= 1
        elif token.kind == "ident" and token.text == "let":
            lets += 1
        elif token.kind == "ident" and token.text == "in":
            lets -= 1
            if lets == 0 and depth == 0:
                end = index
                break
    for part in _split(tokens[start + 1 : end]):
        if len(part) >= 3 and part[0].is_name and part[1].text == "=":
            steps[part[0].text] = part[2:]
    return steps


_LITERALS = ("null", "true", "false")


def _operands(tokens: Sequence[Token]) -> List[List[Token]]:
    """Tokens cut at the ``&`` that are not inside a bracket."""
    parts: List[List[Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.kind == "punct" and token.text in _OPEN:
            depth += 1
        elif token.kind == "punct" and token.text in _CLOSE:
            depth -= 1
        if depth == 0 and token.kind == "other" and token.text == "&":
            parts.append([])
        else:
            parts[-1].append(token)
    return [p for p in parts if p]


def _text(
    tokens: Sequence[Token],
    steps: Mapping[str, Sequence[Token]],
    names: Mapping[str, str],
    unresolved: List[str],
    depth: int = 0,
) -> str:
    """The text an argument stands for: a string, a name that stands for one (a step of the
    query, or a parameter of the dataset), or such pieces joined with ``&``. What it cannot
    follow is added to ``unresolved``, and shown as ``{Name}`` when it is part of a longer
    text."""
    if not tokens or depth > MAX_FOLLOWED:
        return ""
    for index, token in enumerate(tokens):  # a parameter is `"text" meta [...]`
        if token.kind == "ident" and token.text == "meta":
            tokens = tokens[:index]
            break
    operands = _operands(tokens)
    pieces: List[str] = []
    for operand in operands:
        first = operand[0]
        if first.kind == "string" and len(operand) == 1:
            pieces.append(first.text)
        elif first.is_name and len(operand) == 1 and first.text not in _LITERALS:
            name = first.text
            if name in steps and steps[name] and steps[name][0] is not first:
                pieces.append(_text(steps[name], steps, names, unresolved, depth + 1))
            elif name in names:
                pieces.append(names[name])
            else:
                if name not in unresolved:
                    unresolved.append(name)
                pieces.append("" if len(operands) == 1 else "{" + name + "}")
    return "".join(pieces)


def _record(tokens: Sequence[Token]) -> Dict[str, str]:
    """The text values of a record ``[Key = "text", Other = 1]`` (the others are left out)."""
    found: Dict[str, str] = {}
    if not tokens or tokens[0].text != "[":
        return found
    inner = tokens[1 : _matching(tokens, 0)]
    for part in _split(inner):
        if len(part) >= 3 and part[0].is_name and part[1].text == "=":
            if part[2].kind == "string":
                found[part[0].text] = part[2].text
    return found


def _navigations(tokens: Sequence[Token]) -> List[Tuple[str, Dict[str, str]]]:
    """The places where a step is navigated: ``Source{[Schema="dbo", Item="Sales"]}[Data]``,
    as the name of the step and the record."""
    found = []
    for index, token in enumerate(tokens):
        if (
            token.is_name
            and index + 2 < len(tokens)
            and tokens[index + 1].text == "{"
            and tokens[index + 2].text == "["
        ):
            close = _matching(tokens, index + 1)
            record = _record(tokens[index + 2 : close])
            if record:
                found.append((token.text, record))
    return found


def _item(record: Mapping[str, str]) -> str:
    """The table, view, sheet or list that a navigation names."""
    name = record.get("Item") or record.get("Name") or record.get("Id") or ""
    schema = record.get("Schema", "")
    return f"{schema}.{name}" if schema and name else name


def _native_tables(query: str) -> Tuple[str, ...]:
    """The names after FROM and JOIN in the text of a native query, each once."""
    found: List[str] = []
    for hit in _FROM.finditer(_ESCAPES.sub(" ", query)):
        name = re.sub(r"\s+", "", hit.group("name"))
        name = re.sub(r"[\[\]`\"]", "", name)
        if name and name.lower() not in ("select", "lateral") and name not in found:
            found.append(name)
    return tuple(found)


def _shown(query: str) -> str:
    one_line = re.sub(r"\s+", " ", _ESCAPES.sub(" ", query)).strip()
    if len(one_line) > QUERY_SHOWN:
        return one_line[: QUERY_SHOWN - 1] + "…"
    return one_line


def _build(
    call: _Call,
    steps: Mapping[str, Sequence[Token]],
    names: Mapping[str, str],
    inside: Optional[_Call],
) -> Source:
    """What one call of a connector says about where the data is."""
    connector = CONNECTORS[call.name]
    unresolved: List[str] = []
    server = database = location = query = ""
    roles = list(connector.roles)
    for position, tokens in enumerate(call.args):
        if tokens and tokens[0].text == "[":
            record = _record(tokens)
            query = record.get("Query") or record.get("CommandText") or query
            database = record.get("Database") or database
            continue
        role = roles[position] if position < len(roles) else "-"
        if role == "-":
            continue
        value = _text(tokens, steps, names, unresolved)
        if role == "server":
            server = value
        elif role == "database":
            database = value
        elif role == "location":
            location = value
    if inside is not None:  # the file or the address that this format is read from
        inner = _build(inside, steps, names, None)
        location, server = inner.location or location, inner.server or server
        unresolved.extend(n for n in inner.unresolved if n not in unresolved)
    elif (
        connector.wraps and call.args
    ):  # a name that stands for the file or the address
        location = _text(call.args[0], steps, names, unresolved) or location
    return Source(
        connector=call.name,
        kind=connector.kind,
        server=server,
        database=database,
        location=location,
        query=_shown(query) if query else "",
        tables=_native_tables(query) if query else (),
        unresolved=tuple(unresolved),
    )


def _native_query(tokens: Sequence[Token], steps, names) -> str:
    """The text of a ``Value.NativeQuery(Source, "select ...")`` call, if there is one."""
    for index, token in enumerate(tokens):
        if token.kind == "ident" and token.text in ("Value.NativeQuery", "Odbc.Query"):
            if index + 1 < len(tokens) and tokens[index + 1].text == "(":
                close = _matching(tokens, index + 1)
                args = _split(tokens[index + 2 : close])
                if len(args) >= 2:
                    return _text(args[1], steps, names, [])
    return ""


def extract_sources(
    expression: str, parameters: Optional[Mapping[str, str]] = None
) -> List[Source]:
    """The sources that a Power Query expression reads from, in the order it names them.

    :param expression: the M expression of a table (or a shared query) of a dataset
    :param parameters: the texts that names of the dataset stand for (its parameters and
        shared queries that are one text), to resolve ``ServerName`` in
        ``Sql.Database(ServerName, "db")``
    :return: the sources, each place once with the item that is taken from it (the table of a
        database, the sheet of a workbook); empty when the expression names none that this
        module knows or cannot be read
    """
    try:
        return _extract(expression, parameters or {})
    except (
        IndexError,
        KeyError,
        RecursionError,
        ValueError,
    ):  # never the reader's problem
        return []


def _extract(expression: str, parameters: Mapping[str, str]) -> List[Source]:
    tokens = tokenize(expression)
    steps = _steps(tokens)
    names: Dict[str, str] = dict(parameters)
    calls = list(_calls(tokens))
    # the call that opens the file or the address belongs to the reader of the format
    consumed: set = set()
    inside_of: Dict[int, _Call] = {}
    for call in calls:
        if not CONNECTORS[call.name].wraps:
            continue
        inside = next(
            (
                c
                for c in calls
                if c is not call
                and call.start < c.start < call.end
                and CONNECTORS[c.name].location
            ),
            None,
        )
        first = call.args[0] if call.args else []
        if inside is None and len(first) == 1 and first[0].is_name:
            # a name that stands for the step that opens the file
            inside = next(
                (
                    c
                    for c in calls
                    if c is not call
                    and CONNECTORS[c.name].location
                    and _step_of(c, tokens, steps) == first[0].text
                ),
                None,
            )
        if inside is not None:
            inside_of[id(call)] = inside
            consumed.add(id(inside))
    built: List[Tuple[_Call, Source]] = []
    for call in calls:
        if id(call) not in consumed:
            built.append((call, _build(call, steps, names, inside_of.get(id(call)))))

    native = _native_query(tokens, steps, names)
    navigations = _navigations(tokens)
    sources: List[Source] = []
    for call, source in built:
        step = _step_of(call, tokens, steps)
        item = ""
        if len(built) == 1:  # the path down to the table: database, schema, table
            item = " / ".join(i for i in (_item(r) for _, r in navigations) if i)
        else:
            for name, record in navigations:
                if name == step:
                    item = _item(record)
                    break
        extra = {}
        if item:
            extra["item"] = item
        if native and not source.query:
            extra["query"] = _shown(native)
            extra["tables"] = _native_tables(native)
        sources.append(replace_source(source, **extra) if extra else source)
    return _once(sources)


def replace_source(source: Source, **changes: object) -> Source:
    """A source with some fields changed."""
    values = {name: getattr(source, name) for name in source.__dataclass_fields__}
    values.update(changes)
    return Source(**values)  # type: ignore[arg-type]


def _step_of(
    call: _Call, tokens: Sequence[Token], steps: Mapping[str, Sequence[Token]]
) -> str:
    """The name of the step of the ``let`` that a call is in (empty for none)."""
    for name, step_tokens in steps.items():
        if not step_tokens:
            continue
        first = step_tokens[0]
        # the tokens of a step are a slice of the tokens: find where it starts
        for index, token in enumerate(tokens):
            if token is first:
                if index <= call.start < index + len(step_tokens):
                    return name
                break
    return ""


def _once(sources: Sequence[Source]) -> List[Source]:
    """The sources without the same one twice (the same place and the same item)."""
    found: List[Source] = []
    for source in sources:
        if source not in found:
            found.append(source)
    return found


def language_of(expression: str) -> str:
    """What an expression of a table is written in: ``M`` (Power Query), ``DAX`` (a
    calculated table) or an empty text when there is none."""
    text = expression.strip()
    if not text:
        return ""
    tokens = tokenize(text)
    if any(t.kind == "ident" and t.text in ("let", "section") for t in tokens[:3]):
        return "M"
    if any(t.kind == "ident" and t.text in CONNECTORS for t in tokens):
        return "M"
    return "DAX"


def parameter_values(queries: Sequence[Mapping[str, object]]) -> Dict[str, str]:
    """The texts that the shared queries (``expressions`` of a dataset) stand for.

    :param queries: the shared queries as the scan has them (``name`` and ``expression``)
    :return: for each one that is a text, or a text with ``meta [...]`` after it (a parameter),
        its text, by name
    """
    found: Dict[str, str] = {}
    for query in queries:
        name = str(query.get("name") or "")
        tokens = tokenize(str(query.get("expression") or ""))
        if name and tokens and tokens[0].kind == "string":
            if len(tokens) == 1 or tokens[1].text in ("meta", "&"):
                found[name] = tokens[0].text
    return found
