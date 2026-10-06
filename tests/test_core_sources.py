"""Where a table gets its data: the sources named in a Power Query (M) expression."""

import pytest

from pbi_cli.core.sources import (
    CONNECTORS,
    Source,
    extract_sources,
    language_of,
    parameter_values,
    tokenize,
)


def only(expression, parameters=None) -> Source:
    found = extract_sources(expression, parameters)
    assert len(found) == 1, found
    return found[0]


# ---------------------------------------------------------------------------
# databases
# ---------------------------------------------------------------------------


def test_a_sql_server_table_has_its_server_database_and_table():
    source = only(
        """let
            Source = Sql.Database("myserver.database.windows.net", "SalesDB"),
            dbo_Sales = Source{[Schema="dbo",Item="Sales"]}[Data]
        in
            dbo_Sales"""
    )

    assert source == Source(
        connector="Sql.Database",
        kind="SQL Server",
        server="myserver.database.windows.net",
        database="SalesDB",
        item="dbo.Sales",
    )
    assert source.where == "myserver.database.windows.net / SalesDB"
    assert source.describe() == (
        "SQL Server  myserver.database.windows.net / SalesDB  dbo.Sales"
    )


def test_the_query_that_is_sent_to_the_database_and_the_tables_it_reads_are_found():
    source = only(
        'let Source = Sql.Database("srv", "db", [Query="select a.id, b.name\\n'
        'from dbo.Orders a join [dbo].[Customers] b on a.cid = b.id where x = 1"]) in Source'.replace(
            "\\n", "\n"
        )
    )

    assert source.query.startswith("select a.id, b.name from dbo.Orders a join")
    assert source.tables == ("dbo.Orders", "dbo.Customers")


def test_a_native_query_of_a_step_is_the_query_of_the_source():
    source = only(
        """let Source = Sql.Database("srv", "db"),
            Q = Value.NativeQuery(Source, "SELECT * FROM [sales].[Fact] f
                 INNER JOIN dim.Date d ON 1=1", null, [EnableFolding=true])
        in Q"""
    )

    assert source.tables == ("sales.Fact", "dim.Date")
    assert "INNER JOIN" in source.query and "\n" not in source.query


def test_the_line_breaks_and_tabs_that_m_writes_in_a_text_are_blanks_in_the_query():
    source = only(
        'let S = Sql.Database("srv", "db", [Query="select a.id#(lf)from dbo.Orders a'
        '#(cr,lf)join dbo.Customers#(tab)c on a.c = c.id"]) in S'
    )
    native = only(
        'let S = Sql.Database("srv", "db"), '
        'Q = Value.NativeQuery(S, "select 1#(lf)from dbo.T#(cr, lf)where x = 1") in Q'
    )

    assert source.query == (
        "select a.id from dbo.Orders a join dbo.Customers c on a.c = c.id"
    )
    assert source.tables == ("dbo.Orders", "dbo.Customers")
    assert native.query == "select 1 from dbo.T where x = 1"
    assert native.tables == ("dbo.T",)


def test_a_long_query_is_cut_for_a_line():
    source = only(
        f'let S = Sql.Database("a", "b", [Query="select {"x, " * 200} y from t"]) in S'
    )

    assert len(source.query) == 160 and source.query.endswith("…")
    assert source.tables == ("t",)


@pytest.mark.parametrize(
    "call, kind, server, database",
    [
        ('Oracle.Database("host:1521/svc")', "Oracle", "host:1521/svc", ""),
        (
            'PostgreSQL.Database("pg.example", "shop")',
            "PostgreSQL",
            "pg.example",
            "shop",
        ),
        ('MySQL.Database("my.example", "shop")', "MySQL", "my.example", "shop"),
        (
            'Snowflake.Databases("acct.snowflakecomputing.com", "WH")',
            "Snowflake",
            "acct.snowflakecomputing.com",
            "WH",
        ),
        (
            'AnalysisServices.Database("as.example", "Model")',
            "Analysis Services",
            "as.example",
            "Model",
        ),
        ('Teradata.Database("td.example")', "Teradata", "td.example", ""),
        (
            'SapHana.Database("hana:30015", [Implementation="2.0"])',
            "SAP HANA",
            "hana:30015",
            "",
        ),
    ],
)
def test_the_databases_it_knows_have_their_server_and_database(
    call, kind, server, database
):
    source = only(f"let Source = {call} in Source")

    assert (source.kind, source.server, source.database) == (kind, server, database)


def test_every_level_down_to_the_table_is_the_item():
    source = only(
        """let
            Source = Snowflake.Databases("acct.snowflakecomputing.com","WH"),
            DB = Source{[Name="SALES",Kind="Database"]}[Data],
            S = DB{[Name="PUBLIC",Kind="Schema"]}[Data],
            T = S{[Name="ORDERS",Kind="Table"]}[Data]
        in T"""
    )

    assert source.item == "SALES / PUBLIC / ORDERS"


