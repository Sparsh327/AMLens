"""Load IBM AMLworld HI-Small into the MS SQL warehouse (schema: ADR 0001).

    ./scripts/odbc.sh uv run python scripts/load_mssql.py             # first load
    ./scripts/odbc.sh uv run python scripts/load_mssql.py --rebuild   # drop + reload
    ./scripts/odbc.sh uv run python scripts/load_mssql.py --limit 50000 --rebuild  # quick smoke run

(`scripts/odbc.sh` only sets the macOS OpenSSL path the ODBC driver needs.)

Steps: create DB -> create tables -> load banks/entities/accounts -> stream transactions
+ labels in chunks -> parse patterns and link them -> add FKs and indexes -> create the
read-only login -> verify (row counts, and that the reader can't see labels or write).
"""

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyodbc

from amlens.core.config import Settings, get_settings
from amlens.core.exceptions import IngestionError
from amlens.modules.ingestion import ibm_aml
from amlens.modules.ingestion import warehouse_schema as ws

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW = REPO_ROOT / "data" / "raw"
TRANS_CSV = RAW / "HI-Small_Trans.csv"
ACCOUNTS_CSV = RAW / "HI-Small_accounts.csv"
PATTERNS_TXT = RAW / "HI-Small_Patterns.txt"

# pyodbc's fast_executemany sizes each parameter from the FIRST row unless told otherwise.
# A first row of Decimal('12.50') would make it truncate Bitcoin's 6 decimals later, so
# every column's SQL type is declared explicitly: (type, size/precision, scale).
SqlType = tuple[int, int, int]


def VARCHAR(n: int) -> SqlType:
    return (pyodbc.SQL_VARCHAR, n, 0)


INT = (pyodbc.SQL_INTEGER, 0, 0)
BIGINT = (pyodbc.SQL_BIGINT, 0, 0)
BIT = (pyodbc.SQL_BIT, 0, 0)
MONEY = (pyodbc.SQL_DECIMAL, 24, 6)
DATETIME = (pyodbc.SQL_TYPE_TIMESTAMP, 19, 0)

