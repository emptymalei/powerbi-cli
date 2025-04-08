from pathlib import Path

import pandas as pd


def multi_group_dict_to_excel(data: dict, target: Path):
    """
    Writes a dictionary of lists into an Excel file, with each key as a worksheet.

    ```python
    data = {
        "Sheet1": [{"a": 1, "b": 2}, {"a": 3, "b": 4}],
        "Sheet2": [{"x": 10, "y": 20}, {"x": 30, "y": 40}],
    }
    dict_to_excel(data, "output.xlsx")
    ```

    :param data: A dictionary where each key maps to a list of dictionaries or data.
    :param file_name: Name of the Excel file to save
    """
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        for sheet_name, rows in data.items():
            df = pd.DataFrame(rows)
            df.to_excel(writer, sheet_name=sheet_name, index=False)
