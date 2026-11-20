"""Turn the raw IBM AMLworld HI-Small files into rows ready for the warehouse.

Pure functions, no database: easy to unit test. `scripts/load_mssql.py` does the I/O.
Every rule here comes from docs/decisions/0001-warehouse-schema.md (ADR 0001):

- files are read as TEXT, so amounts go str -> Decimal exactly (never via float)
- bank ids are ints ("010" and "0010" are the same bank)
- transaction_id = 1-based row position in the CSV
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from amlens.core.exceptions import IngestionError

TRANSACTION_COLUMNS = [
    "timestamp",
    "from_bank",
    "from_account",
    "to_bank",
    "to_account",
    "amount_received",
    "receiving_currency",
    "amount_paid",
    "payment_currency",
    "payment_format",
    "is_laundering",
]
ACCOUNT_COLUMNS = ["bank_name", "bank_id", "account_number", "entity_id", "entity_name"]
TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M"

# Exact spellings found in the data (notebook 4.3) -> ISO 4217 code. XBT is the common Bitcoin code.
CURRENCY_ISO: dict[str, str] = {
    "Australian Dollar": "AUD",
    "Bitcoin": "XBT",
    "Brazil Real": "BRL",
    "Canadian Dollar": "CAD",
    "Euro": "EUR",
    "Mexican Peso": "MXN",
    "Ruble": "RUB",
    "Rupee": "INR",
    "Saudi Riyal": "SAR",
    "Shekel": "ILS",
    "Swiss Franc": "CHF",
    "UK Pound": "GBP",
    "US Dollar": "USD",
    "Yen": "JPY",
    "Yuan": "CNY",
}
PAYMENT_FORMATS = ["ACH", "Bitcoin", "Cash", "Cheque", "Credit Card", "Reinvestment", "Wire"]

# The source has no transaction id, so pattern rows are matched to transactions by their
# content: every column except is_laundering, bank ids normalised to int (notebook 6.3).
MatchKey = tuple[str, int, str, int, str, str, str, str, str, str]

# Column order = INSERT order in scripts/load_mssql.py
TransactionRow = tuple[int, datetime, int, str, int, str, Decimal, str, Decimal, str, str]
LabelRow = tuple[int, bool]


def read_csv_as_text(path: Path, columns: Sequence[str], chunksize: int) -> Iterator[pd.DataFrame]:
    """Yield the CSV in chunks, every value as the exact text from the file.

    `keep_default_na=False` stops pandas turning strings like "NA" into missing values.
    """
    reader = pd.read_csv(path, dtype=str, keep_default_na=False, chunksize=chunksize)
    for chunk in reader:
        if len(chunk.columns) != len(columns):
            raise IngestionError(f"{path.name}: expected {len(columns)} columns, got {len(chunk.columns)}")
        chunk.columns = pd.Index(columns)
        yield chunk


def parse_amount(text: str) -> Decimal:
    """'2848.96' -> Decimal('2848.96'). Exact: Decimal is built from the text, not a float."""
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise IngestionError(f"not a number: {text!r}") from exc
    if not value.is_finite() or value < 0:
        raise IngestionError(f"invalid amount: {text!r}")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -6:
        raise IngestionError(f"more than 6 decimals won't fit DECIMAL(24,6): {text!r}")
    return value


def match_key(fields: Sequence[str]) -> MatchKey:
    """The 10 non-label fields of a transaction line -> a hashable key."""
    if len(fields) != 10:
        raise IngestionError(f"expected 10 fields for a match key, got {len(fields)}")
    ts, from_bank, from_acct, to_bank, to_acct, amt_recv, recv_cur, amt_paid, pay_cur, fmt = fields
    return (ts, int(from_bank), from_acct, int(to_bank), to_acct, amt_recv, recv_cur, amt_paid, pay_cur, fmt)


def transaction_rows(chunk: pd.DataFrame, first_id: int) -> tuple[list[TransactionRow], list[LabelRow]]:
    """Convert one text chunk into (bank.transactions rows, labels.transaction_labels rows)."""
    bad_labels = set(chunk["is_laundering"].unique()) - {"0", "1"}
    if bad_labels:
        raise IngestionError(f"is_laundering must be 0/1, found {sorted(bad_labels)}")

    ids = range(first_id, first_id + len(chunk))
    timestamps = [datetime.strptime(s, TIMESTAMP_FORMAT) for s in chunk["timestamp"]]
    rows: list[TransactionRow] = list(
        zip(
            ids,
            timestamps,
            chunk["from_bank"].astype("int64").tolist(),
            chunk["from_account"].tolist(),
            chunk["to_bank"].astype("int64").tolist(),
            chunk["to_account"].tolist(),
            [parse_amount(s) for s in chunk["amount_paid"]],
            chunk["payment_currency"].tolist(),
            [parse_amount(s) for s in chunk["amount_received"]],
            chunk["receiving_currency"].tolist(),
            chunk["payment_format"].tolist(),
            strict=True,
        )
    )
    labels: list[LabelRow] = list(zip(ids, (chunk["is_laundering"] == "1").tolist(), strict=True))
    return rows, labels


def entity_type(entity_name: str) -> str:
    """'Corporation #33520' -> 'Corporation'."""
    return entity_name.split(" #", 1)[0]


@dataclass(frozen=True)
class LaunderingAttempt:
    attempt_id: int
    typology: str  # e.g. "FAN-OUT"
    detail: str | None  # e.g. "Max 16-degree Fan-Out"; None for STACK / BIPARTITE


@dataclass(frozen=True)
class PatternTransaction:
    attempt_id: int
    key: MatchKey


def parse_patterns(text: str) -> tuple[list[LaunderingAttempt], list[PatternTransaction]]:
    """Parse HI-Small_Patterns.txt (BEGIN ... END blocks of transaction lines).

    Same state machine as notebook 6.1, but strict: anything unexpected raises.
    """
    attempts: list[LaunderingAttempt] = []
    pattern_rows: list[PatternTransaction] = []
    current: LaunderingAttempt | None = None

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("BEGIN"):
            if current is not None:
                raise IngestionError(f"line {line_no}: BEGIN inside an open attempt")
            _, sep, header = line.partition(" - ")
            if not sep:
                raise IngestionError(f"line {line_no}: BEGIN line without ' - <typology>'")
            name, _, extra = header.partition(":")
            current = LaunderingAttempt(len(attempts) + 1, name.strip(), extra.strip() or None)
            attempts.append(current)
        elif line.startswith("END"):
            if current is None:
                raise IngestionError(f"line {line_no}: END without BEGIN")
            current = None
        else:
            if current is None:
                raise IngestionError(f"line {line_no}: transaction outside a BEGIN/END block")
            fields = line.split(",")
            if len(fields) != len(TRANSACTION_COLUMNS):
                raise IngestionError(f"line {line_no}: expected 11 fields, got {len(fields)}")
            if fields[-1] != "1":
                raise IngestionError(f"line {line_no}: pattern transaction not labelled laundering")
            pattern_rows.append(PatternTransaction(current.attempt_id, match_key(fields[:-1])))

    if current is not None:
        raise IngestionError("file ended inside an open attempt (missing END)")
    return attempts, pattern_rows