# ---------------------------------------------------------------------------
# files, addresses and services
# ---------------------------------------------------------------------------


def test_a_workbook_has_its_file_and_its_sheet():
    source = only(
        """let
            Source = Excel.Workbook(File.Contents("C:\\Users\\me\\customers.xlsx"), null, true),
            Customers_Sheet = Source{[Item="Customers",Kind="Sheet"]}[Data]
        in Customers_Sheet"""
    )

    assert (source.kind, source.location, source.item) == (
        "Excel workbook",
        "C:\\Users\\me\\customers.xlsx",
        "Customers",
    )
    assert source.unresolved == ()  # null and true are not names


def test_a_file_named_by_a_step_is_followed():
    source = only(
        """let
            Path = "D:\\data\\book.xlsx",
            Source = Excel.Workbook(File.Contents(Path), null, true)
        in Source"""
    )

    assert source.location == "D:\\data\\book.xlsx" and source.unresolved == ()


def test_a_file_opened_in_an_earlier_step_is_the_file_of_the_format_that_reads_it():
    found = extract_sources(
        """let
            Source = File.Contents("C:\\data\\a.csv"),
            T = Csv.Document(Source, [Delimiter=","])
        in T"""
    )

    assert [(s.connector, s.kind, s.location) for s in found] == [
        ("Csv.Document", "CSV file", "C:\\data\\a.csv")
    ]


def test_a_file_that_is_named_by_a_parameter_has_the_text_of_the_parameter():
    expression = 'let T = Csv.Document(PathParam, [Delimiter=","]) in T'

    assert only(expression, {"PathParam": "/data/x.csv"}).location == "/data/x.csv"
    unknown = only(expression)
    assert unknown.location == "" and unknown.unresolved == ("PathParam",)


def test_a_step_with_a_parameter_after_its_text_is_followed():
    source = only(
        """let
            Server = "srv.example" meta [IsParameterQuery=true],
            S = Sql.Database(Server, "db")
        in S"""
    )

    assert (source.server, source.unresolved) == ("srv.example", ())


def test_names_that_stand_for_each_other_in_a_circle_do_not_loop():
    source = only('let A = B, B = A, S = Sql.Database(A, "db") in S')

    assert source.server == "" and source.database == "db"


def test_a_workbook_on_the_web_has_its_address():
    source = only(
        'let S = Excel.Workbook(Web.Contents("https://contoso.sharepoint.com/f/book.xlsx"), null, true) in S'
    )

    assert source.kind == "Excel workbook"
    assert source.location == "https://contoso.sharepoint.com/f/book.xlsx"


@pytest.mark.parametrize(
    "call, kind",
    [
        ('Csv.Document(File.Contents("/data/x.csv"),[Delimiter=","])', "CSV file"),
        ('Json.Document(File.Contents("/data/x.json"))', "JSON"),
        ('Xml.Tables(File.Contents("/data/x.xml"))', "XML"),
        ('Pdf.Tables(File.Contents("/data/x.pdf"))', "PDF"),
    ],
)
def test_the_formats_it_knows_are_read_from_their_file(call, kind):
    source = only(f"let Source = {call} in Source")

    assert source.kind == kind and source.location.startswith("/data/x.")


def test_the_inner_call_is_not_a_second_source():
    found = extract_sources('let S = Csv.Document(File.Contents("/x.csv")) in S')

    assert [s.connector for s in found] == ["Csv.Document"]


def test_a_file_that_is_read_without_a_format_is_a_source_too():
    assert only('let S = File.Contents("/x.bin") in S').kind == "File"
    assert only('let S = Web.Contents("https://x.example/api") in S').location == (
        "https://x.example/api"
    )


@pytest.mark.parametrize(
    "call, kind, location",
    [
        (
            'SharePoint.Files("https://c.sharepoint.com/sites/f", [ApiVersion = 15])',
            "SharePoint files",
            "https://c.sharepoint.com/sites/f",
        ),
        (
            'SharePoint.Tables("https://c.sharepoint.com/sites/f")',
            "SharePoint list",
            "https://c.sharepoint.com/sites/f",
        ),
        (
            'OData.Feed("https://svc.example/odata", null, [Implementation="2.0"])',
            "OData",
            "https://svc.example/odata",
        ),
        ('Folder.Files("C:\\Reports")', "Folder", "C:\\Reports"),
        (
            'AzureStorage.DataLake("https://acct.dfs.core.windows.net/fs")',
            "Azure Data Lake Storage",
            "https://acct.dfs.core.windows.net/fs",
        ),
        ('Odbc.DataSource("dsn=Warehouse")', "ODBC", "dsn=Warehouse"),
    ],
)
def test_the_services_and_places_it_knows_have_their_address(call, kind, location):
    source = only(f"let Source = {call} in Source")

    assert (source.kind, source.location) == (kind, location)


