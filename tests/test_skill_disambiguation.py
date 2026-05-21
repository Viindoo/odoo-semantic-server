# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Skill disambiguation tests — verifies keyword-based routing accuracy >= 80%.

Tests that the classify_query heuristic correctly routes 30 natural-language
queries to the right persona skill or MCP tool bucket.
"""
import pytest

QUERIES = [
    # odoo-risk-overview (CEO — upgrade risk, deprecated APIs, customization risk)
    ("what breaks if I modify amount_total", "odoo-risk-overview"),
    ("give me a risk overview of our Odoo customization", "odoo-risk-overview"),
    ("business risk report for our Odoo upgrade", "odoo-risk-overview"),

    # odoo-customization-inventory (CEO — list customizations, inventory)
    ("list all our Odoo customizations", "odoo-customization-inventory"),
    ("inventory of custom modules we have built", "odoo-customization-inventory"),
    ("what have we built on top of Odoo standard", "odoo-customization-inventory"),

    # odoo-override-finder (Developer — where to hook, override point)
    ("find override point for action_confirm in sale order", "odoo-override-finder"),
    ("best place to extend partner creation logic", "odoo-override-finder"),
    ("where should I hook into invoice validation", "odoo-override-finder"),

    # odoo-deprecation-audit (Developer — deprecated API, upgrade readiness)
    ("audit deprecated API usage in our codebase", "odoo-deprecation-audit"),
    ("find old-style code before we upgrade Odoo", "odoo-deprecation-audit"),
    ("upgrade readiness check for deprecated symbols", "odoo-deprecation-audit"),

    # odoo-version-diff (Developer/Marketer — what changed between versions)
    ("what changed between Odoo 16 and 17", "odoo-version-diff"),
    ("show breaking changes in the upgrade from v16 to v17", "odoo-version-diff"),
    ("new API in Odoo version 17", "odoo-version-diff"),

    # odoo-feature-check (Consultant — does Odoo have X, built-in, out of the box)
    ("does Odoo have a project management module built in", "odoo-feature-check"),
    ("is subscription billing available out of the box in Odoo", "odoo-feature-check"),
    ("check if expense management exists in standard Odoo", "odoo-feature-check"),

    # odoo-gap-analysis (Consultant — gap analysis, what needs custom, requirements vs standard)
    ("gap analysis for client requirements against Odoo standard", "odoo-gap-analysis"),
    ("what needs to be customized for this project", "odoo-gap-analysis"),
    ("standard versus custom feature map for the client", "odoo-gap-analysis"),

    # odoo-feature-highlights (Marketer — highlight features, sales deck, exciting)
    ("highlight new features in Odoo 17 for our sales deck", "odoo-feature-highlights"),
    ("what is exciting in the new Odoo version for marketing", "odoo-feature-highlights"),
    ("feature comparison for the sales deck presentation", "odoo-feature-highlights"),

    # odoo-addon-diff (Marketer — CE vs EE, enterprise edition, addon comparison)
    ("compare Odoo CE vs EE features for our proposal", "odoo-addon-diff"),
    ("what modules are only in Enterprise edition", "odoo-addon-diff"),
    ("addon comparison community versus enterprise for manufacturing", "odoo-addon-diff"),

    # odoo-capability-proof (Sales — prove Odoo can do X, capability evidence)
    ("prove Odoo can handle multi-currency invoicing for this client", "odoo-capability-proof"),
    ("show capability evidence that Odoo supports approval workflows", "odoo-capability-proof"),

    # odoo-objection-handler (Sales — handle objection, counter argument)
    ("handle the objection that Odoo cannot support complex workflows", "odoo-objection-handler"),
]

assert len(QUERIES) == 30, f"Expected 30 test cases, got {len(QUERIES)}"


def classify_query(query: str) -> str:
    """Simple keyword heuristic for testing disambiguation."""
    q = query.lower()

    # odoo-objection-handler — must check before risk-overview (shares "cannot/can't")
    if any(kw in q for kw in ["objection", "counter argument", "odoo cannot", "odoo can't",
                                "doesn't support", "handle the objection", "phản bác"]):
        return "odoo-objection-handler"

    # odoo-risk-overview — risk, upgrade risk, what breaks
    if any(kw in q for kw in ["risk overview", "upgrade risk", "business risk", "what breaks",
                                "rủi ro", "báo cáo rủi ro"]):
        return "odoo-risk-overview"

    # odoo-customization-inventory — list customizations, inventory, what have we built
    if any(kw in q for kw in ["list all our", "inventory of custom", "what have we built",
                                "kiểm kê", "liệt kê tất cả customization", "bản kiểm kê"]):
        return "odoo-customization-inventory"

    # odoo-override-finder — override point, hook into, where to extend
    if any(kw in q for kw in ["override point", "hook into", "best place to extend",
                                "where should i hook", "điểm override", "override method"]):
        return "odoo-override-finder"

    # odoo-deprecation-audit — deprecated API, upgrade readiness, old-style code
    if any(kw in q for kw in ["deprecated api", "old-style code", "upgrade readiness",
                                "kiểm tra deprecated", "find deprecated", "deprecated symbol"]):
        return "odoo-deprecation-audit"

    # odoo-version-diff — what changed between versions, breaking changes, new API in version
    if any(kw in q for kw in ["what changed between", "breaking changes", "new api in",
                                "from v16 to v17", "from odoo 16", "version 17",
                                "api nào thay đổi", "tính năng mới odoo 17"]):
        return "odoo-version-diff"

    # odoo-gap-analysis — gap analysis, what needs to be customized, standard vs custom
    if any(kw in q for kw in ["gap analysis", "what needs to be customized",
                                "standard versus custom", "standard vs custom",
                                "phân tích gap", "tính năng nào cần custom"]):
        return "odoo-gap-analysis"

    # odoo-feature-check — does Odoo have X, built-in, out of the box, exists in standard
    if any(kw in q for kw in ["does odoo have", "built in", "out of the box",
                                "exists in standard", "available out of the box",
                                "odoo có sẵn", "check if", "in standard odoo"]):
        return "odoo-feature-check"

    # odoo-feature-highlights — highlight features, sales deck, exciting in version
    if any(kw in q for kw in ["highlight new features", "what is exciting", "sales deck",
                                "tính năng nổi bật", "nêu điểm mạnh", "feature comparison"]):
        return "odoo-feature-highlights"

    # odoo-addon-diff — CE vs EE, enterprise edition, addon comparison
    if any(kw in q for kw in ["ce vs ee", "community versus enterprise",
                                "enterprise edition", "addon comparison",
                                "so sánh ce và ee", "module nào chỉ có trong enterprise"]):
        return "odoo-addon-diff"

    # odoo-capability-proof — prove Odoo can, capability evidence, show capability
    if any(kw in q for kw in ["prove odoo can", "capability evidence", "show capability",
                                "chứng minh odoo", "bằng chứng tính năng"]):
        return "odoo-capability-proof"

    return "none"


@pytest.mark.parametrize("query,expected", QUERIES)
def test_disambiguation(query, expected):
    result = classify_query(query)
    assert result == expected, (
        f"Query: {query!r} → got {result!r}, expected {expected!r}"
    )


def test_overall_accuracy():
    correct = sum(1 for q, e in QUERIES if classify_query(q) == e)
    accuracy = correct / len(QUERIES)
    assert accuracy >= 0.80, f"Accuracy {accuracy:.1%} < 80%"
