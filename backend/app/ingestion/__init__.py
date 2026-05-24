"""
Ingestion package — structured-input parsers that map source documents to cases.

Currently provides Excel (.xlsx) ingestion for the clinical-case batch dataset.
Pure parsing/preprocessing logic with no FastAPI/DB dependencies so it is unit-testable
in isolation and reusable from both the upload route and offline tooling.
"""

from app.ingestion.excel import (
    CaseRecord,
    ExcelParseError,
    ParseResult,
    ValidationReport,
    parse_excel_cases,
)

__all__ = [
    "CaseRecord",
    "ExcelParseError",
    "ParseResult",
    "ValidationReport",
    "parse_excel_cases",
]
