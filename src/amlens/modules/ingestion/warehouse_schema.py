"""T-SQL for the MS SQL warehouse (docs/decisions/0001-warehouse-schema.md).

Two schemas:
- `bank`   what a real bank has. The read-only text-to-SQL login can SELECT here.
- `labels` ground truth (is_laundering, typologies). The reader login is DENIED.

Bulk-load pattern: create tables with primary keys only, load the data, THEN add
foreign keys (WITH CHECK, so every existing row is validated) and indexes. Checking an
FK and updating indexes on every insert is far slower than doing it once at the end.
"""

SCHEMAS = ["bank", "labels"]

CREATE_TABLES = [
    """
    CREATE TABLE bank.banks (
        bank_id   INT         NOT NULL CONSTRAINT pk_banks PRIMARY KEY,
        bank_name VARCHAR(50) NOT NULL
    )""",
    """
    CREATE TABLE bank.entities (
        entity_id   VARCHAR(20) NOT NULL CONSTRAINT pk_entities PRIMARY KEY,
        entity_name VARCHAR(50) NOT NULL,
        entity_type VARCHAR(30) NOT NULL
    )""",
    """
    CREATE TABLE bank.accounts (
        bank_id        INT         NOT NULL,
        account_number VARCHAR(20) NOT NULL,
        entity_id      VARCHAR(20) NOT NULL,
        CONSTRAINT pk_accounts PRIMARY KEY (bank_id, account_number)
    )""",
    """
    CREATE TABLE bank.currencies (
        currency_name VARCHAR(30) NOT NULL CONSTRAINT pk_currencies PRIMARY KEY,
        iso_code      CHAR(3)     NOT NULL CONSTRAINT uq_currencies_iso UNIQUE
    )""",
    """
    CREATE TABLE bank.payment_formats (
        payment_format VARCHAR(20) NOT NULL CONSTRAINT pk_payment_formats PRIMARY KEY
    )""",
    """
    CREATE TABLE bank.transactions (
        transaction_id     BIGINT        NOT NULL CONSTRAINT pk_transactions PRIMARY KEY,
        ts                 DATETIME2(0)  NOT NULL,  -- UTC by convention; source has no time zone
        from_bank          INT           NOT NULL,
        from_account       VARCHAR(20)   NOT NULL,
        to_bank            INT           NOT NULL,
        to_account         VARCHAR(20)   NOT NULL,
        amount_paid        DECIMAL(24,6) NOT NULL,
        payment_currency   VARCHAR(30)   NOT NULL,
        amount_received    DECIMAL(24,6) NOT NULL,
        receiving_currency VARCHAR(30)   NOT NULL,
        payment_format     VARCHAR(20)   NOT NULL,
        is_self_transfer AS CAST(
            CASE WHEN from_bank = to_bank AND from_account = to_account THEN 1 ELSE 0 END AS BIT
        ) PERSISTED
    )""",
    """
    CREATE TABLE labels.transaction_labels (
        transaction_id BIGINT NOT NULL CONSTRAINT pk_transaction_labels PRIMARY KEY,
        is_laundering  BIT    NOT NULL
    )""",
    """
    CREATE TABLE labels.laundering_attempts (
        attempt_id INT         NOT NULL CONSTRAINT pk_laundering_attempts PRIMARY KEY,
        typology   VARCHAR(20) NOT NULL,
        detail     VARCHAR(50) NULL
    )""",
    """
    CREATE TABLE labels.transaction_attempts (
        transaction_id BIGINT NOT NULL,
        attempt_id     INT    NOT NULL,
        CONSTRAINT pk_transaction_attempts PRIMARY KEY (transaction_id, attempt_id)
    )""",
]

