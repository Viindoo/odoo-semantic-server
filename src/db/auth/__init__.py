# SPDX-License-Identifier: AGPL-3.0-or-later
"""AuthStore mixin package.

`src.db.auth_registry.AuthStore` is composed from the domain mixins defined in
this package (api_key / ssh / user / tenant / feedback). Each mixin file holds
one domain's SQL methods and relies on `self._pool` (a `PgPool`) being set by
`AuthStore.__init__`. Mixins never instantiate or import `auth_registry`, so a
bare `import src.db.auth.<mixin>` is cycle-free.
"""
