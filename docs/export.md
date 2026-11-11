# Exporting a report

`pbi reports export` saves a report as a file: a `.pbix` to open in Power BI Desktop, or a
PDF, a PowerPoint presentation or images of its pages. (`pbi export` is the same command.)
Nothing is stored in the [data lake](lake.md); the file goes where you say.

```bash
pbi reports export -g GROUP_ID -r REPORT_ID -t sales.pbix            # for Power BI Desktop
pbi reports export -g GROUP_ID -r REPORT_ID -f pdf -t sales.pdf      # a PDF
pbi reports export -g GROUP_ID -r REPORT_ID -f pptx -t ~/Reports     # a file named after the report
```

Find the ids with `pbi reports list`, or in the [terminal UI](tui.md) (the JSON tab of a
report). The command uses your user account (`pbi auth -t <token> -g user`): it needs to be
able to open the report.

## Formats

| `-f` | You get | Needs |
| --- | --- | --- |
| `pbix` (the default) | the report as a `.pbix` (a paginated report as `.rdl`) | permission to download the report |
| `pdf` | a PDF | the workspace on a Premium, Embedded or Fabric capacity |
| `pptx` | a PowerPoint presentation | the same |
| `png` | an image of each page, as a `.zip` when the report has several | the same, and *Export reports as image files* switched on by an administrator |
| `xlsx`, `docx`, `csv`, `xml`, `mhtml`, `accessiblepdf` | a **paginated** report in that format | the same |

A capacity of *Premium Per User* does not count for the formats other than `pbix`: Power BI
does not offer the export there. If the job fails, the error lists the usual causes.

## Choosing what is exported (pdf, pptx, png)

```bash
# Two pages, in this order, for one territory, in German number formats
pbi reports export -g GROUP_ID -r REPORT_ID -f pdf \
    -p ReportSection3 -p ReportSection1 \
    --filter "Store/Territory eq 'NC'" --locale de-de -t sales.pdf
```

- `-p` / `--page` is the *name* of a page, as `pbi reports pages` lists it (not its title).
  Without it, every page that is not hidden is exported; `--include-hidden-pages` adds those.
- `--filter` is a report filter in the syntax of the URL of a report (`Table/Field eq
  'value'`, `Table/Field in ('a', 'b')`, joined with `and`), without the `?filter=`.
- `--locale` sets how numbers and dates are written.
- One export holds at most 50 pages or visuals, and the file at most 250 MB.

Personal bookmarks and persistent filters are not part of an export, and a report with R or
Python visuals, uncertified custom visuals or ArcGIS maps shows an error symbol in their place.

## Paginated reports

`--param Year=2026` (repeat it) sets the parameters of a paginated report for the formats
`xlsx`, `docx`, `csv`, `xml`, `mhtml` and `accessiblepdf`. Pages, filters and hidden pages are
for Power BI reports, and parameters for paginated ones: the command says so before it asks
Power BI anything.

## Where the file goes

`-t` is a file name or a folder. A name without an ending gets the one of the format
(`-t sales` with `-f pdf` is `sales.pdf`); a folder gets a file named after the report. If the
ending you typed is not what Power BI sent (a `.png` name for a `.zip` of pages), the file is
saved as you said and the command tells you. Without `-t` nothing is saved, and the command
says how big the file is.

## Waiting

Everything but a `.pbix` is made by Power BI as a job. The command starts it, shows how far
it is (`Running 40%`), and saves the file when it is done. `--timeout` (600 seconds by
default) is how long it waits; a job that takes longer is named so that you can try again
with more time or fewer pages. When Power BI answers that it is busy, the command starts the
job again, up to three times.

## From Python

```python
from pbi_cli.core.reportexport import export_report

with open_client(credentials_provider) as client:
    done = export_report(client, group_id, report_id, "pdf", pages=["ReportSection1"])
Path("sales" + done.extension).write_bytes(done.content)
```

See [`pbi_cli.core.reportexport`](references/core/reportexport.md).