# Run after the data is in. WITH CHECK makes SQL Server validate every existing row.
ADD_CONSTRAINTS_AND_INDEXES = [
    "ALTER TABLE bank.accounts WITH CHECK ADD CONSTRAINT fk_accounts_bank"
    " FOREIGN KEY (bank_id) REFERENCES bank.banks (bank_id)",
    "ALTER TABLE bank.accounts WITH CHECK ADD CONSTRAINT fk_accounts_entity"
    " FOREIGN KEY (entity_id) REFERENCES bank.entities (entity_id)",
    # Two FKs to accounts: sender AND receiver (notebook 5.2)
    "ALTER TABLE bank.transactions WITH CHECK ADD CONSTRAINT fk_transactions_from_account"
    " FOREIGN KEY (from_bank, from_account) REFERENCES bank.accounts (bank_id, account_number)",
    "ALTER TABLE bank.transactions WITH CHECK ADD CONSTRAINT fk_transactions_to_account"
    " FOREIGN KEY (to_bank, to_account) REFERENCES bank.accounts (bank_id, account_number)",
    "ALTER TABLE bank.transactions WITH CHECK ADD CONSTRAINT fk_transactions_payment_currency"
    " FOREIGN KEY (payment_currency) REFERENCES bank.currencies (currency_name)",
    "ALTER TABLE bank.transactions WITH CHECK ADD CONSTRAINT fk_transactions_receiving_currency"
    " FOREIGN KEY (receiving_currency) REFERENCES bank.currencies (currency_name)",
    "ALTER TABLE bank.transactions WITH CHECK ADD CONSTRAINT fk_transactions_payment_format"
    " FOREIGN KEY (payment_format) REFERENCES bank.payment_formats (payment_format)",
    "ALTER TABLE labels.transaction_labels WITH CHECK ADD CONSTRAINT fk_labels_transaction"
    " FOREIGN KEY (transaction_id) REFERENCES bank.transactions (transaction_id)",
    "ALTER TABLE labels.transaction_attempts WITH CHECK ADD CONSTRAINT fk_tx_attempts_transaction"
    " FOREIGN KEY (transaction_id) REFERENCES bank.transactions (transaction_id)",
    "ALTER TABLE labels.transaction_attempts WITH CHECK ADD CONSTRAINT fk_tx_attempts_attempt"
    " FOREIGN KEY (attempt_id) REFERENCES labels.laundering_attempts (attempt_id)",
    # Rules ask "this account, this time window" for senders (fan-out) and receivers (fan-in)
    "CREATE INDEX ix_transactions_from_ts ON bank.transactions (from_bank, from_account, ts)",
    "CREATE INDEX ix_transactions_to_ts ON bank.transactions (to_bank, to_account, ts)",
    "CREATE INDEX ix_transactions_ts ON bank.transactions (ts)",
    "CREATE INDEX ix_accounts_entity ON bank.accounts (entity_id)",
]


def sql_literal(value: str) -> str:
    """Quote a string for T-SQL DDL, where parameters (?) aren't allowed: O'Brien -> 'O''Brien'."""
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(name: str) -> str:
    """Quote a database/user name: amlens -> [amlens]. Only plain names are accepted."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f"[{name}]"


def reader_login_sql(user: str, password: str) -> str:
    """Server-level login (run in `master`). Re-running updates the password."""
    u, p = sql_identifier(user), sql_literal(password)
    return (
        f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = {sql_literal(user)})\n"
        f"    CREATE LOGIN {u} WITH PASSWORD = {p}, DEFAULT_DATABASE = [master], CHECK_POLICY = ON;\n"
        f"ELSE\n"
        f"    ALTER LOGIN {u} WITH PASSWORD = {p};"
    )


def reader_grants_sql(user: str) -> list[str]:
    """Database user + permissions (run in the warehouse DB).

    Only GRANT SELECT on `bank`; nothing else is granted, so no INSERT/UPDATE/DELETE/DDL.
    DENY on `labels` is explicit, so a later broad GRANT can't accidentally expose labels.
    """
    u = sql_identifier(user)
    return [
        f"CREATE USER {u} FOR LOGIN {u}",
        f"GRANT SELECT ON SCHEMA::bank TO {u}",
        f"DENY SELECT, INSERT, UPDATE, DELETE ON SCHEMA::labels TO {u}",
    ]
