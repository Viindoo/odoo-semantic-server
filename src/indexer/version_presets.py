# SPDX-License-Identifier: AGPL-3.0-or-later
"""Version presets — bundled profile + repo definitions for one-shot setup.

Source date: 2026-05-10. Update when Viindoo addon repo URLs/branches change.
"""

_SOURCE_DATE = "2026-05-10"

PRESETS: dict[str, dict] = {
    "viindoo-17.0": {
        "profile_name": "viindoo17",
        "odoo_version": "17.0",
        "description": "Viindoo addons on Odoo CE 17.0",
        "repos": [
            {
                "url": "https://github.com/odoo/odoo",
                "branch": "17.0",
                "local_path_hint": "~/git/odoo_17.0",
            },
            {
                "url": "https://github.com/Viindoo/tvtmaaddons",
                "branch": "17.0",
                "local_path_hint": "~/git/tvtmaaddons17",
            },
        ],
    },
    "viindoo-18.0": {
        "profile_name": "viindoo18",
        "odoo_version": "18.0",
        "description": "Viindoo addons on Odoo CE 18.0",
        "repos": [
            {
                "url": "https://github.com/odoo/odoo",
                "branch": "18.0",
                "local_path_hint": "~/git/odoo_18.0",
            },
            # Viindoo 18.0 addons repo TBD by upstream.
        ],
    },
}


def list_presets() -> list[str]:
    """Return preset names sorted alphabetically."""
    return sorted(PRESETS.keys())


def load_preset(name: str) -> dict:
    """Return preset dict (deep-copy to prevent mutation)."""
    import copy
    if name not in PRESETS:
        raise KeyError(f"unknown preset: {name!r}; available: {list_presets()}")
    return copy.deepcopy(PRESETS[name])
