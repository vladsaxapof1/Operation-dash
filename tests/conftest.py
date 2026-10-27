import os
import shutil
import tempfile
from pathlib import Path

import pytest

# отдельная папка данных для тестов — до импорта opsdash
_HOME = Path(tempfile.mkdtemp(prefix="opsdash-test-"))
os.environ["OPSDASH_HOME"] = str(_HOME)

from opsdash import config, store  # noqa: E402


@pytest.fixture(autouse=True)
def clean_home():
    for sub in ("data", "config", "secrets"):
        shutil.rmtree(_HOME / sub, ignore_errors=True)
    store._initialized.clear()
    config.ensure_dirs()
    yield _HOME


@pytest.fixture
def settings_with(clean_home):
    def apply(**sections):
        for name, values in sections.items():
            config.update_settings(name, values)
        return config.load_settings()

    return apply
