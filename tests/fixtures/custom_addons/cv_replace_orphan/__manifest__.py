# WP-14 fixture: extension A replaces group G; extension B targets a descendant
# of the original G — WP-15 will mark B as applied=false reason=replaced_ancestor.
{
    'name': 'cv_replace_orphan',
    'version': '0.1.0',
    'depends': ['cv_basic_form'],
    'data': ['views/partner_orphan.xml'],
    'license': 'OPL-1',
    'installable': True,
}
