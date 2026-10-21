# Trimmed excerpt of OCA/OpenUpgrade 10.0
# odoo/addons/openupgrade_records/lib/apriori.py @ 61af1d5a91
# Era shape: merged_modules is a LIST of (old, new) tuples, with a duplicate
# ('mail_tip' listed twice upstream).
""" Encode any known changes to the database here
to help the matching process
"""

renamed_modules = {
    # OCA/account-invoice-reporting
    'account_invoice_group_picking':
    'account_invoice_report_grouped_by_picking',
    # OCA/connector
    'connector': 'queue_job',
}

merged_modules = [
    ('account_full_reconcile', 'account'),
    ('mail_tip', 'mail'),
    ('project_timesheet', 'hr_timesheet'),
    ('mail_tip', 'mail'),
    ('test_pylint', 'test_lint'),
]

renamed_models = {
}
