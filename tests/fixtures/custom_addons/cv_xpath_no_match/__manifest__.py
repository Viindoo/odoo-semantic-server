# WP-14 fixture: extension whose XPath expression matches nothing.
# WP-15 will surface this as applied=false reason=xpath_no_match; parser just records it.
{
    'name': 'cv_xpath_no_match',
    'version': '0.1.0',
    'depends': ['cv_basic_form'],
    'data': ['views/partner_nomatch.xml'],
    'license': 'OPL-1',
    'installable': True,
}