INSERT_TRANSACTION = (
    "INSERT INTO bank.transactions (transaction_id, ts, from_bank, from_account, to_bank, to_account,"
    " amount_paid, payment_currency, amount_received, receiving_currency, payment_format)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
TRANSACTION_SIZES = [
    BIGINT,
    DATETIME,
    INT,
    VARCHAR(20),
    INT,
    VARCHAR(20),
    MONEY,
    VARCHAR(30),
    MONEY,
    VARCHAR(30),
    VARCHAR(20),
]


def connect(settings: Settings, database: str, *, autocommit: bool = False) -> pyodbc.Connection:
    return pyodbc.connect(settings.mssql_conn_str(database), autocommit=autocommit, timeout=10)


def insert_many(conn: pyodbc.Connection, sql: str, rows: Sequence[Sequence[Any]], sizes: list[Any]) -> None:
    if not rows:
        return
    cur = conn.cursor()
    cur.fast_executemany = True  # send rows in bulk instead of one round trip per row
    cur.setinputsizes(sizes)
    cur.executemany(sql, rows)
    cur.close()


def recreate_database(settings: Settings, rebuild: bool) -> None:
    db = settings.mssql_warehouse_db
    with connect(settings, "master", autocommit=True) as master:
        exists = master.execute("SELECT DB_ID(?)", db).fetchval() is not None
        if exists and not rebuild:
            sys.exit(f"Database {db} already exists. Re-run with --rebuild to drop and reload it.")
        if exists:
            print(f"Dropping database {db} ...")
            # Kick out any open connections first, or DROP fails.
            master.execute(f"ALTER DATABASE {ws.sql_identifier(db)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            master.execute(f"DROP DATABASE {ws.sql_identifier(db)}")
        master.execute(f"CREATE DATABASE {ws.sql_identifier(db)}")
        # SIMPLE recovery: no point-in-time restore needed for a rebuildable copy; much less log.
        master.execute(f"ALTER DATABASE {ws.sql_identifier(db)} SET RECOVERY SIMPLE")


def create_tables(conn: pyodbc.Connection) -> None:
    for schema in ws.SCHEMAS:
        conn.execute(f"CREATE SCHEMA {schema}")
    for ddl in ws.CREATE_TABLES:
        conn.execute(ddl)
    conn.commit()


def load_reference_data(conn: pyodbc.Connection) -> None:
    insert_many(
        conn,
        "INSERT INTO bank.currencies (currency_name, iso_code) VALUES (?, ?)",
        list(ibm_aml.CURRENCY_ISO.items()),
        [VARCHAR(30), (pyodbc.SQL_CHAR, 3, 0)],
    )
    insert_many(
        conn,
        "INSERT INTO bank.payment_formats (payment_format) VALUES (?)",
        [(f,) for f in ibm_aml.PAYMENT_FORMATS],
        [VARCHAR(20)],
    )
    conn.commit()


def load_accounts(conn: pyodbc.Connection) -> int:
    accounts = next(ibm_aml.read_csv_as_text(ACCOUNTS_CSV, ibm_aml.ACCOUNT_COLUMNS, chunksize=10**7))
    accounts["bank_id"] = accounts["bank_id"].astype("int64")

    banks = accounts[["bank_id", "bank_name"]].drop_duplicates()
    if banks["bank_id"].duplicated().any():
        raise IngestionError("a bank_id has more than one bank_name")
    entities = accounts[["entity_id", "entity_name"]].drop_duplicates()
    if entities["entity_id"].duplicated().any():
        raise IngestionError("an entity_id has more than one entity_name")
    entities["entity_type"] = entities["entity_name"].map(ibm_aml.entity_type)

    insert_many(
        conn,
        "INSERT INTO bank.banks (bank_id, bank_name) VALUES (?, ?)",
        list(banks.itertuples(index=False, name=None)),
        [INT, VARCHAR(50)],
    )
    insert_many(
        conn,
        "INSERT INTO bank.entities (entity_id, entity_name, entity_type) VALUES (?, ?, ?)",
        list(entities.itertuples(index=False, name=None)),
        [VARCHAR(20), VARCHAR(50), VARCHAR(30)],
    )
    insert_many(
        conn,
        "INSERT INTO bank.accounts (bank_id, account_number, entity_id) VALUES (?, ?, ?)",
        list(accounts[["bank_id", "account_number", "entity_id"]].itertuples(index=False, name=None)),
        [INT, VARCHAR(20), VARCHAR(20)],
    )
    conn.commit()
    print(f"  banks {len(banks):,}  entities {len(entities):,}  accounts {len(accounts):,}")
    return len(accounts)


def load_transactions(
    conn: pyodbc.Connection, chunksize: int, limit: int | None
) -> tuple[int, dict[ibm_aml.MatchKey, int]]:
    """Stream the CSV in chunks. Returns (row count, laundering match_key -> transaction_id)."""
    next_id = 1  # transaction_id = 1-based row position in the CSV (ADR 0001 #1)
    laundering_ids: dict[ibm_aml.MatchKey, int] = {}
    started = time.perf_counter()

    for chunk in ibm_aml.read_csv_as_text(TRANS_CSV, ibm_aml.TRANSACTION_COLUMNS, chunksize):
        if limit is not None:
            chunk = chunk.iloc[: max(0, limit - next_id + 1)]
            if chunk.empty:
                break
        rows, labels = ibm_aml.transaction_rows(chunk, next_id)
        insert_many(conn, INSERT_TRANSACTION, rows, TRANSACTION_SIZES)
        insert_many(
            conn,
            "INSERT INTO labels.transaction_labels (transaction_id, is_laundering) VALUES (?, ?)",
            labels,
            [BIGINT, BIT],
        )
        conn.commit()  # one transaction per chunk: a crash loses at most one chunk

        # Remember laundering rows so pattern lines can be linked to their transaction_id.
        laundering = chunk[chunk["is_laundering"] == "1"]
        for offset, fields in zip(laundering.index, laundering.to_numpy().tolist(), strict=True):
            key = ibm_aml.match_key(fields[:-1])
            if key in laundering_ids:
                raise IngestionError(f"two laundering rows share the same content: {key}")
            laundering_ids[key] = next_id + (offset - chunk.index[0])

        next_id += len(chunk)
        rate = (next_id - 1) / (time.perf_counter() - started)
        print(f"  transactions {next_id - 1:>10,}  ({rate:,.0f} rows/s)", flush=True)

    return next_id - 1, laundering_ids


def load_patterns(
    conn: pyodbc.Connection, laundering_ids: dict[ibm_aml.MatchKey, int], partial: bool
) -> tuple[int, int]:
    attempts, pattern_rows = ibm_aml.parse_patterns(PATTERNS_TXT.read_text())
    links: list[tuple[int, int]] = []
    unmatched = 0
    for p in pattern_rows:
        tx_id = laundering_ids.get(p.key)
        if tx_id is None:
            unmatched += 1
        else:
            links.append((tx_id, p.attempt_id))
    if unmatched and not partial:
        raise IngestionError(f"{unmatched} pattern rows match no laundering transaction")

    insert_many(
        conn,
        "INSERT INTO labels.laundering_attempts (attempt_id, typology, detail) VALUES (?, ?, ?)",
        [(a.attempt_id, a.typology, a.detail) for a in attempts],
        [INT, VARCHAR(20), VARCHAR(50)],
    )
    insert_many(
        conn,
        "INSERT INTO labels.transaction_attempts (transaction_id, attempt_id) VALUES (?, ?)",
        links,
        [BIGINT, INT],
    )
    conn.commit()
    note = f" ({unmatched:,} skipped: --limit)" if unmatched else ""
    print(f"  attempts {len(attempts):,}  linked pattern transactions {len(links):,}{note}")
    return len(attempts), len(links)


def add_constraints_and_indexes(conn: pyodbc.Connection) -> None:
    for ddl in ws.ADD_CONSTRAINTS_AND_INDEXES:
        name = ddl.split("CONSTRAINT ")[-1].split()[0] if "CONSTRAINT" in ddl else ddl.split()[2]
        t = time.perf_counter()
        conn.execute(ddl)
        conn.commit()
        print(f"  {name:38s} {time.perf_counter() - t:6.1f}s")


def create_reader(settings: Settings, conn: pyodbc.Connection) -> None:
    user, password = settings.mssql_reader_user, settings.mssql_reader_password.get_secret_value()
    with connect(settings, "master", autocommit=True) as master:
        master.execute(ws.reader_login_sql(user, password))
    for stmt in ws.reader_grants_sql(user):
        conn.execute(stmt)
    conn.commit()


def verify(settings: Settings, expected_tx: int, expected_links: int) -> None:
    """Fail loudly if counts are off or the reader login can do more than read `bank`."""
    db = settings.mssql_warehouse_db
    with connect(settings, db) as admin:
        counts = {
            t: admin.execute(f"SELECT COUNT_BIG(*) FROM {t}").fetchval()
            for t in ("bank.transactions", "labels.transaction_labels", "labels.transaction_attempts")
        }
        btc_min = admin.execute(
            "SELECT MIN(amount_paid) FROM bank.transactions WHERE payment_currency = 'Bitcoin'"
        ).fetchval()
    print(f"  counts {counts}  smallest Bitcoin payment {btc_min}")
    if counts["bank.transactions"] != expected_tx or counts["labels.transaction_labels"] != expected_tx:
        raise IngestionError(f"row count mismatch, expected {expected_tx:,}: {counts}")
    if counts["labels.transaction_attempts"] != expected_links:
        raise IngestionError(f"pattern link count mismatch, expected {expected_links:,}")

    reader_conn = settings.mssql_conn_str(db, settings.mssql_reader_user, settings.mssql_reader_password)
    with pyodbc.connect(reader_conn, timeout=10) as reader:
        reader.execute("SELECT TOP 1 transaction_id FROM bank.transactions").fetchone()
        for forbidden in (
            "SELECT TOP 1 * FROM labels.transaction_labels",
            "DELETE FROM bank.transactions WHERE transaction_id = -1",
            "CREATE TABLE bank.hack (x INT)",
        ):
            try:
                reader.execute(forbidden)
            except pyodbc.Error:
                reader.rollback()
                continue
            raise IngestionError(f"reader login was allowed to run: {forbidden}")
    print(f"  reader '{settings.mssql_reader_user}': can read bank, cannot read labels or write ✓")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--rebuild", action="store_true", help="drop and recreate the warehouse DB")
    parser.add_argument("--chunksize", type=int, default=200_000, help="CSV rows per insert batch")
    parser.add_argument("--limit", type=int, default=None, help="load only the first N transactions")
    args = parser.parse_args()

    for f in (TRANS_CSV, ACCOUNTS_CSV, PATTERNS_TXT):
        if not f.exists():
            sys.exit(f"Missing {f}. Run: uv run python scripts/download_data.py")

    settings = get_settings()
    db = settings.mssql_warehouse_db
    started = time.perf_counter()

    print(f"[1/7] database {db}")
    recreate_database(settings, args.rebuild)
    with connect(settings, db) as conn:
        print("[2/7] tables")
        create_tables(conn)
        load_reference_data(conn)
        print("[3/7] banks, entities, accounts")
        load_accounts(conn)
        print("[4/7] transactions + labels")
        n_tx, laundering_ids = load_transactions(conn, args.chunksize, args.limit)
        print("[5/7] laundering patterns")
        _, n_links = load_patterns(conn, laundering_ids, partial=args.limit is not None)
        print("[6/7] foreign keys + indexes (validates every row)")
        add_constraints_and_indexes(conn)
        create_reader(settings, conn)
    print("[7/7] verify")
    verify(settings, n_tx, n_links)
    print(f"Done in {time.perf_counter() - started:,.0f}s. Warehouse: {db} on {settings.mssql_host}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
