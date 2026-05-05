"""Validation engine for staging transforms.

Provides a small, project-scoped validation runner that loads declarative
rules from Documentation/validation_rules.json and applies them to Pandas
DataFrames produced by the staging transforms.

The engine is intentionally lightweight (no extra dependencies) and supports:
- required column checks
- row count min/max
- per-column null-rate thresholds
- uniqueness checks for key columns

Failures are reported with a severity (error/warn/info) and details.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ValidationFailure:
    severity: str
    check: str
    message: str
    details: Optional[Dict[str, Any]] = None


@dataclass
class ValidationReport:
    failures: List[ValidationFailure] = field(default_factory=list)

    def add(self, severity: str, check: str, message: str, details: Optional[Dict[str, Any]] = None):
        self.failures.append(ValidationFailure(severity, check, message, details))


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_rules(path: Optional[Path] = None) -> Dict[str, Any]:
    if path is None:
        path = _project_root() / "Documentation" / "validation_rules.json"

    try:
        with path.open("r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("Validation rules file not found: %s", path)
        return {}
    except json.JSONDecodeError as e:
        logger.error("Failed to parse validation rules: %s", e)
        return {}


def _check_schema(report: ValidationReport, df: pd.DataFrame, rules: Dict[str, Any]):
    schema = rules.get("schema", {})
    required = schema.get("required_columns", [])
    missing = [c for c in required if c not in df.columns]
    if missing:
        report.add("error", "schema", "Missing required columns", {"missing": missing})


def _check_row_count(report: ValidationReport, df: pd.DataFrame, rules: Dict[str, Any]):
    rc = rules.get("row_count", {})
    n = len(df)
    if rc:
        if rc.get("min") is not None and n < int(rc.get("min")):
            report.add("error", "row_count", "Row count below minimum", {"min": rc.get("min"), "actual": n})
        if rc.get("max") is not None and n > int(rc.get("max")):
            report.add("warn", "row_count", "Row count above maximum", {"max": rc.get("max"), "actual": n})


def _check_null_thresholds(report: ValidationReport, df: pd.DataFrame, rules: Dict[str, Any]):
    nulls = rules.get("null_thresholds", {})
    for col, thresh in nulls.items():
        if col not in df.columns:
            report.add("warn", "null_thresholds", f"Column {col} not present", {"column": col})
            continue
        try:
            null_rate = float(df[col].isna().mean())
        except Exception:
            report.add("warn", "null_thresholds", f"Unable to compute null rate for {col}", {"column": col})
            continue
        if null_rate > float(thresh):
            report.add("error", "null_thresholds", f"Null rate {null_rate:.3f} exceeds threshold {thresh}", {"column": col, "null_rate": null_rate})


def _check_uniqueness(report: ValidationReport, df: pd.DataFrame, rules: Dict[str, Any]):
    unique = rules.get("uniqueness", [])
    for subset in unique:
        # ensure columns exist
        if any(c not in df.columns for c in subset):
            report.add("warn", "uniqueness", "Uniqueness columns missing", {"columns": subset})
            continue
        dup = df.duplicated(subset=subset, keep=False)
        if dup.any():
            dup_count = int(dup.sum())
            report.add("error", "uniqueness", "Duplicate rows found for uniqueness keys", {"columns": subset, "duplicates": dup_count})


def run_validations(df: pd.DataFrame, data_type: str, city: Optional[str] = None, year: Optional[int] = None, month: Optional[int] = None, rules_path: Optional[str] = None) -> ValidationReport:
    """Run configured validations for a given DataFrame and data_type.

    Returns a ValidationReport containing any failures found.
    """
    report = ValidationReport()

    if df is None:
        report.add("error", "data", "DataFrame is None or empty")
        return report

    if not isinstance(df, pd.DataFrame):
        report.add("error", "data", "Provided object is not a DataFrame")
        return report

    rules = {}
    try:
        rules_doc = load_rules(Path(rules_path) if rules_path else None)
        rules = rules_doc.get(data_type, {}) if isinstance(rules_doc, dict) else {}
    except Exception as e:
        logger.warning("Failed to load validation rules: %s", e)

    # Run checks
    _check_schema(report, df, rules)

    # If schema has fatal failures, return early
    if any(f.severity == "error" and f.check == "schema" for f in report.failures):
        return report

    _check_row_count(report, df, rules)
    _check_null_thresholds(report, df, rules)
    _check_uniqueness(report, df, rules)

    return report


__all__ = ["run_validations", "load_rules", "ValidationReport", "ValidationFailure"]
