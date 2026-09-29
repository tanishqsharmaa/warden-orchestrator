import os
import pytest

@pytest.fixture(autouse=True)
def clean_env():
    """Ensure environment variables don't leak across tests."""
    old_env = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(old_env)
