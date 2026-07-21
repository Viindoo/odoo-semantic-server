# SPDX-License-Identifier: AGPL-3.0-or-later
# Verbatim add_option() argument-list excerpt from real Odoo 19.0
# odoo/tools/config.py - CI-layer content-parity fixture (issue #364 S3).
# Extraction method + provenance: see this dir's README.md. Argument text is
# copied byte-for-byte from real source (via the production
# _find_option_call_arg_spans span-finder) and wrapped in a bare
# `group.add_option(...)` statement - this file is never imported/executed,
# only ast.parse'd, so `group` need not be a real object.

group.add_option("--addons-path", dest="addons_path", type='addons_path', metavar='PATH,...', my_default=[],
                         help="specify additional addons paths (separated by commas).")

group.add_option("-D", "--data-dir", dest="data_dir", type='path',  # sensitive default set in _load_default_options
                         help="Directory where to store Odoo data")

group.add_option("-d", "--database", dest="db_name", type='comma', metavar="DATABASE,...", my_default=[], env_name='PGDATABASE',
                         help="database(s) used when installing or updating modules.")

group.add_option("--db-filter", dest="dbfilter", my_default='', metavar="REGEXP",
                         help="Regular expressions for filtering available databases for Web UI. "
                              "The expression can use %d (domain) and %h (host) placeholders.")

group.add_option("--db_app_name", dest="db_app_name", my_default="odoo-{pid}", env_name='PGAPPNAME',
                         help="specify the application name in the database, {pid} is substituted by the process pid")

group.add_option("-c", "--config", dest="config", type='path', file_loadable=False, env_name='ODOO_RC',
                         help="specify alternate config file")

