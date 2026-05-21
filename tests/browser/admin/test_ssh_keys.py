# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/browser/admin/test_ssh_keys.py
"""Browser tests for /admin/ssh-keys page (M8 W7).

Consolidated from TestSshKeysPage in tests/test_web_ui_browser.py.
URL: /admin/ssh-keys (was /ssh-keys).
Selectors: data-testid (was #generate-form, #import-form, text=...).
"""
import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.postgres]

SSH_KEYS_URL = "/admin/ssh-keys"


class TestSshKeysPage:
    def test_empty_state_visible(self, astro_server, clean_browser, page):
        """GET /admin/ssh-keys → ssh-keys-empty-state visible."""
        page.goto(f"{astro_server}{SSH_KEYS_URL}")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("ssh-keys-empty-state")).to_be_visible(timeout=5000)

    def test_generate_form_visible(self, astro_server, clean_browser, page):
        """GET /admin/ssh-keys → generate form and button visible."""
        page.goto(f"{astro_server}{SSH_KEYS_URL}")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("generate-ssh-key-form")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("ssh-key-name-input")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("generate-ssh-key-button")).to_be_visible(timeout=5000)

    def test_import_form_visible(self, astro_server, clean_browser, page):
        """GET /admin/ssh-keys → import form visible."""
        page.goto(f"{astro_server}{SSH_KEYS_URL}")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("import-ssh-key-form")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("ssh-import-name-input")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("ssh-import-pem-input")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("import-ssh-key-button")).to_be_visible(timeout=5000)

    def test_generate_key_shows_public_key_banner(self, astro_server, clean_browser, page):
        """Fill name, click generate → new-pubkey-banner visible with ssh-ed25519.

        The generate handler is slow on CI (Ed25519 keypair + Fernet encryption
        + DB INSERT + 800ms reload timer). Use expect's auto-wait with a
        generous timeout instead of a fixed sleep that races the network.
        """
        page.goto(f"{astro_server}{SSH_KEYS_URL}")
        page.wait_for_load_state("load")

        page.get_by_test_id("ssh-key-name-input").fill("test-ed25519-br")
        page.get_by_test_id("generate-ssh-key-button").click()

        expect(page.get_by_test_id("new-pubkey-banner")).to_be_visible(timeout=10000)
        pubkey_text = page.get_by_test_id("new-pubkey-value").inner_text()
        assert "ssh-ed25519" in pubkey_text

    def test_generated_key_appears_in_table(self, astro_server, clean_browser, page):
        """Generated key → ssh-key-row appears in the list."""
        page.goto(f"{astro_server}{SSH_KEYS_URL}")
        page.wait_for_load_state("load")

        page.get_by_test_id("ssh-key-name-input").fill("my-deploy-key-br")
        page.get_by_test_id("generate-ssh-key-button").click()
        # Wait for the banner so we know save_ssh_key succeeded before reloading.
        expect(page.get_by_test_id("new-pubkey-banner")).to_be_visible(timeout=10000)

        # Reload to get clean list view (post-create shows banner, GET shows table)
        page.goto(f"{astro_server}{SSH_KEYS_URL}")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("ssh-key-row").first).to_be_visible(timeout=5000)

    def test_import_existing_keypair_shows_pubkey_banner(
        self, astro_server, clean_browser, page
    ):
        """Import a PEM private key → new-pubkey-banner visible with ssh-ed25519."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        private_key = Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.OpenSSH,
            encryption_algorithm=NoEncryption(),
        ).decode()

        page.goto(f"{astro_server}{SSH_KEYS_URL}")
        page.wait_for_load_state("load")

        page.get_by_test_id("ssh-import-name-input").fill("imported-ed25519-br")
        page.get_by_test_id("ssh-import-pem-input").fill(pem)
        page.get_by_test_id("import-ssh-key-button").click()

        expect(page.get_by_test_id("new-pubkey-banner")).to_be_visible(timeout=10000)
        pubkey_text = page.get_by_test_id("new-pubkey-value").inner_text()
        assert "ssh-ed25519" in pubkey_text

    def test_delete_key_removes_from_list(self, astro_server, clean_browser, page):
        """Generate key, reload, click delete → ssh-key-row disappears."""
        page.goto(f"{astro_server}{SSH_KEYS_URL}")
        page.wait_for_load_state("load")

        page.get_by_test_id("ssh-key-name-input").fill("to-delete-key-br")
        page.get_by_test_id("generate-ssh-key-button").click()
        expect(page.get_by_test_id("new-pubkey-banner")).to_be_visible(timeout=10000)

        # Reload to get table view
        page.goto(f"{astro_server}{SSH_KEYS_URL}")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("ssh-key-row").first).to_be_visible(timeout=5000)

        page.on("dialog", lambda d: d.accept())
        page.locator('[data-testid^="delete-ssh-key-button-"]').first.click()
        expect(page.get_by_test_id("ssh-key-row")).not_to_be_visible(timeout=8000)
