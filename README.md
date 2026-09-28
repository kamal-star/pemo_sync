# Pemo Sync (ERPNext app)

Pulls Pemo card spend into ERPNext through Pemo's External API, and books it as
Journal Entries with the receipts attached. Built for Designsmith (Frappe/ERPNext v15).

## What it does

1. **Fetch.** On a draft **Pemo Transaction Import**, click **Fetch from Pemo**. A
   background job pulls `GET /v1/transactions` for the date range, 50 at a time, and
   stops when a page comes back short. It also:
   - adds one row per transaction, or one row per line when Pemo split it;
   - downloads each receipt straight away, because Pemo's links expire after about
     30 minutes, and stores it as a private file on the import;
   - skips any transaction that is already in another draft or submitted import;
   - pre-fills Expense Account, Project and VAT Applicable from Pemo's own
     categorisation, where the values match (see below).
2. **Review.** For each row, check the Expense Account and the VAT choice, then tick
   **Verified**. **Rows → Mark Complete Rows Verified** ticks every row that already
   has an account and a VAT choice.
3. **Submit.** Each transaction becomes one draft Journal Entry, moved to
   **Waiting for Accounts Approval**, so it goes through the normal JE Standard
   approval. Its receipts are attached. After the database commit, Pemo is sent
   `PATCH markAsExported`, so the same spend is never handed out again.

### Postings

| Row | Journal Entry |
|---|---|
| Purchase / withdrawal / fee, VAT Yes | Dr Expense (net), Dr VAT Input (VAT), Cr Pemo wallet (gross) |
| Purchase, VAT No | Dr Expense, Cr Pemo wallet |
| Refund / cashback / chargeback / reversal (`Is Credit`) | the same lines, sides swapped |

- **Amount booked:** Pemo `totalAmount`, the wallet-currency amount including the FX
  fee. (UMI's version booked `actualAmount`, which is in the merchant's currency, so a
  USD purchase was booked as that many AED.) The merchant's own amount and currency
  are kept on the row for reference.
- **VAT:** backed out of the gross at the Settings rate (5%). Tick **Manual VAT
  Amount** on a row to type the VAT from the receipt instead.
- **Cost center:** the row's Project cost center, else Settings → Default Cost
  Center, else the Company default.
- Each JE has **Reference No** = Pemo reference, plus **Pemo Import** and **Pemo
  Transaction ID**, which trace it back to the import.

### Skipped on fetch

- `declined` transactions.
- `topUp`, `walletTransfer` and `subWalletTransfer`, while **Skip Wallet Funding
  Movements** is on. Wallet funding is already booked from the bank side
  (JV/26/6333: BOB OD → PEMO - DS), so importing it too would double count.
- Wallet-currency transactions that are not in the company currency.

Every skip is listed in the import's **Last Fetch** log.

### Pre-fill from Pemo's `accounting` array

| Pemo type | Fills | Match |
|---|---|---|
| `chartOfAccounts` | Expense Account | account number = code, else account name = value |
| `project` | Project | name = code/value, else project_name = value |
| `vatCode` | VAT Applicable | "5%" / "standard" → Yes, "zero" / "exempt" → No |
| `vendor` (+ `trn`) | Supplier, Supplier TRN | — |
| `receiptNumber`, or a custom field labelled Invoice No / Bill No | Invoice No | — |
| `memo` | Memo | — |

Anything that does not match is left blank for the reviewer. Pemo's raw category is
shown on the row as **Pemo Category**.

## After submit

- **FC rejects a JE and deletes it, or cancels an approved one.** The import row is
  unlinked automatically. Fix the row on the submitted import (account, VAT and
  project stay editable on unposted rows), then click **Create Missing Journal
  Entries**.
- **The Pemo confirmation failed.** The form shows a red banner. Fix the cause (see
  **Pemo Export Log**), then click **Retry Pemo Confirmation**. The PATCH is
  idempotent, so retrying is safe.
- **Cancelling the import** deletes its draft JEs and cancels its approved ones.
  Pemo still has those records as exported, so to re-import them, amend the import,
  or tick **Include Already Exported** on a new one.

## Install (Designsmith bench)

```bash
cd ~/frappe-bench
bench get-app https://github.com/kamal-star/pemo_sync.git
bench --site erp.designsmith.ae install-app pemo_sync
bench restart
```

`install-app` does three things:

- It takes over the existing **Pemo Transaction Import** and **Pemo Transaction
  Details** doctypes. They were UI-made custom doctypes with no records. The app keeps
  every existing field and adds the new ones.
- It **disables** (does not delete) the Server and Client Scripts copied from UMI:
  *Create Jv*, *api*, *attach pemo* and *Pemo Calculation*. They called `apiapp`,
  which is not installed here, and *Create Jv* would post every row a second time.
- It adds **Pemo Import** and **Pemo Transaction ID** fields to Journal Entry.

## Configure: Pemo Settings

| Field | Designsmith value |
|---|---|
| Environment | Production (the Sandbox key only works with Sandbox) |
| API Key | from the Pemo app, entered by Designsmith |
| Company | Designsmith Interior Works LLC |
| Pemo Wallet Account | PEMO - DS |
| Input VAT Account | VAT Input - DS (VAT 5% - DS is disabled) |
| VAT Rate | 5 |
| Default Cost Center | Group - DS |
| Journal Entry Workflow State | Waiting for Accounts Approval |

Save, then click **Test Connection**.

The background jobs need workers running: the `long` queue for fetches and the
`short` queue for confirmations.

## Permissions

| Role | Can |
|---|---|
| Accounts User | create, fetch, submit |
| Accounts Manager / System Manager | the above, plus cancel and amend |

JEs are created with `ignore_permissions`, so a user who can submit an import does not
also need Journal Entry create rights.
