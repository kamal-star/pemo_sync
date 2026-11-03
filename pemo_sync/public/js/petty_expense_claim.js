// Copyright (c) 2026, Designsmith and contributors
// For license information, please see license.txt

// Added by pemo_sync to Designsmith's Petty Expense Claim form.
frappe.ui.form.on("Petty Expense Claim", {
	refresh(frm) {
		if (frm.doc.docstatus !== 0 || frm.is_new() || !frm.doc.pemo_transaction_id) return;
		frm.add_custom_button(__("Refresh from Pemo"), () => {
			const go = () =>
				frappe
					.call({
						method: "pemo_sync.pec_sync.refresh_claim",
						args: { name: frm.doc.name },
						freeze: true,
						freeze_message: __("Reading the transaction from Pemo..."),
					})
					.then((r) => {
						if (r.message) frappe.msgprint({ title: __("Pemo"), message: r.message, indicator: "blue" });
						frm.reload_doc();
					});
			if (frm.is_dirty()) {
				frappe.confirm(__("Refreshing replaces the rows with Pemo's data and discards unsaved changes. Continue?"), go);
			} else {
				go();
			}
		});
	},
});