def test_a_file_of_a_sharepoint_site_has_the_name_that_is_taken_from_it():
    source = only(
        'let S = SharePoint.Files("https://c.sharepoint.com/sites/f"), '
        'F = S{[Name="a.xlsx"]}[Content] in F'
    )

    assert source.item == "a.xlsx"


# ---------------------------------------------------------------------------
# names that stand for text
# ---------------------------------------------------------------------------


def test_a_parameter_of_the_dataset_is_followed():
    source = only(
        "let Source = Sql.Database(ServerName, DbName) in Source",
        {"ServerName": "param.example", "DbName": "Sales"},
    )

    assert (source.server, source.database, source.unresolved) == (
        "param.example",
        "Sales",
        (),
    )


def test_a_quoted_name_is_followed_too():
    source = only(
        'let Source = Sql.Database(#"Server Name", "db") in Source',
        {"Server Name": "x"},
    )

    assert source.server == "x"


def test_a_name_that_cannot_be_followed_is_said():
    source = only('let Source = Sql.Database(ServerName, "db") in Source')

    assert source.server == "" and source.unresolved == ("ServerName",)
    assert source.describe() == "SQL Server  db  (not known: ServerName)"


def test_a_text_joined_from_pieces_shows_what_it_could_not_follow():
    source = only(
        'let S = Web.Contents("https://x.example/" & Version & "/items") in S',
        {},
    )

    assert source.location == "https://x.example/{Version}/items"
    assert source.unresolved == ("Version",)


def test_a_text_joined_from_pieces_that_are_known_is_whole():
    source = only(
        'let S = Web.Contents("https://x.example/" & Version & "/items") in S',
        {"Version": "v2"},
    )

    assert source.location == "https://x.example/v2/items" and source.unresolved == ()


def test_a_step_that_names_itself_does_not_loop():
    source = only('let A = A, S = Sql.Database(A, "db") in S')

    assert source.server == ""


def test_the_values_of_parameters_come_from_the_shared_queries():
    shared = [
        {
            "name": "ServerName",
            "expression": '"srv1" meta [IsParameterQuery=true, Type="Text"]',
        },
        {"name": "Plain", "expression": '"text"'},
        {"name": "Joined", "expression": '"a" & "b"'},
        {"name": "Number", "expression": "5"},
        {"name": "Query", "expression": "let a = 1 in a"},
        {"name": "", "expression": '"nameless"'},
        {"expression": '"nameless"'},
    ]

    assert parameter_values(shared) == {
        "ServerName": "srv1",
        "Plain": "text",
        "Joined": "a",
    }


# ---------------------------------------------------------------------------
# several sources, none, and what is not M
# ---------------------------------------------------------------------------


def test_a_query_that_joins_two_sources_names_both_with_their_own_table():
    found = extract_sources(
        """let
            A = Sql.Database("one", "d1"),
            B = Excel.Workbook(File.Contents("c:\\x.xlsx")),
            J = Table.NestedJoin(A{[Schema="dbo",Item="T"]}[Data], {"k"},
                                 B{[Item="S",Kind="Sheet"]}[Data], {"k"}, "j")
        in J"""
    )

    assert [(s.kind, s.item) for s in found] == [
        ("SQL Server", "dbo.T"),
        ("Excel workbook", "S"),
    ]


def test_a_nested_call_with_commas_does_not_shift_the_arguments_of_the_connector():
    source = only('let S = Sql.Database(Text.Combine({"a", "b"}, "."), "db") in S')

    assert (source.server, source.database) == ("", "db")


def test_null_is_not_a_name_that_could_not_be_followed():
    source = only('let S = Sql.Database("srv", null) in S')

    assert (source.server, source.database, source.unresolved) == ("srv", "", ())


def test_an_argument_that_says_nothing_we_show_is_not_read():
    source = only('let S = Sql.Database("a", "b", SomeOptions) in S')

    assert source.unresolved == ()


def test_a_name_that_could_not_be_followed_inside_a_file_is_said_for_the_format():
    source = only("let S = Excel.Workbook(File.Contents(PathParam), null, true) in S")

    assert source.kind == "Excel workbook" and source.unresolved == ("PathParam",)


def test_each_reader_has_the_file_it_opens_not_the_first_file():
    found = extract_sources(
        """let
            A = Csv.Document(File.Contents("a.csv")),
            B = Json.Document(File.Contents("b.json"))
        in B"""
    )

    assert [(s.kind, s.location) for s in found] == [
        ("CSV file", "a.csv"),
        ("JSON", "b.json"),
    ]


