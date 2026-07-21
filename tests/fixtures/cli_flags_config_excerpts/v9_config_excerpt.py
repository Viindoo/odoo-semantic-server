# SPDX-License-Identifier: AGPL-3.0-or-later
# Verbatim add_option() argument-list excerpt from real Odoo 9.0
# openerp/tools/config.py - CI-layer content-parity fixture (issue #364 S3).
# Extraction method + provenance: see this dir's README.md. Argument text is
# copied byte-for-byte from real source (via the production
# _find_option_call_arg_spans span-finder) and wrapped in a bare
# `group.add_option(...)` statement - this file is never imported/executed,
# only ast.parse'd, so `group` need not be a real object.

group.add_option("--addons-path", dest="addons_path",
                         help="specify additional addons paths (separated by commas).",
                         action="callback", callback=self._check_addons_path, nargs=1, type="string")

group.add_option("-D", "--data-dir", dest="data_dir", my_default=_get_default_datadir(),
                         help="Directory where to store Odoo data")

group.add_option("-d", "--database", dest="db_name", my_default=False,
                         help="specify the database name")

group.add_option("--db_host", dest="db_host", my_default=False,
                         help="specify the database host")

group.add_option("-w", "--db_password", dest="db_password", my_default=False,
                         help="specify the database password")

group.add_option("-c", "--config", dest="config", help="specify alternate config file")

