# WP-7 fixture: trimmed deps to subset only (removed sales_team, account_payment, utm).
{
    'name': 'Sales',
    'version': '1.2',
    'category': 'Sales/Sales',
    'depends': ['product', 'account', 'mail'],
    'installable': True,
    'license': 'LGPL-3',
}
