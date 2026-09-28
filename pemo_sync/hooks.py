app_name = "pemo_sync"
app_title = "Pemo Sync"
app_publisher = "Designsmith"
app_description = "Import Pemo card spend into ERPNext as Journal Entries, with receipts."
app_email = "kamal.maharofficial@gmail.com"
app_license = "MIT"
required_apps = ["erpnext"]

# ---------------------------------------------------------------------------
# Install / migrate
# ---------------------------------------------------------------------------
# after_install disables the Server/Client Scripts copied over from UMI
# ("Create Jv", "api", "attach pemo", "Pemo Calculation") — left enabled they
# would post every row a second time. Both hooks (re)create the two Journal
# Entry link fields back to the import.
after_install = "pemo_sync.install.after_install"
after_migrate = "pemo_sync.install.after_migrate"

# ---------------------------------------------------------------------------
# Journal Entry lifecycle
# ---------------------------------------------------------------------------
# When a Pemo JE is deleted (rejected draft) or cancelled, free its import row
# so it can be posted again from the import's "Create Missing Journal Entries".
doc_events = {
    "Journal Entry": {
        "on_trash": "pemo_sync.posting.release_rows_for_journal_entry",
        "on_cancel": "pemo_sync.posting.release_rows_for_journal_entry",
    },
}
