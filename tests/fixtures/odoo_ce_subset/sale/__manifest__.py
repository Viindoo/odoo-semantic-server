# WP-7 fixture: trimmed deps to subset only (removed sales_team, account_payment, utm).
# WP-14: added views/*.xml (frozen copies from Odoo CE 17.0).
{
    'name': 'Sales',
    'version': '1.2',
    'category': 'Sales/Sales',
    'depends': ['product', 'account', 'mail'],
    'data': [
        'views/account_views.xml',
        'views/crm_team_views.xml',
        'views/mail_activity_plan_views.xml',
        'views/mail_activity_views.xml',
        'views/payment_views.xml',
        'views/product_document_views.xml',
        'views/product_packaging_views.xml',
        'views/product_views.xml',
        'views/res_partner_views.xml',
        'views/sale_order_line_views.xml',
        'views/sale_order_views.xml',
        'views/utm_campaign_views.xml',
    ],
    'installable': True,
    'license': 'LGPL-3',
}
