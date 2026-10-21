# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/browser/admin/test_lifecycle.py
"""Browser tests for /admin/lifecycle - the module lifecycle page (#380).

Business rules:
  - An admin reaches the page from the sidebar and it renders from the real API.
  - It is visibility only: it says nothing needs approval and offers no
    approve / retire control (ADR-0057).
"""
import re

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.postgres]

URL = "/admin/lifecycle"


class TestLifecyclePage:
    def test_sidebar_link_opens_the_page(self, astro_server, clean_browser, page):
        page.goto(f"{astro_server}/admin/")
        page.wait_for_load_state("load")
        page.get_by_test_id("nav-lifecycle").click()
        page.wait_for_url(re.compile(r".*/admin/lifecycle$"))
        expect(page.get_by_role("heading", name="Module Lifecycle", exact=True)).to_be_visible(
            timeout=5000)

    def test_the_page_renders_and_asks_for_no_approval(self, astro_server, clean_browser, page):
        page.goto(f"{astro_server}{URL}")
        page.wait_for_load_state("load")
        expect(page.get_by_test_id("lifecycle-summary")).to_be_visible(timeout=5000)
        expect(page.get_by_text("nothing waits for approval")).to_be_visible()
        expect(page.get_by_test_id("lifecycle-error")).to_have_count(0)
        controls = page.get_by_role("button", name=re.compile(r"approve|retire|delete", re.I))
        assert controls.count() == 0, "the lifecycle page must not offer approval controls"
