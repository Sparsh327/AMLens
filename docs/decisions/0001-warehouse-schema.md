# ADR 0001 — MS SQL warehouse schema for IBM AMLworld HI-Small

- **Status:** accepted
- **Date:** 2026-11-17
- **Evidence:** `notebooks/01_explore_hi_small.ipynb` (step numbers below refer to it)

## Context

The MS SQL warehouse plays the bank's transaction system. Three components will read it:
- the rules engine (per-account time windows)
- the scoring features
- the **text-to-SQL agent** (read-only, allowlisted)

The source is IBM AMLworld HI-Small. It is **synthetic data**, with these files:
- `HI-Small_Trans.csv`: 5,078,345 rows, 2022-09-01 → 09-18
- `HI-Small_accounts.csv`: 518,581 rows
- `HI-Small_Patterns.txt`: 370 laundering attempts

The schema must:
- store money exactly
- make wrong joins impossible
- keep the ground-truth labels away from the agent

## Decisions

### Two schemas, two permission levels
- `bank`: what a real bank would have. The text-to-SQL user (`amlens_reader`) gets `SELECT` here only.
- `labels`: ground truth (`is_laundering`, typologies). It is used by evals and model training, and the reader user has **no access**.

**Why:** if the agent could read `is_laundering`, it could "solve" an alert by looking up the answer. That would make evals meaningless and would never exist at a real bank. Enforcing it with database permissions (not a prompt) is CLAUDE.md rule 5.

### Tables

```
bank.banks            (bank_id INT PK, bank_name VARCHAR(50))
bank.entities         (entity_id VARCHAR(20) PK, entity_name VARCHAR(50), entity_type VARCHAR(30))
bank.accounts         (bank_id INT, account_number VARCHAR(20), entity_id VARCHAR(20),
                       PK (bank_id, account_number), FK bank_id → banks, FK entity_id → entities)
bank.currencies       (currency_name VARCHAR(30) PK, iso_code CHAR(3) UNIQUE)
bank.payment_formats  (payment_format VARCHAR(20) PK)
bank.transactions     (transaction_id BIGINT PK,
                       ts DATETIME2(0),
                       from_bank INT, from_account VARCHAR(20),
                       to_bank INT,   to_account VARCHAR(20),
                       amount_paid DECIMAL(24,6),     payment_currency VARCHAR(30),
                       amount_received DECIMAL(24,6), receiving_currency VARCHAR(30),
                       payment_format VARCHAR(20),
                       is_self_transfer AS (...) PERSISTED,
                       FK (from_bank, from_account) → accounts, FK (to_bank, to_account) → accounts,
                       FK currencies ×2, FK payment_format)

labels.transaction_labels   (transaction_id BIGINT PK FK, is_laundering BIT)
labels.laundering_attempts  (attempt_id INT PK, typology VARCHAR(20), detail VARCHAR(50))
labels.transaction_attempts (transaction_id BIGINT, attempt_id INT, PK (transaction_id, attempt_id))
```

### Decision by decision

