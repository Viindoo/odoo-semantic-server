# Trimmed excerpt of OCA/OpenUpgrade 8.0
# openerp/addons/openupgrade_records/lib/apriori.py @ 7b78addaa126f141dac007212f506610ed6bba4f
# Era shape: renamed_modules dict, NO merged_modules assignment at all.
""" Encode any known changes to the database here
to help the matching process
"""

renamed_modules = {
    'base_calendar': 'calendar',
    'mrp_jit': 'procurement_jit',
    'project_mrp': 'sale_service',
    # OCA/account-invoice-reporting
    'base_condition_template': 'base_comment_template',
    'account_banking_mt940':
        'account_bank_statement_import_mt940_base',
}

renamed_models = {
}
