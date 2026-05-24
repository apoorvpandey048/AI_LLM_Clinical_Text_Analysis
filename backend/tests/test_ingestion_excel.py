"""
Unit tests for Excel (.xlsx) ingestion (app.ingestion.excel).

Pure-function tests: no DB, no FastAPI, no LLM. Fixtures are synthetic workbooks built
in-memory with openpyxl. A final OPTIONAL test validates against the real
`Stichprobe 1 - 150 Fälle.xlsx` if it can be located (skipped otherwise).
"""

import io
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.ingestion.excel import (
    ExcelParseError,
    parse_excel_cases,
    clean_cell_text,
    ID_PATTERN,
)


def _make_xlsx(headers, rows, sheet_title="Export Berichte") -> bytes:
    """Build an in-memory .xlsx with the given header row + data rows."""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


HEADERS = ["ID", "Diagnosen", "Operationen", "Beurteilung / Verlauf", "Procedere", "Labor"]


# --- core mapping ---------------------------------------------------------------------


def test_one_row_one_case_with_id_label():
    data = _make_xlsx(
        HEADERS,
        [
            ["OLC-CHI-000000001", "Appendizitis", "Appendektomie", "komplikationslos", "Entlassung", ""],
            ["OLC-CHI-000000002", "Cholezystitis", "Cholezystektomie", "milde Wundinfektion", "AB-Therapie", ""],
        ],
    )
    result = parse_excel_cases(data)
    assert len(result.cases) == 2
    assert [c.case_label for c in result.cases] == ["OLC-CHI-000000001", "OLC-CHI-000000002"]
    assert result.report.valid_cases == 2
    assert result.report.total_data_rows == 2


def test_labeled_section_concatenation_order_and_content():
    data = _make_xlsx(
        HEADERS,
        [["OLC-CHI-000000001", "Appendizitis", "Appendektomie", "Verlauf X", "Plan Y", ""]],
    )
    text = parse_excel_cases(data).cases[0].input_text
    # Labeled sections present, in sheet column order, with value on the next line.
    assert "Diagnosen:\nAppendizitis" in text
    assert "Operationen:\nAppendektomie" in text
    assert "Beurteilung / Verlauf:\nVerlauf X" in text
    assert "Procedere:\nPlan Y" in text
    # Order preserved.
    assert text.index("Diagnosen:") < text.index("Operationen:") < text.index("Procedere:")
    # ID is the label, not a content section.
    assert "ID:\n" not in text


def test_empty_cells_skipped_no_dangling_labels():
    data = _make_xlsx(
        HEADERS,
        [["OLC-CHI-000000001", "Appendizitis", "", "   ", "Plan Y", ""]],
    )
    text = parse_excel_cases(data).cases[0].input_text
    assert "Operationen:" not in text  # empty -> skipped
    assert "Beurteilung / Verlauf:" not in text  # whitespace-only -> skipped
    assert "Diagnosen:\nAppendizitis" in text
    assert "Procedere:\nPlan Y" in text


def test_drop_columns_excluded_even_if_filled():
    headers = ["ID", "Diagnosen", "SOFA Score 1", "Labor", "kardiovaskuläre Risikofaktoren"]
    data = _make_xlsx(headers, [["OLC-CHI-000000001", "Dx", "5", "Na 140", "Nikotin"]])
    result = parse_excel_cases(data)
    text = result.cases[0].input_text
    assert "SOFA Score 1:" not in text
    assert "Labor:" not in text
    assert "kardiovaskuläre Risikofaktoren:" not in text
    assert "Diagnosen:\nDx" in text
    # Reported as dropped.
    dropped = {d.casefold() for d in result.report.columns_dropped}
    assert "labor" in dropped


# --- text cleaning --------------------------------------------------------------------


def test_x000d_escape_stripped_keeps_newline():
    data = _make_xlsx(HEADERS, [["OLC-CHI-000000001", "Linie1_x000D_\nLinie2", "", "", "", ""]])
    text = parse_excel_cases(data).cases[0].input_text
    assert "_x000D_" not in text
    assert "Linie1\nLinie2" in text


def test_real_carriage_return_normalized():
    assert clean_cell_text("a\r\nb") == "a\nb"
    assert clean_cell_text("a\rb") == "a\nb"


def test_bullets_and_tabs_normalized():
    data = _make_xlsx(HEADERS, [["OLC-CHI-000000001", "·\tPunkt eins\n·\tPunkt zwei", "", "", "", ""]])
    text = parse_excel_cases(data).cases[0].input_text
    assert "- Punkt eins" in text
    assert "- Punkt zwei" in text
    assert "·" not in text
    assert "\t" not in text


