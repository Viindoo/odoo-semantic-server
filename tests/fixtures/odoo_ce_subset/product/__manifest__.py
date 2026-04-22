# WP-7 fixture: trimmed deps to subset only (uom removed, keeping base+mail).
# WP-14: added views/*.xml (frozen copies from Odoo CE 17.0).
{
    'name': 'Products & Pricelists',
    'version': '1.2',
    'category': 'Sales/Sales',
    'depends': ['base', 'mail'],
    'data': [
        'views/product_attribute_value_views.xml',
        'views/product_attribute_views.xml',
        'views/product_category_views.xml',
        'views/product_document_views.xml',
        'views/product_packaging_views.xml',
        'views/product_pricelist_item_views.xml',
        'views/product_pricelist_views.xml',
        'views/product_supplierinfo_views.xml',
        'views/product_tag_views.xml',
        'views/product_template_views.xml',
        'views/product_views.xml',
        'views/res_config_settings_views.xml',
        'views/res_country_group_views.xml',
        'views/res_partner_views.xml',
    ],
    'installable': True,
    'license': 'LGPL-3',
}
