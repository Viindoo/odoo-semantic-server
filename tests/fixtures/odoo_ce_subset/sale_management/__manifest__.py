# WP-7 fixture: trimmed deps to subset only.
# WP-14: added views/*.xml (frozen copies from Odoo CE 17.0).
{
    'name': 'Sales Management',
    'version': '1.0',
    'category': 'Sales/Sales',
    'depends': ['sale'],
    'data': [
        'views/digest_views.xml',
        'views/res_config_settings_views.xml',
        'views/sale_order_template_views.xml',
        'views/sale_order_views.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
