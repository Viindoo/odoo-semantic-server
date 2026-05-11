# tests/test_clone_poll_timeout.py
"""M7-C2: Clone-status polling timeout feature tests.

Static code verification tests (no browser or DB needed).
Verify that repos.html includes MAX_TICKS constant and timeout logic.
"""
import pathlib


class TestReposClonePollTimeout:
    """M7-C2: Clone-status polling caps at 72 ticks (6 min)."""

    def test_clone_poll_max_ticks_constant_present(self):
        """Verify repos.html has MAX_TICKS constant = 72."""
        repos_html = pathlib.Path(
            "src/web_ui/templates/repos.html"
        ).read_text()
        assert "MAX_TICKS = 72" in repos_html
        assert "data-poll-ticks" in repos_html

    def test_clone_poll_timeout_message_in_code(self):
        """Verify timeout message HTML is in repos.html."""
        repos_html = pathlib.Path(
            "src/web_ui/templates/repos.html"
        ).read_text()
        assert "Polling timed out" in repos_html
        assert "check server logs" in repos_html

    def test_clone_poll_tick_increment_logic(self):
        """Verify tick counter increments in pollCloneCells()."""
        repos_html = pathlib.Path(
            "src/web_ui/templates/repos.html"
        ).read_text()
        assert "data-poll-ticks" in repos_html
        assert "parseInt" in repos_html
        assert "setAttribute('data-poll-ticks'" in repos_html
        assert ">= MAX_TICKS" in repos_html or "ticks >= MAX_TICKS" in repos_html

    def test_clone_poll_returns_early_when_stuck(self):
        """Verify fetch is not called when tick limit exceeded."""
        repos_html = pathlib.Path(
            "src/web_ui/templates/repos.html"
        ).read_text()
        # Verify that return statement exists before fetch in the timeout case
        lines = repos_html.split("\n")
        poll_func_start = None
        for i, line in enumerate(lines):
            if "function pollCloneCells()" in line:
                poll_func_start = i
                break

        assert poll_func_start is not None, "pollCloneCells function not found"

        # Check that ticks >= MAX_TICKS check comes before fetch
        ticks_check_idx = None
        fetch_idx = None
        for i in range(poll_func_start, len(lines)):
            if ">= MAX_TICKS" in lines[i] or "ticks >= MAX_TICKS" in lines[i]:
                ticks_check_idx = i
            if "fetch(" in lines[i]:
                fetch_idx = i
                break

        assert ticks_check_idx is not None, "Tick limit check not found"
        assert fetch_idx is not None, "fetch call not found"
        assert (
            ticks_check_idx < fetch_idx
        ), "Tick check should come before fetch call"
