# tests/test_web_ui_browser.py
"""Browser-level E2E tests for Web UI (Playwright headless Chromium).

These tests simulate real user interactions — navigation, form fills, button clicks,
redirect flows — and are intentionally separate from the HTTP-level tests in
test_web_ui_*.py which test handler logic directly.

Fixtures:
  web_ui_server  — session-scoped uvicorn server on 127.0.0.1:8099
  clean_browser  — wipes all Web UI tables before/after each test
  page           — pytest-playwright function-scoped Playwright page
"""
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


class TestDashboard:
    def test_title_and_nav_links(self, web_ui_server, clean_browser, page):
        page.goto(web_ui_server)
        assert "Dashboard" in page.title()
        assert page.locator("nav a:has-text('API Keys')").is_visible()
        assert page.locator("nav a:has-text('SSH Keys')").is_visible()
        assert page.locator("nav a:has-text('Repos')").is_visible()

    def test_empty_state_has_link_to_repos(self, web_ui_server, clean_browser, page):
        page.goto(web_ui_server)
        assert page.locator("a[href='/repos']").first.is_visible()

    def test_stat_grid_shows_zeros_on_empty_db(self, web_ui_server, clean_browser, page):
        page.goto(web_ui_server)
        stats = page.locator(".stat .number").all_inner_texts()
        assert all(s.strip() == "0" for s in stats), f"Expected all zeros, got {stats}"


class TestNavigation:
    def test_nav_links_reach_all_pages(self, web_ui_server, clean_browser, page):
        page.goto(web_ui_server)

        page.click("nav a:has-text('API Keys')")
        page.wait_for_load_state("load")
        assert "/api-keys" in page.url

        page.click("nav a:has-text('SSH Keys')")
        page.wait_for_load_state("load")
        assert "/ssh-keys" in page.url

        page.click("nav a:has-text('Repos')")
        page.wait_for_load_state("load")
        assert "/repos" in page.url

        page.click("nav a:has-text('Dashboard')")
        page.wait_for_load_state("load")
        assert page.url.rstrip("/") == web_ui_server.rstrip("/")

    def test_active_class_set_on_current_page(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/api-keys")
        assert "API Keys" in page.locator("nav a.active").inner_text()


class TestApiKeysPage:
    def test_empty_state_message(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/api-keys")
        assert page.locator("text=No API keys yet").is_visible()

    def test_create_form_visible_with_required_field(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/api-keys")
        name_input = page.locator("input[name='name']")
        assert name_input.is_visible()
        assert page.locator("button:has-text('Create')").is_visible()
        assert name_input.get_attribute("required") is not None

    def test_create_key_shows_raw_key_once(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/api-keys")
        page.fill("input[name='name']", "browser-key-1")
        page.click("button:has-text('Create')")
        page.wait_for_load_state("load")
        content = page.content()
        assert "osm_" in content
        assert "not be shown again" in content or "copy it now" in content.lower()

    def test_created_key_appears_in_table_as_active(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/api-keys")
        page.fill("input[name='name']", "my-browser-key")
        page.click("button:has-text('Create')")
        page.wait_for_load_state("load")
        assert "my-browser-key" in page.content()
        assert page.locator(".badge-ok").is_visible()

    def test_deactivate_changes_badge_and_removes_button(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/api-keys")
        page.fill("input[name='name']", "key-to-deact")
        page.click("button:has-text('Create')")
        page.wait_for_load_state("load")

        assert page.locator("button:has-text('Deactivate')").is_visible()
        page.click("button:has-text('Deactivate')")
        page.wait_for_load_state("load")

        assert page.locator(".badge-error:has-text('inactive')").is_visible()
        assert not page.locator("button:has-text('Deactivate')").is_visible()


class TestReposPage:
    def test_empty_state_and_add_profile_form(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/repos")
        assert page.locator("text=No profiles yet").is_visible()
        assert page.locator("input[name='name']").is_visible()
        assert page.locator("input[name='version']").is_visible()

    def test_add_repo_form_hidden_without_profile(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/repos")
        assert page.locator("text=Add a profile first").is_visible()
        assert not page.locator("select[name='profile']").is_visible()

    def test_add_profile_shows_in_page(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/repos")
        page.fill("input[name='name']", "viindoo17")
        page.fill("input[name='version']", "17.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")
        assert "viindoo17" in page.content()
        assert "17.0" in page.content()

    def test_add_profile_reveals_repo_form_with_select(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/repos")
        page.fill("input[name='name']", "odoo17")
        page.fill("input[name='version']", "17.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        select = page.locator("select[name='profile']")
        assert select.is_visible()
        assert "odoo17" in select.inner_html()

    def test_add_repo_to_profile(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/repos")
        page.fill("input[name='name']", "test-profile")
        page.fill("input[name='version']", "16.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        page.fill("input[name='branch']", "16.0")
        page.fill("input[name='local_path']", "/tmp/odoo_16")
        page.click("button:has-text('Add Repo')")
        page.wait_for_load_state("load")

        assert "/tmp/odoo_16" in page.content()

    def test_index_button_visible_after_add_repo(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/repos")
        page.fill("input[name='name']", "myprof")
        page.fill("input[name='version']", "17.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        page.fill("input[name='branch']", "17.0")
        page.fill("input[name='local_path']", "/tmp/myrepo")
        page.click("button:has-text('Add Repo')")
        page.wait_for_load_state("load")

        assert page.locator("button:has-text('Index')").is_visible()


class TestSshKeysPage:
    def test_empty_state(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/ssh-keys")
        assert page.locator("text=No SSH keys stored").is_visible()

    def test_generate_form_visible(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/ssh-keys")
        assert page.locator("input[name='name']").is_visible()
        assert page.locator("button:has-text('Generate Ed25519 Keypair')").is_visible()

    def test_generate_key_shows_public_key(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/ssh-keys")
        page.fill("input[name='name']", "test-ed25519")
        page.click("button:has-text('Generate Ed25519 Keypair')")
        page.wait_for_load_state("load")
        content = page.content()
        assert "ssh-ed25519" in content
        assert "deploy key" in content.lower()

    def test_generated_key_appears_in_table(self, web_ui_server, clean_browser, page):
        page.goto(f"{web_ui_server}/ssh-keys")
        page.fill("input[name='name']", "my-deploy-key")
        page.click("button:has-text('Generate Ed25519 Keypair')")
        page.wait_for_load_state("load")
        assert "my-deploy-key" in page.content()

    def test_delete_key_removes_from_list(self, web_ui_server, clean_browser, page):
        # Generate a key first
        page.goto(f"{web_ui_server}/ssh-keys")
        page.fill("input[name='name']", "to-delete-key")
        page.click("button:has-text('Generate Ed25519 Keypair')")
        page.wait_for_load_state("load")

        # Reload to get clean list view (POST response shows key, GET shows table)
        page.goto(f"{web_ui_server}/ssh-keys")
        assert "to-delete-key" in page.content()

        # Accept the JS confirm dialog before clicking Delete
        page.on("dialog", lambda d: d.accept())
        page.click("button:has-text('Delete')")
        page.wait_for_load_state("load")

        assert "to-delete-key" not in page.content()