def test_clean_cell_text_handles_none_and_numbers():
    assert clean_cell_text(None) == ""
    assert clean_cell_text(42) == "42"


# --- validation -----------------------------------------------------------------------


def test_invalid_id_flagged_but_ingested():
    data = _make_xlsx(HEADERS, [["NOT-AN-ID", "Dx", "", "", "", ""]])
    result = parse_excel_cases(data)
    assert len(result.cases) == 1
    assert "NOT-AN-ID" in result.report.invalid_id_format
    assert any("OLC-CHI" in w for w in result.cases[0].warnings)


def test_duplicate_id_flagged_and_disambiguated():
    data = _make_xlsx(
        HEADERS,
        [
            ["OLC-CHI-000000001", "Dx A", "", "", "", ""],
            ["OLC-CHI-000000001", "Dx B", "", "", "", ""],
        ],
    )
    result = parse_excel_cases(data)
    assert len(result.cases) == 2
    assert "OLC-CHI-000000001" in result.report.duplicate_ids
    labels = [c.case_label for c in result.cases]
    assert labels[0] == "OLC-CHI-000000001"
    assert labels[1] != labels[0]  # disambiguated so cases stay distinct


def test_missing_id_uses_row_label():
    data = _make_xlsx(HEADERS, [["", "Dx", "", "", "", ""]])
    result = parse_excel_cases(data)
    assert result.cases[0].case_label == "Row 1"
    assert 1 in result.report.missing_ids


def test_empty_core_rows_flagged():
    # Has content only in a non-core column (none of Diagnosen/Operationen/Verlauf/Procedere).
    headers = ["ID", "Histologie", "Diagnosen"]
    data = _make_xlsx(headers, [["OLC-CHI-000000001", "Adenokarzinom", ""]])
    result = parse_excel_cases(data)
    assert len(result.cases) == 1  # still ingested
    assert "OLC-CHI-000000001" in result.report.empty_core_rows


def test_blank_rows_skipped():
    data = _make_xlsx(
        HEADERS,
        [
            ["OLC-CHI-000000001", "Dx", "", "", "", ""],
            ["", "", "", "", "", ""],  # fully blank -> skipped
            ["OLC-CHI-000000002", "Dx2", "", "", "", ""],
        ],
    )
    result = parse_excel_cases(data)
    assert len(result.cases) == 2
    assert result.report.total_data_rows == 2  # blank row not counted


# --- workbook/sheet handling ----------------------------------------------------------


def test_sheet_autodetected_when_not_preferred_name():
    data = _make_xlsx(HEADERS, [["OLC-CHI-000000001", "Dx", "", "", "", ""]], sheet_title="Tabelle1")
    result = parse_excel_cases(data)
    assert result.report.sheet_name == "Tabelle1"
    assert len(result.cases) == 1


def test_header_row_below_a_title_row():
    wb = Workbook()
    ws = wb.active
    ws.title = "Export Berichte"
    ws.append(["Klinik-Export 2026"])  # title row, no ID
    ws.append(HEADERS)  # real header row
    ws.append(["OLC-CHI-000000001", "Dx", "", "", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    result = parse_excel_cases(buf.getvalue())
    assert len(result.cases) == 1
    assert result.cases[0].case_label == "OLC-CHI-000000001"


def test_corrupt_bytes_raises_excel_parse_error():
    with pytest.raises(ExcelParseError):
        parse_excel_cases(b"this is not a real xlsx file")


def test_no_id_column_raises():
    data = _make_xlsx(["Foo", "Bar"], [["a", "b"]])
    with pytest.raises(ExcelParseError):
        parse_excel_cases(data)


# --- optional golden test against the real dataset ------------------------------------


def _find_real_dataset() -> Path | None:
    candidates = [
        Path("/mnt/c/Users/Apoor/n/Stichprobe 1 - 150 Fälle.xlsx"),
        Path(__file__).resolve().parents[3] / "Stichprobe 1 - 150 Fälle.xlsx",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


@pytest.mark.skipif(_find_real_dataset() is None, reason="real Stichprobe dataset not available")
def test_golden_real_dataset():
    path = _find_real_dataset()
    result = parse_excel_cases(path)
    # Profiled: 156 data rows of surgical discharge reports.
    assert 150 <= result.report.valid_cases <= 160, result.report.as_dict()
    # All real IDs match the OLC-CHI pattern; none missing.
    assert not result.report.missing_ids, result.report.missing_ids
    assert not result.report.invalid_id_format, result.report.invalid_id_format[:5]
    # Every case has labeled-section text and a real ID label.
    for c in result.cases:
        assert ID_PATTERN.match(c.case_label), c.case_label
        assert ":\n" in c.input_text
        assert "_x000D_" not in c.input_text
