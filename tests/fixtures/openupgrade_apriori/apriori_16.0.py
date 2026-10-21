# Trimmed excerpt of OCA/OpenUpgrade 16.0
# openupgrade_scripts/apriori.py @ afa31b1df1
# Era shape: both maps are dicts with double quotes and comments; upstream has
# a stray space in "gift_card ". The dotted "coupon.coupon" entry is NOT
# upstream (it is a model name, from renamed_models): added to prove that a
# non-module name is dropped.
""" Encode any known changes to the database here
to help the matching process
"""

# Renamed modules is a mapping from old module name to new module name
renamed_modules = {
    # odoo
    "coupon": "loyalty",
    "sale_coupon": "sale_loyalty",
    "website_sale_coupon_delivery": "website_sale_loyalty_delivery",
}

# Merged modules contain a mapping from old module names to other,
# preexisting module names
merged_modules = {
    # odoo
    "fetchmail": "mail",
    "gift_card ": "loyalty",
    "pos_gift_card": "pos_loyalty",
    # synthetic (see header)
    "coupon.coupon": "loyalty.card",
}

# only used here for upgrade_analysis
merged_models = {
    # odoo
    "gift.card": "loyalty.card",
}
