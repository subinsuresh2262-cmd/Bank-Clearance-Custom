import frappe
from frappe import _
from frappe.utils import flt
from erpnext.accounts.doctype.bank_clearance.bank_clearance import (
    BankClearance as ERPNextBankClearance,
)


class BankClearance(ERPNextBankClearance):

    @frappe.whitelist()
    def update_clearance_date(self):
        has_cheque_payment = any(
            d.clearance_date
            and d.payment_document == "Payment Entry"
            and is_cheque_payment_entry(d.payment_entry)
            for d in self.get("payment_entries")
        )

        if has_cheque_payment and not self.custom_account_to:
            frappe.throw(_("Please select Account To (Bank) in Bank Clearance"))

        validate_tax_rows(self)

        for d in self.get("payment_entries"):
            if (
                d.clearance_date
                and d.payment_document == "Payment Entry"
                and is_cheque_payment_entry(d.payment_entry)
            ):
                result = create_internal_transfer_and_tax_entries(
                    pe_name=d.payment_entry,
                    clearance_date=d.clearance_date,
                    account_to=self.custom_account_to,
                    bank_clearance_doc=self,
                )

                if result:
                    self.db_set("custom_cleared_amount_after_tax", result.get("net_amount"))
                    if result.get("journal_entry"):
                        self.db_set("custom_journal_entry_for_tax", result.get("journal_entry"))

        return super().update_clearance_date()


@frappe.whitelist()
def get_taxes_from_template(template_name, company=None):
    if not template_name:
        return []

    template = frappe.get_doc("Sales Taxes and Charges Template", template_name)

    rows = []
    for d in template.get("taxes"):
        rows.append({
            "charge_type": d.charge_type or "",
            "account_head": d.account_head or "",
            "description": d.description or "",
            "tax_rate": float(d.rate or 0),
            "tax_amount": float(d.tax_amount or 0) if d.charge_type == "Actual" else 0,
            "total": 0,
        })

    return rows


def validate_tax_rows(doc):
    total = 0
    for row in doc.get("custom_taxes") or []:
        if not row.account_head:
            frappe.throw(_("Account Head is mandatory in Taxes and Charges row #{0}").format(row.idx))
        total += flt(row.tax_amount)

    if flt(doc.custom_total_taxes_and_charges) != flt(total):
        doc.custom_total_taxes_and_charges = total


def is_cheque_payment_entry(payment_entry: str) -> bool:
    mode_of_payment = frappe.db.get_value("Payment Entry", payment_entry, "mode_of_payment")
    if not mode_of_payment:
        return False

    return bool(
        frappe.db.exists(
            "Mode of Payment",
            {"name": mode_of_payment, "type": "Cheque"},
        )
    )


def is_bank_account(account):
    return frappe.db.get_value("Account", account, "account_type") == "Bank"


def create_internal_transfer_and_tax_entries(pe_name: str, clearance_date, account_to: str, bank_clearance_doc=None):
    pe = frappe.get_doc("Payment Entry", pe_name)

    if pe.docstatus != 1:
        return None

    existing_pe = frappe.db.exists(
        "Payment Entry",
        {
            "payment_type": "Internal Transfer",
            "reference_no": pe.name,
            "docstatus": ("!=", 2),
        },
    )
    if existing_pe:
        return None

    if pe.party_type == "Customer":
        pdc_account = pe.paid_to
        bank_account = account_to
    elif pe.party_type == "Supplier":
        pdc_account = pe.paid_from
        bank_account = account_to
    else:
        return None

    if not pdc_account or not bank_account:
        return None

    if not is_bank_account(bank_account):
        frappe.throw(_("Selected Account To must be a Bank account"))

    total_tax = flt(bank_clearance_doc.custom_total_taxes_and_charges) if bank_clearance_doc else 0
    original_amount = flt(pe.paid_amount)

    if total_tax < 0:
        frappe.throw(_("Total Taxes and Charges cannot be negative"))

    if total_tax > original_amount:
        frappe.throw(_("Total Taxes and Charges cannot be greater than cleared amount"))

    net_amount = original_amount - total_tax

    internal_pe = frappe.new_doc("Payment Entry")
    internal_pe.payment_type = "Internal Transfer"
    internal_pe.company = pe.company
    internal_pe.posting_date = clearance_date or pe.posting_date

    if pe.party_type == "Customer":
        internal_pe.paid_from = pdc_account
        internal_pe.paid_to = bank_account
    else:
        internal_pe.paid_from = bank_account
        internal_pe.paid_to = pdc_account

    internal_pe.paid_amount = net_amount
    internal_pe.received_amount = net_amount
    internal_pe.reference_no = pe.name
    internal_pe.reference_date = clearance_date
    internal_pe.clearance_date = clearance_date
    internal_pe.remarks = f"Cleared from Bank Clearance against {pe.name} after taxes/charges deduction"
    internal_pe.flags.ignore_permissions = True
    internal_pe.insert()
    internal_pe.submit()

    journal_entry_name = None
    if total_tax:
        je = create_tax_journal_entry(
            pe=pe,
            posting_date=clearance_date or pe.posting_date,
            bank_account=bank_account,
            total_tax=total_tax,
            tax_rows=bank_clearance_doc.get("custom_taxes") if bank_clearance_doc else [],
        )
        journal_entry_name = je.name

    return {
        "payment_entry": internal_pe.name,
        "journal_entry": journal_entry_name,
        "net_amount": net_amount,
        "tax_amount": total_tax,
    }


def create_tax_journal_entry(pe, posting_date, bank_account, total_tax, tax_rows):
    if not tax_rows:
        frappe.throw(_("Please add tax rows before clearance"))

    company = pe.company

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = posting_date
    je.user_remark = f"Bank clearance taxes/charges for Payment Entry {pe.name}"

    je.append("accounts", {
        "account": bank_account,
        "credit_in_account_currency": flt(total_tax),
        "exchange_rate": 1,
    })

    for row in tax_rows:
        amount = flt(row.tax_amount)
        if not amount:
            continue

        je.append("accounts", {
            "account": row.account_head,
            "debit_in_account_currency": amount,
            "exchange_rate": 1,
            "reference_type": "Payment Entry",
            "reference_name": pe.name,
        })

    je.flags.ignore_permissions = True
    je.insert()
    je.submit()
    return je
