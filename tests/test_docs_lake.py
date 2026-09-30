"""docs/lake.md lists the registry's endpoints and quotas: keep the two in step."""

import re
from pathlib import Path

from pbi_cli.core.registry import ENDPOINTS

PAGE = Path(__file__).resolve().parents[1] / "docs" / "lake.md"


def table_rows():
    """``{endpoint id: [cells]}`` for the rows of the endpoint table."""
    text = PAGE.read_text(encoding="utf-8")
    section = text.split("## Endpoints and quotas", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        match = re.match(r"\| `([a-z_.]+)` \|", line)
        if match:
            rows[match.group(1)] = [
                c.strip() for c in line.strip().strip("|").split("|")
            ]
    return rows


def test_every_endpoint_is_in_the_table_with_its_documented_quota():
    rows = table_rows()
    for endpoint in ENDPOINTS:
        assert endpoint.id in rows, f"{endpoint.id} is missing from docs/lake.md"
        _, operation, needs, quota, stored = rows[endpoint.id]
        assert needs == endpoint.scope.value, endpoint.id
        expected_quota = endpoint.limit.describe() if endpoint.limit else "–"
        assert quota == expected_quota, endpoint.id
        assert stored == endpoint.kind.value, endpoint.id
        assert operation.startswith(f"[{endpoint.title}]"), endpoint.id
        assert f"]({endpoint.doc_url})" in operation, endpoint.id


def test_the_table_lists_no_endpoint_the_registry_does_not_have():
    assert set(table_rows()) == {endpoint.id for endpoint in ENDPOINTS}
