import frappe
from frappe import _
from frappe.utils import flt
from erpnext.accounts.doctype.bank_clearance.bank_clearance import (
    BankClearance as ERPNextBankClearance,
)


class BankClearance(ERPNextBankClearance):

    @frappe.whitelist()
    def update_clearance_date(self):
        selected_card_entries = []
        selected_cheque_entries = []

        has_card_or_cheque_payment = any(
            d.clearance_date
            and d.payment_document == "Payment Entry"
            and get_payment_entry_type(d.payment_entry) in ("Card", "Cheque")
            for d in self.get("payment_entries")
        )

        if has_card_or_cheque_payment and not self.custom_account_to:
            frappe.throw(_("Please select Account To (Bank) in Bank Clearance"))

        validate_tax_rows(self)

        for d in self.get("payment_entries"):
            if not (
                d.clearance_date
                and d.payment_document == "Payment Entry"
                and d.payment_entry
            ):
                continue

            payment_type = get_payment_entry_type(d.payment_entry)

            if payment_type == "Card":
                selected_card_entries.append({
                    "payment_entry": d.payment_entry,
                    "clearance_date": d.clearance_date,
                })

            elif payment_type == "Cheque":
                selected_cheque_entries.append({
                    "payment_entry": d.payment_entry,
                    "clearance_date": d.clearance_date,
                })

        # CARD -> grouped logic
        # 1 Internal Transfer PE for net cleared amount
        # 1 JE for charges + VAT, crediting source card/visa account
        if selected_card_entries:
            result = create_internal_transfer_and_tax_entries_grouped(
                selected_entries=selected_card_entries,
                account_to=self.custom_account_to,
                bank_clearance_doc=self,
            )

            if result:
                self.db_set("custom_cleared_amount_after_tax", result.get("net_amount"))
                if result.get("journal_entry"):
                    self.db_set("custom_journal_entry_for_tax", result.get("journal_entry"))

        # CHEQUE -> old logic (one PE per cleared entry, no grouped charges logic)
        for row in selected_cheque_entries:
            create_internal_transfer_for_single_entry(
                payment_entry_name=row.get("payment_entry"),
                clearance_date=row.get("clearance_date"),
                account_to=self.custom_account_to,
            )

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


def get_payment_entry_type(payment_entry: str):
    mode_of_payment = frappe.db.get_value("Payment Entry", payment_entry, "mode_of_payment")
    if not mode_of_payment:
        return None

    mop_meta = frappe.get_meta("Mode of Payment")

    # Prefer custom field if created
    if mop_meta.has_field("custom_payment_type"):
        custom_type = frappe.db.get_value("Mode of Payment", mode_of_payment, "custom_payment_type")
        if custom_type:
            return custom_type

    # Fallback to standard type
    if mop_meta.has_field("type"):
        return frappe.db.get_value("Mode of Payment", mode_of_payment, "type")

    return None


def is_bank_account(account):
    return frappe.db.get_value("Account", account, "account_type") == "Bank"


def create_internal_transfer_for_single_entry(payment_entry_name: str, clearance_date=None, account_to: str = None):
    pe = frappe.get_doc("Payment Entry", payment_entry_name)

    if pe.docstatus != 1:
        return None

    existing_pe = frappe.db.exists(
        "Payment Entry",
        {
            "payment_type": "Internal Transfer",
            "reference_no": ["like", f"%{pe.name}%"],
            "docstatus": ("!=", 2),
        },
    )
    if existing_pe:
        return None

    if pe.party_type == "Customer":
        pdc_account = pe.paid_to
        bank_account = account_to
        if not pdc_account or not bank_account:
            return None

        if not is_bank_account(bank_account):
            frappe.throw(_("Selected Account To must be a Bank account"))

        paid_from = pdc_account
        paid_to = bank_account

    elif pe.party_type == "Supplier":
        pdc_account = pe.paid_from
        bank_account = account_to
        if not pdc_account or not bank_account:
            return None

        if not is_bank_account(bank_account):
            frappe.throw(_("Selected Account To must be a Bank account"))

        paid_from = bank_account
        paid_to = pdc_account

    else:
        return None

    posting_date = clearance_date or pe.posting_date

    internal_pe = frappe.new_doc("Payment Entry")
    internal_pe.payment_type = "Internal Transfer"
    internal_pe.company = pe.company
    internal_pe.posting_date = posting_date
    internal_pe.paid_from = paid_from
    internal_pe.paid_to = paid_to
    internal_pe.paid_amount = flt(pe.paid_amount)
    internal_pe.received_amount = flt(pe.paid_amount)
    internal_pe.reference_no = pe.name
    internal_pe.reference_date = posting_date
    internal_pe.clearance_date = posting_date
    internal_pe.remarks = f"Cleared from Bank Clearance against Payment Entry {pe.name}"
    internal_pe.flags.ignore_permissions = True
    internal_pe.insert()
    internal_pe.submit()

    return internal_pe.name


