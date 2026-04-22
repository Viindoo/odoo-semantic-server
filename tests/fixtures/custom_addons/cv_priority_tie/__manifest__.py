# WP-14 fixture: two extensions with identical priority — load_order is the tiebreaker.
{
    'name': 'cv_priority_tie',
    'version': '0.1.0',
    'depends': ['cv_basic_form'],
    'data': ['views/partner_tie.xml'],
    'license': 'OPL-1',
    'installable': True,
}
