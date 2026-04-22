# WP-7 fixture: trimmed deps to subset only.
# WP-14: added views/*.xml (frozen copies from Odoo CE 17.0).
{
    'name': 'Contacts',
    'category': 'Sales/CRM',
    'depends': ['base', 'mail'],
    'data': [
        'views/contact_views.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
