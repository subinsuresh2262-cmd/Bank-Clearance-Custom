frappe.ui.form.on("Bank Clearance", {
    onload(frm) {
        set_account_filter(frm);
        set_template_query(frm);
    },

    refresh(frm) {
        set_account_filter(frm);
        set_template_query(frm);

        setTimeout(() => {
            calculate_all_rows(frm);
        }, 300);
    },

    custom_sales_taxes_and_charges_template(frm) {
        if (!frm.doc.custom_sales_taxes_and_charges_template) {
            frm.clear_table("custom_taxes");
            frm.refresh_field("custom_taxes");
            calculate_all_rows(frm);
            return;
        }

        frappe.call({
            method: "bank_clearance_custom.overrides.bank_clearance.get_taxes_from_template",
            args: {
                template_name: frm.doc.custom_sales_taxes_and_charges_template,
                company: frm.doc.company,
            },
            callback: function (r) {
                frm.clear_table("custom_taxes");

                (r.message || []).forEach(row => {
                    let child = frm.add_child("custom_taxes");
                    child.charge_type = row.charge_type || "";
                    child.account_head = row.account_head || "";
                    child.description = row.description || "";
                    child.tax_rate = parseFloat(row.tax_rate || 0);
                    child.tax_amount = parseFloat(row.tax_amount || 0);
                    child.total = 0;
                });

                frm.refresh_field("custom_taxes");

                setTimeout(() => {
                    calculate_all_rows(frm);
                }, 300);
            }
        });
    }
});

frappe.ui.form.on("Bank Clearance Tax", {
    charge_type(frm, cdt, cdn) {
        calculate_all_rows(frm);
    },
    tax_rate(frm, cdt, cdn) {
        calculate_all_rows(frm);
    },
    tax_amount(frm, cdt, cdn) {
        calculate_all_rows(frm);
    },
    custom_taxes_add(frm) {
        calculate_all_rows(frm);
    },
    custom_taxes_remove(frm) {
        calculate_all_rows(frm);
    }
});

function set_account_filter(frm) {
    frm.set_query("custom_account_to", () => {
        return {
            filters: {
                account_type: "Bank",
                is_group: 0,
                company: frm.doc.company || undefined
            }
        };
    });
}

function set_template_query(frm) {
    frm.set_query("custom_sales_taxes_and_charges_template", () => {
        return {
            filters: {
                company: frm.doc.company || undefined
            }
        };
    });
}

function get_base_amount(frm) {
    let total = 0;
    (frm.doc.payment_entries || []).forEach(d => {
        total += parseFloat(d.amount || 0);
    });
    return total;
}

function calculate_all_rows(frm) {
    let base_total = get_base_amount(frm);
    let total_taxes = 0;
    let running_total = 0;
    let previous_row_amount = 0;

    (frm.doc.custom_taxes || []).forEach((row, index) => {
        let charge_type = (row.charge_type || "").trim();
        let rate = parseFloat(row.tax_rate || 0);
        let amount = parseFloat(row.tax_amount || 0);

        if (charge_type === "Actual") {
            amount = parseFloat(row.tax_amount || 0);
        } else if (charge_type === "On Net Total") {
            amount = (base_total * rate) / 100;
        } else if (charge_type === "On Previous Row Amount") {
            amount = (previous_row_amount * rate) / 100;
        } else if (charge_type === "On Previous Row Total") {
            amount = (running_total * rate) / 100;
        } else if (charge_type === "On Item Quantity") {
            amount = 0;
        } else {
            amount = parseFloat(row.tax_amount || 0);
        }

        if (index === 0) {
            running_total = amount;
        } else {
            running_total += amount;
        }

        row.tax_amount = amount;
        row.total = running_total;

        total_taxes += amount;
        previous_row_amount = amount;
    });

    let grand_total = base_total - total_taxes;

    frm.refresh_field("custom_taxes");

    if (frm.fields_dict.custom_base_total) {
        frm.set_value("custom_base_total", base_total);
    }

    if (frm.fields_dict.custom_total_taxes_and_charges) {
        frm.set_value("custom_total_taxes_and_charges", total_taxes);
    }

    if (frm.fields_dict.custom_grand_total) {
        frm.set_value("custom_grand_total", grand_total);
    }

    frm.refresh_fields([
        "custom_base_total",
        "custom_total_taxes_and_charges",
        "custom_grand_total"
    ]);
}