def test_each_reader_has_the_file_its_step_opens():
    found = extract_sources(
        """let
            F1 = File.Contents("a.csv"),
            F2 = File.Contents("b.csv"),
            A = Csv.Document(F2),
            B = Csv.Document(F1)
        in B"""
    )

    assert [(s.kind, s.location) for s in found] == [
        ("CSV file", "b.csv"),
        ("CSV file", "a.csv"),
    ]


def test_a_query_option_is_the_query_even_when_a_step_sends_another():
    source = only(
        """let
            Source = Sql.Database("a", "b", [Query="select 1 from t1"]),
            Q = Value.NativeQuery(Source, "select 2 from t2")
        in Q"""
    )

    assert source.query == "select 1 from t1" and source.tables == ("t1",)


def test_a_comment_between_the_name_and_the_brackets_does_not_hide_the_call():
    source = only('let S = Sql.Database /* the warehouse */ ("a", "b") in S')

    assert (source.server, source.database) == ("a", "b")


def test_a_table_that_a_query_reads_twice_is_named_once():
    source = only(
        'let S = Sql.Database("a", "b", [Query="select * from dbo.A x join dbo.A y on 1=1"]) in S'
    )

    assert source.tables == ("dbo.A",)


def test_a_place_with_a_server_and_an_address_says_both():
    source = only(
        'let S = Databricks.Catalogs("host.example", "/sql/1.0/warehouses/x") in S'
    )

    assert source.where == "host.example  /sql/1.0/warehouses/x"
    assert source.describe() == "Databricks  host.example  /sql/1.0/warehouses/x"


def test_a_failure_while_reading_is_not_the_readers_problem(monkeypatch):
    def broken(expression, parameters):
        raise ValueError("odd")

    monkeypatch.setattr("pbi_cli.core.sources._extract", broken)

    assert extract_sources("let a = 1 in a") == []


def test_the_same_source_twice_is_one():
    found = extract_sources(
        'let A = Sql.Database("s", "d"), B = Sql.Database("s", "d") in B'
    )

    assert len(found) == 1


def test_the_same_server_with_two_tables_is_two_sources_of_one_place():
    found = extract_sources(
        """let A = Sql.Database("s", "d"), B = Sql.Database("s", "d"),
            X = A{[Schema="a",Item="x"]}[Data], Y = B{[Schema="b",Item="y"]}[Data] in X"""
    )

    assert {s.key for s in found} == {("SQL Server", "s", "d", "")}
    assert [s.item for s in found] == ["a.x", "b.y"]


def test_a_comment_is_not_a_source():
    source = only(
        """let // Sql.Database("no", "no")
            Source = Sql.Database("yes", "yes") /* Sql.Database("no2", "no2") */
        in Source"""
    )

    assert source.server == "yes"


def test_a_string_that_looks_like_a_call_is_not_a_source():
    assert extract_sources('let S = Text.From("Sql.Database(a, b)") in S') == []


def test_a_name_in_quotes_is_the_text_between_them():
    tokens = tokenize('#"My ""odd"" step" = "a ""b"" c"')

    assert [(t.kind, t.text) for t in tokens] == [
        ("quoted", 'My "odd" step'),
        ("punct", "="),
        ("string", 'a "b" c'),
    ]


def test_a_calculated_table_has_no_source():
    expression = "CALENDAR(DATE(2020,1,1), DATE(2025,12,31))"

    assert extract_sources(expression) == []
    assert language_of(expression) == "DAX"


def test_a_query_that_only_works_on_other_queries_has_no_source():
    expression = "let Source = Table.Combine({Sales2019, Sales2020}) in Source"

    assert extract_sources(expression) == []
    assert language_of(expression) == "M"


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "   ",
        "let let let ((( ",
        "let Source = Sql.Database(",
        'let Source = Sql.Database("a", [',
        "}}}]]]))) in in",
        'let S = Sql.Database("a" & ) in S',
        "\x00\x01\x02",
    ],
)
def test_an_expression_that_cannot_be_read_never_raises(garbage):
    extract_sources(garbage)  # whatever it finds, it does not fail
    language_of(garbage)


def test_what_a_table_is_written_in():
    assert language_of("") == ""
    assert language_of("  let Source = 1 in Source") == "M"
    assert language_of('Sql.Database("a", "b")') == "M"  # a bare call is M
    assert language_of("SUMMARIZE(Sales, Sales[Year])") == "DAX"


def test_every_connector_has_a_kind_and_roles_that_are_known():
    for name, connector in CONNECTORS.items():
        assert connector.kind and "." in name
        assert set(connector.roles) <= {"server", "database", "location", "-"}
        assert not (connector.wraps and connector.roles)  # a format has no roles
