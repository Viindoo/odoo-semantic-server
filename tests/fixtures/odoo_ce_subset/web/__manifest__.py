# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
# WP-7 fixture: trimmed deps to subset only.
# WP-14: added views/*.xml (frozen copies from Odoo CE 17.0).

{
    'name': 'Web',
    'category': 'Hidden',
    'version': '1.0',
    'depends': ['base'],
    'data': [
        'views/base_document_layout_views.xml',
        'views/partner_view.xml',
    ],
    'installable': True,
    'auto_install': True,
    'license': 'LGPL-3',
}