def create_internal_transfer_and_tax_entries_grouped(selected_entries, account_to: str, bank_clearance_doc=None):
    if not selected_entries:
        return None

    pe_docs = []
    clearance_dates = []

    for row in selected_entries:
        pe = frappe.get_doc("Payment Entry", row.get("payment_entry"))

        if pe.docstatus != 1:
            continue

        existing_pe = frappe.db.exists(
            "Payment Entry",
            {
                "payment_type": "Internal Transfer",
                "reference_no": ["like", f"%{pe.name}%"],
                "docstatus": ("!=", 2),
            },
        )
        if existing_pe:
            continue

        pe_docs.append(pe)
        clearance_dates.append(row.get("clearance_date"))

    if not pe_docs:
        return None

    first_pe = pe_docs[0]

    for pe in pe_docs:
        if pe.company != first_pe.company:
            frappe.throw(_("All selected Card Payment Entries must belong to the same Company"))

        if pe.party_type != first_pe.party_type:
            frappe.throw(_("All selected Card Payment Entries must have the same Party Type"))

    if first_pe.party_type == "Customer":
        source_account = first_pe.paid_to
        bank_account = account_to
        for pe in pe_docs:
            if pe.paid_to != source_account:
                frappe.throw(_("All selected Card Payment Entries must have the same Paid To account"))

    elif first_pe.party_type == "Supplier":
        source_account = first_pe.paid_from
        bank_account = account_to
        for pe in pe_docs:
            if pe.paid_from != source_account:
                frappe.throw(_("All selected Card Payment Entries must have the same Paid From account"))
    else:
        return None

    if not source_account or not bank_account:
        return None

    if not is_bank_account(bank_account):
        frappe.throw(_("Selected Account To must be a Bank account"))

    total_tax = flt(bank_clearance_doc.custom_total_taxes_and_charges) if bank_clearance_doc else 0
    original_amount = sum(flt(pe.paid_amount) for pe in pe_docs)

    if total_tax < 0:
        frappe.throw(_("Total Taxes and Charges cannot be negative"))

    if total_tax > original_amount:
        frappe.throw(_("Total Taxes and Charges cannot be greater than cleared amount"))

    net_amount = original_amount - total_tax
    posting_date = clearance_dates[0] or first_pe.posting_date
    reference_no = ", ".join([pe.name for pe in pe_docs])

    # 1) Create Internal Transfer PE only for net bank receipt
    internal_pe = frappe.new_doc("Payment Entry")
    internal_pe.payment_type = "Internal Transfer"
    internal_pe.company = first_pe.company
    internal_pe.posting_date = posting_date

    if first_pe.party_type == "Customer":
        internal_pe.paid_from = source_account
        internal_pe.paid_to = bank_account
    else:
        internal_pe.paid_from = bank_account
        internal_pe.paid_to = source_account

    internal_pe.paid_amount = net_amount
    internal_pe.received_amount = net_amount
    internal_pe.reference_no = reference_no
    internal_pe.reference_date = posting_date
    internal_pe.clearance_date = posting_date
    internal_pe.remarks = (
        f"Cleared from Bank Clearance against Card Payment Entries {reference_no} "
        f"after taxes/charges deduction"
    )
    internal_pe.flags.ignore_permissions = True
    internal_pe.insert()
    internal_pe.submit()

    # 2) Create JE only for tax/charges adjustment
    journal_entry_name = None
    if total_tax:
        je = create_tax_journal_entry(
            pe_list=pe_docs,
            posting_date=posting_date,
            source_account=source_account,
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


def create_tax_journal_entry(pe_list, posting_date, source_account, total_tax, tax_rows):
    if not tax_rows:
        frappe.throw(_("Please add tax rows before clearance"))

    if not source_account:
        frappe.throw(_("Source Card / Visa account is missing"))

    first_pe = pe_list[0]
    company = first_pe.company
    reference_names = ", ".join([pe.name for pe in pe_list])

    debit_total = 0

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = posting_date
    je.user_remark = f"Bank clearance taxes/charges for Card Payment Entries {reference_names}"

    for row in tax_rows:
        amount = flt(row.tax_amount)
        if not amount:
            continue

        debit_total += amount

        je.append("accounts", {
            "account": row.account_head,
            "debit_in_account_currency": amount,
            "exchange_rate": 1,
        })

    if flt(debit_total) <= 0:
        frappe.throw(_("Total tax/charges amount must be greater than zero"))

    # Credit source Visa / Card clearing account only with tax total
    # Bank is already handled in Internal Transfer PE with net amount
    je.append("accounts", {
        "account": source_account,
        "credit_in_account_currency": flt(debit_total),
        "exchange_rate": 1,
    })

    je.flags.ignore_permissions = True
    je.insert()
    je.submit()
    return je