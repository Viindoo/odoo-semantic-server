# SPDX-License-Identifier: AGPL-3.0-or-later
# Verbatim add_option() argument-list excerpt from real Odoo 20.0
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

group.add_option("--http-interface", dest="http_interface", my_default='127.0.0.1',
                         help="Listen interface address for HTTP services.")

group.add_option("--syslog", action="store_true", dest="syslog", my_default=False,
                         help="Send the log to the syslog server (deprecated)")

group.add_option("-d", "--database", dest="db_name", type='comma', metavar="DATABASE,...", my_default=[], env_name='PGDATABASE',
                         help="database(s) used when installing or updating modules.")

group.add_option("--stop", "--stop-after-init", action="store_true", dest="stop_after_init", my_default=False, file_exportable=False, file_loadable=False,
                         help="stop the server after its initialization")
