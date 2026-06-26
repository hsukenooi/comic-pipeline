import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: tests that hit real Gixen")


@pytest.fixture(autouse=True)
def _clear_server_url_env(monkeypatch):
    """Make tests hermetic w.r.t. the ambient server URL (BUI-220).

    A dev/CI shell commonly exports GIXEN_SERVER_URL (the deprecated alias), and
    the CLI now emits a one-line deprecation warning to stderr the first time it
    falls back to that alias. CliRunner mixes stderr into captured output, so an
    ambient alias would nondeterministically pollute whichever test runs first.
    Clear both vars before every test; tests that need them set them explicitly.
    """
    monkeypatch.delenv("COMICS_SERVER_URL", raising=False)
    monkeypatch.delenv("GIXEN_SERVER_URL", raising=False)
