"""Unit tests for the IBM AML parsing rules (ADR 0001). No database needed."""

from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from amlens.core.exceptions import IngestionError
from amlens.modules.ingestion import ibm_aml
from amlens.modules.ingestion import warehouse_schema as ws

TX_LINE = "2022/09/01 00:06,021174,800737690,012,80011F990,2848.96,Euro,2848.96,Euro,ACH,1"
BTC_LINE = "2022/09/01 00:07,0010,8000A0000,010,8000B0000,0.000008,Bitcoin,0.000008,Bitcoin,Bitcoin,0"


# --- money ----------------------------------------------------------------------------------


def test_parse_amount_is_exact() -> None:
    # float(0.1) + float(0.2) != 0.3, Decimal built from text is exact
    assert ibm_aml.parse_amount("0.1") + ibm_aml.parse_amount("0.2") == Decimal("0.3")


def test_parse_amount_keeps_bitcoin_six_decimals() -> None:
    assert ibm_aml.parse_amount("0.000008") == Decimal("0.000008")


@pytest.mark.parametrize("bad", ["abc", "", "-5.00", "NaN", "1.0000001"])
def test_parse_amount_rejects_bad_values(bad: str) -> None:
    with pytest.raises(IngestionError):
        ibm_aml.parse_amount(bad)


# --- transactions ---------------------------------------------------------------------------


def _chunk(*lines: str) -> pd.DataFrame:
    return pd.DataFrame([line.split(",") for line in lines], columns=ibm_aml.TRANSACTION_COLUMNS)


def test_transaction_rows_types_and_ids() -> None:
    rows, labels = ibm_aml.transaction_rows(_chunk(TX_LINE, BTC_LINE), first_id=101)

    tx_id, ts, from_bank, from_acct, to_bank, _, paid, pay_cur, _, _, fmt = rows[0]
    assert tx_id == 101
    assert ts.isoformat() == "2022-09-01T00:06:00"
    assert (from_bank, to_bank) == (21174, 12)  # zero padding dropped
    assert from_acct == "800737690"
    assert paid == Decimal("2848.96")
    assert (pay_cur, fmt) == ("Euro", "ACH")

    assert rows[1][0] == 102
    assert rows[1][6] == Decimal("0.000008")
    assert labels == [(101, True), (102, False)]


def test_transaction_rows_rejects_bad_label() -> None:
    with pytest.raises(IngestionError, match="0/1"):
        ibm_aml.transaction_rows(_chunk(TX_LINE.removesuffix("1") + "yes"), first_id=1)


def test_match_key_treats_bank_padding_as_same_bank() -> None:
    a = ibm_aml.match_key(BTC_LINE.split(",")[:-1])
    b = ibm_aml.match_key(BTC_LINE.replace(",0010,", ",010,").split(",")[:-1])
    assert a == b


def test_read_csv_as_text_keeps_exact_text(tmp_path: Path) -> None:
    csv = tmp_path / "t.csv"
    header = "Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,"
    header += "Amount Paid,Payment Currency,Payment Format,Is Laundering"
    csv.write_text(f"{header}\n{TX_LINE}\n")
    chunk = next(ibm_aml.read_csv_as_text(csv, ibm_aml.TRANSACTION_COLUMNS, chunksize=10))
    assert chunk.loc[0, "from_bank"] == "021174"  # not 21174.0 or 21174
    assert chunk.loc[0, "amount_paid"] == "2848.96"


# --- patterns -------------------------------------------------------------------------------

PATTERNS = f"""BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 16-degree Fan-Out
{TX_LINE}
{TX_LINE.replace("00:06", "04:33")}
END LAUNDERING ATTEMPT - FAN-OUT

BEGIN LAUNDERING ATTEMPT - STACK
{TX_LINE.replace("00:06", "09:14")}
END LAUNDERING ATTEMPT - STACK
"""


def test_parse_patterns() -> None:
    attempts, rows = ibm_aml.parse_patterns(PATTERNS)
    assert attempts == [
        ibm_aml.LaunderingAttempt(1, "FAN-OUT", "Max 16-degree Fan-Out"),
        ibm_aml.LaunderingAttempt(2, "STACK", None),
    ]
    assert [r.attempt_id for r in rows] == [1, 1, 2]
    assert rows[0].key == ibm_aml.match_key(TX_LINE.split(",")[:-1])


@pytest.mark.parametrize(
    "text",
    [
        TX_LINE,  # transaction outside a block
        "END LAUNDERING ATTEMPT - CYCLE",  # END without BEGIN
        "BEGIN LAUNDERING ATTEMPT - CYCLE\n" + TX_LINE,  # missing END
        "BEGIN LAUNDERING ATTEMPT - CYCLE\nBEGIN LAUNDERING ATTEMPT - STACK",  # nested BEGIN
        "BEGIN LAUNDERING ATTEMPT - CYCLE\n" + TX_LINE.removesuffix("1") + "0\nEND",  # not laundering
        "BEGIN LAUNDERING ATTEMPT - CYCLE\n1,2,3\nEND",  # wrong field count
    ],
)
def test_parse_patterns_rejects_malformed(text: str) -> None:
    with pytest.raises(IngestionError):
        ibm_aml.parse_patterns(text)


def test_entity_type() -> None:
    assert ibm_aml.entity_type("Corporation #33520") == "Corporation"
    assert ibm_aml.entity_type("Sole Proprietorship #50438") == "Sole Proprietorship"


def test_currency_iso_codes_are_unique_three_letters() -> None:
    codes = list(ibm_aml.CURRENCY_ISO.values())
    assert len(codes) == len(set(codes)) == 15
    assert all(len(c) == 3 and c.isupper() for c in codes)


# --- SQL quoting + reader permissions -------------------------------------------------------


def test_sql_literal_escapes_quotes() -> None:
    assert ws.sql_literal("O'Brien") == "'O''Brien'"


@pytest.mark.parametrize("bad", ["x]; DROP DATABASE y; --", "a b", "a'b"])
def test_sql_identifier_rejects_unsafe_names(bad: str) -> None:
    with pytest.raises(ValueError):
        ws.sql_identifier(bad)


def test_reader_gets_select_on_bank_only() -> None:
    grants = " ".join(ws.reader_grants_sql("amlens_reader"))
    assert "GRANT SELECT ON SCHEMA::bank" in grants
    assert "DENY SELECT, INSERT, UPDATE, DELETE ON SCHEMA::labels" in grants
    assert "INSERT ON SCHEMA::bank" not in grants
    assert "db_owner" not in grants and "db_datareader" not in grants