| # | Decision | Evidence |
|---|---|---|
| 1 | **`transaction_id` = 1-based row position in the CSV**, assigned by the loader, not `IDENTITY` (which can skip values after a failed insert). No natural key exists: even all 11 columns together have 9 duplicate pairs. | 2.1, 2.3 |
| 2 | **Account PK = `(bank_id, account_number)`.** 8 account numbers exist at two banks, so joining on the number alone silently merges unrelated customers. | 5.1 |
| 3 | **`bank_id` is `INT`.** The CSV writes it zero-padded and inconsistently (`010` and `0010`), but each integer has exactly one bank name. | 5.1 |
| 4 | **Foreign keys transactions → accounts on both sides.** 0 of 5.08M transactions reference an unknown account. | 5.2 |
| 5 | **Money = `DECIMAL(24,6)`** (see below). | 4.2 |
| 6 | **Keep both amount/currency pairs.** They differ exactly when currencies differ (72,158 FX rows), and one can't be derived from the other without exchange rates. | 4.1 |
| 7 | **Readable names + lookup-table FKs** for currency and payment format. Queries stay join-free for the agent (`WHERE payment_currency = 'US Dollar'`), and the lookup table lists valid values plus ISO codes (`USD`, `XBT` for Bitcoin) to translate user wording. | 3.3, 4.3 |
| 8 | **`is_self_transfer` persisted computed column.** 591,212 rows (11.6%) are self-transfers: all Reinvestment rows plus ~70k own-account FX conversions. The graph tool must skip them (fake cycles), but they stay in the data because 11 are laundering. | 5.3 |
| 9 | **Indexes:** clustered PK on `transaction_id`; `(from_bank, from_account, ts)`, `(to_bank, to_account, ts)`, `(ts)`. The rules are per-account time windows (fan-out/fan-in), and the busiest accounts have up to 169k rows, so scans are not an option. | 2.2, 5.4 |
| 10 | **Typology labels in a link table.** Only 3,209 of 5,177 laundering rows (62%) belong to a pattern. | 6.3 |
| 11 | **Timestamp `DATETIME2(0)`, treated as UTC.** The source has minute precision and no time zone. | 2.2 |

### Money: why not the CLAUDE.md default `DECIMAL(18,2)`

- Every fiat amount has exactly 2 decimals.
- Every **Bitcoin** amount has exactly **6** (e.g. `0.025852`).
- At 2 decimals, 23,966 of 146,066 Bitcoin payments would be stored as `0.00`, and the rest would lose precision.
- The largest amount is ~1.05 trillion Yen (13 integer digits).
- `DECIMAL(24,6)` gives 18 integer digits and 6 decimals.
- It costs 13 bytes per value instead of 9; at 5M rows × 2 columns that's ~40 MB, which is acceptable.

**Options considered:**
- **(a) `DECIMAL(18,2)`:** loses Bitcoin. Rejected.
- **(b) Store Bitcoin in satoshis as `BIGINT`:** exact, but every query needs per-currency unit logic, and the text-to-SQL agent would get it wrong. Rejected.
- **(c) One wider `DECIMAL(24,6)` column:** chosen.

**CLAUDE.md §5 is updated:** app code still uses `Decimal`; the column type for this warehouse is `(24,6)`.

## What failed first

- **The float decimal check was wrong.** The first check was `(amount * 100) % 1 != 0`. It flagged ~1,450 large US Dollar amounts as having "extra decimals", because `154102439.08 * 100` isn't exact in float. Re-reading the raw CSV as text showed exactly 2 or 6 decimals. Lesson: count digits on the source text, not on floats.
- **The CSV-speed guess was wrong.** I expected CSV loading to take ~30 s and motivated Parquet with speed. Measured: CSV 0.13 s, Parquet 0.08 s. Parquet is kept for size (476 → 132 MB) and preserved types, not speed.

## Consequences

- **The loader** (`scripts/load_mssql.py`):
  - reads the CSV in file order to assign `transaction_id`
  - parses `bank_id` as int and amounts as `Decimal` from text (never via float)
  - loads `labels.*` from `Is Laundering` and the patterns file
- **Recall is reported twice:** on pattern laundering (62%) and on *all* laundering. A shape-based rule can't catch the 38% that has no typology, and hiding that would break CLAUDE.md rule 3.
- **Evals use Sep 1–10 only.** Sep 11–18 is a tail of finishing schemes, 57–73% laundering (step 2.2), so it's not representative.
- **Synthetic-data quirks the model must not learn:**
  - no Wire laundering (3.3)
  - no cross-currency laundering (4.1)

  Both are listed in the README as limitations.
- **Not decided yet:**
  - columnstore index for analytics (revisit if feature queries are slow)
  - partitioning by date (not needed at 5M rows)
