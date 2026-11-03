# Pemo Sync (ERPNext app)

Brings Pemo card spend into ERPNext through Pemo's External API, with the receipts
attached. Built for Designsmith (Frappe/ERPNext v15).

There are two ways in:

- **Automatic (main flow):** when someone in Pemo marks a transaction **Ready for
  export**, it arrives in ERPNext as a **Petty Expense Claim**.
- **Manual:** a **Pemo Transaction Import** turns a date range into Journal Entries.
  Use it for refunds and back-fills.

## Automatic sync: Pemo → Petty Expense Claim

Pemo's API is pull-only: it has no webhooks. So the "push" is a scheduled pull that
runs every 5 minutes while **Pemo Settings → Auto Sync** is ticked. **Sync Now** runs
it immediately.

Each pass:

1. Asks Pemo for transactions marked Ready for export, dated on or after **Sync From
   Date**.
2. Creates one **Petty Expense Claim** per transaction. It goes through the normal PEC
   approval workflow, and the site's "JV CREATION NEW -v" script posts it on approval.
3. Tells Pemo the transaction is exported (`PATCH markAsExported`), so it never arrives
   twice.

### Export button in Pemo

Pemo's own **Export** button marks a transaction exported without ever offering it to
the API as Ready. With **Include Transactions Exported in Pemo** ticked, the sync also
claims those (exported or manualExported, on or after Sync From Date, and not already
in ERPNext). It does not confirm them back, because Pemo already counts them as
exported. Clicking Export in Pemo therefore lands in ERPNext within about 5 minutes,
or immediately with **Sync Now**. Pemo has no webhooks, so nothing faster is possible
from the ERPNext side.

### Auto Submit (Designsmith runs with it on)

With **Pemo Settings → Auto Submit Claims** ticked, each claim is submitted as soon as
it is created, with no approval. The workflow goes straight to Approved, and "JV
CREATION NEW -v" posts the Journal Entry. The submit runs as Administrator, so it
doesn't depend on who clicked Sync Now.

**Every claim is submitted, including one whose Pemo transaction has no category.**
That one posts to the default claim type's account (Miscellaneous Expense → Office
expenses - DS) with VAT No, and Last Sync Status flags it. To avoid this, set the
category and VAT code in Pemo before marking a transaction Ready for export.

### Amounts

The amount booked is Pemo's `billingAmount` plus the FX fee, which is how Pemo itself
defines `totalAmount`. On Designsmith's live account, `totalAmount` comes back 10 times
too large; `billingAmount` and `walletDebitedAmount` are right, and the wallet balance
reconciles with them. Every mismatch is noted in Last Sync Status. If Pemo fixes their
side, nothing changes here.

### How a claim is filled

| Claim field | From |
|---|---|
| Requested By | the cardholder's ERPNext user (matched by Pemo email), else Settings → Fallback Requested By |
| Expense Type | Settings → Claim Expense Type (Other Expenses). A project type is used only when Pemo gives a Project with a Project Manager and Category |
| Date, Cheque Number | transaction date (Designsmith time); Pemo reference |
| Petty Cash Account | the Pemo wallet (PEMO - DS), so the approval JE credits the wallet |
| Status | Paid. The card already paid it, so it must not be paid again through a Payment Entry |
| Rows | one per Pemo line (split transactions give several rows) |
| Row Expense Type + Account | Pemo's category is the account: its full name ("Vehicle Fuel - DS"), short name, or number. The type is the Expense Claim Type that defaults to that account, else one named like it ("Site Welfare - DS" → Site Welfare), else Settings → Default Expense Claim Type |
| Row VAT | Pemo VAT code: "VAT Input - DS - 5" → Yes at 5% on VAT Input - DS; "5%" / standard → Yes; zero / exempt → No. With no code, Settings → VAT Applicable when Unknown. The VAT amount is backed out of the gross |
| Pemo section | transaction id, reference, type, cardholder, supplier, TRN, invoice no |
| Attachments | every receipt, downloaded during the pass (Pemo's links expire after about 30 minutes) |

### What is not turned into a claim

| Pemo record | What happens | Marked exported? |
|---|---|---|
| Top-up / wallet transfer | skipped: wallet funding is booked from the bank side | yes, so it stops recurring |
| Declined, zero amount | skipped | yes |
| Refund / cashback / reversal | **held**: book it with a Pemo Transaction Import, which reverses the expense | no, it stays Ready in Pemo |
| Not in company currency | **held** | no |
| Receipt download failed | claim created; receipts retried next pass with fresh links | only after the receipts arrive |

**Last Sync Status** on Pemo Settings lists every held and skipped record. Errors also
go to the Error Log.

### Safety

- If the database commit succeeds but the call to Pemo fails, the claim already exists.
  The next pass sees the transaction id is already claimed and only confirms it, so
  nothing is booked twice.
- A pass that starts while another is still running skips itself (file lock).
- Pemo Transaction Import skips anything that already has a claim.

## Manual import: Pemo Transaction Import → Journal Entries

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

## Update an installed site

```bash
cd ~/frappe-bench/apps/pemo_sync && git pull
cd ~/frappe-bench && bench --site erp.designsmith.ae migrate && bench restart
```

`migrate` adds the Pemo fields to Petty Expense Claim. The scheduler must be on for
Auto Sync: `bench --site erp.designsmith.ae enable-scheduler`.

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
| Journal Entry Workflow State | Waiting for Accounts Approval (manual import only) |
| Auto Sync | tick once Test Connection passes |
| Sync From Date | the go-live date, so spend already booked by hand is not claimed again |
| Claim Expense Type | Other Expenses |
| Default Expense Claim Type | Miscellaneous Expense (→ Office expenses - DS) |
| Fallback Requested By | an accounts user who owns claims from cards with no ERPNext user |
| VAT Applicable when Unknown | No |
| Include Transactions Exported in Pemo | unticked (only Ready for export is fetched) |
| Auto Submit Claims | ticked (no approval; every claim is submitted) |
| Claim Workflow State | blank (only used when a claim is not auto-submitted) |
| Mark Claims as Paid | ticked |

Save, then click **Test Connection**.

The background jobs need workers running: the `long` queue for fetches and syncs,
and the `short` queue for confirmations.

## Permissions

| Role | Can |
|---|---|
| Accounts User | create, fetch, submit |
| Accounts Manager / System Manager | the above, plus cancel and amend |

JEs are created with `ignore_permissions`, so a user who can submit an import does not
also need Journal Entry create rights.
