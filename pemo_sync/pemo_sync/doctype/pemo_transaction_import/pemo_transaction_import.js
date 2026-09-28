// Copyright (c) 2026, Designsmith and contributors
// For license information, please see license.txt

const PEMO_METHOD = "pemo_sync.pemo_sync.doctype.pemo_transaction_import.pemo_transaction_import";

frappe.ui.form.on("Pemo Transaction Import", {
	setup(frm) {
		frm.set_query("pemo_account", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("expense_account", "transactions", () => ({
			filters: { company: frm.doc.company, is_group: 0, disabled: 0 },
		}));
		frm.set_query("vat_account", "transactions", () => ({
			filters: { company: frm.doc.company, is_group: 0, disabled: 0, account_type: "Tax" },
		}));
		frm.set_query("project", "transactions", () => ({
			filters: { company: frm.doc.company },
		}));
	},

	onload(frm) {
		if (frm.is_new()) {
			frappe.call(`${PEMO_METHOD}.get_form_defaults`).then(({ message: d }) => {
				if (!d) return;
				frm.doc.__onload = Object.assign(frm.doc.__onload || {}, d);
				if (d.company && (!frm.doc.company || frm.doc.company === d.company)) {
					frm.set_value("company", d.company);
					if (!frm.doc.pemo_account && d.pemo_account) frm.set_value("pemo_account", d.pemo_account);
				}
			});
		}
		frappe.realtime.off("pemo_fetch_done");
		frappe.realtime.on("pemo_fetch_done", (data) => {
			if (!data || data.name !== frm.doc.name) return;
			frappe.show_alert(
				{ message: data.message, indicator: data.ok ? "green" : "red" },
				data.ok ? 10 : 20
			);
			frm.reload_doc();
		});
	},

	refresh(frm) {
		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.add_custom_button(__("Fetch from Pemo"), () => fetch_from_pemo(frm));
			frm.add_custom_button(__("Mark Complete Rows Verified"), () => verify_complete_rows(frm), __("Rows"));
		}
		if (frm.doc.docstatus === 1) {
			const unposted = (frm.doc.transactions || []).filter((r) => !r.journal_entry).length;
			if (unposted) {
				frm.add_custom_button(__("Create Missing Journal Entries ({0})", [unposted]), () =>
					frm.call({ method: `${PEMO_METHOD}.create_missing_journal_entries`, args: { name: frm.doc.name }, freeze: true })
						.then((r) => {
							frappe.show_alert({ message: __("{0} Journal Entr(ies) created", [(r.message || []).length]), indicator: "green" });
							frm.reload_doc();
						})
				);
			}
			if (["Failed", "Pending"].includes(frm.doc.pemo_export_status)) {
				frm.add_custom_button(__("Retry Pemo Confirmation"), () =>
					frm.call({ method: `${PEMO_METHOD}.retry_export_confirmation`, args: { name: frm.doc.name } })
						.then(() => frappe.show_alert(__("Queued. Reload in a few seconds.")))
				);
			}
		}
		if (frm.doc.pemo_export_status === "Failed") {
			frm.dashboard.set_headline_alert(
				__("Pemo was not told these transactions are exported, so they will come back on the next fetch. See the Pemo Export Log, then use Retry Pemo Confirmation."),
				"red"
			);
		}
	},
});

frappe.ui.form.on("Pemo Transaction Details", {
	amount_incl: (frm, cdt, cdn) => calculate_row(frm, cdt, cdn),
	vat_applicable: (frm, cdt, cdn) => calculate_row(frm, cdt, cdn),
	vat_amount: (frm, cdt, cdn) => calculate_row(frm, cdt, cdn),
	manual_vat: (frm, cdt, cdn) => calculate_row(frm, cdt, cdn),
});

function fetch_from_pemo(frm) {
	const go = () =>
		frm.call({ method: `${PEMO_METHOD}.fetch_transactions`, args: { name: frm.doc.name }, freeze: true })
			.then((r) => r.message && frappe.show_alert({ message: r.message, indicator: "blue" }, 10));
	if (frm.is_dirty()) {
		frm.save().then(go);
	} else {
		go();
	}
}

function verify_complete_rows(frm) {
	let count = 0;
	(frm.doc.transactions || []).forEach((row) => {
		if (row.journal_entry || row.verified) return;
		const vat_ok = row.vat_applicable === "No" || (row.vat_applicable === "Yes" && row.vat_account);
		if (row.expense_account && vat_ok && row.amount_incl > 0) {
			frappe.model.set_value(row.doctype, row.name, "verified", 1);
			count++;
		}
	});
	frappe.show_alert(__("{0} row(s) marked verified. Rows missing an account or VAT choice were left alone.", [count]));
}

// Mirrors PemoTransactionImport._calculate, which is authoritative on save.
function calculate_row(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	const onload = frm.doc.__onload || {};
	const rate = flt(onload.vat_rate) || 5;
	const gross = flt(row.amount_incl, 2);
	if (row.vat_applicable === "Yes") {
		const vat = row.manual_vat ? flt(row.vat_amount, 2) : flt(gross - gross / (1 + rate / 100), 2);
		row.tax_rate = rate;
		row.vat_amount = vat;
		row.amount_excl_vat = flt(gross - vat, 2);
		if (!row.vat_account && onload.input_vat_account) row.vat_account = onload.input_vat_account;
	} else {
		row.tax_rate = 0;
		row.vat_amount = 0;
		row.amount_excl_vat = gross;
		if (row.vat_applicable === "No") row.vat_account = "";
	}
	frm.refresh_field("transactions");
}
