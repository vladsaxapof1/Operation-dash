import os
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

# отдельная папка данных для тестов — до импорта opsdash
_HOME = Path(tempfile.mkdtemp(prefix="opsdash-test-"))
os.environ["OPSDASH_HOME"] = str(_HOME)

from opsdash import config, store  # noqa: E402


@pytest.fixture(autouse=True)
def clean_home():
    # задания, запущенные прошлым тестом без очереди (jobs.submit — отдельным потоком), могут ещё писать в базу:
    # если удалить папку под ними, поток создаст схему в старом файле, а новая база останется без таблиц
    for t in threading.enumerate():
        if t is not threading.main_thread() and not t.daemon:
            t.join(timeout=10)
    for sub in ("data", "config", "secrets"):
        shutil.rmtree(_HOME / sub, ignore_errors=True)
    store._initialized.clear()
    config.ensure_dirs()
    # по умолчанию у PULSE новый дизайн (v3); тесты страниц написаны под классическую разметку — она остаётся
    # у тех, кто её выбрал. Новый дизайн проверяют tests/test_design_v3.py
    config.update_settings("web", {"design": "classic"})
    from opsdash import updates

    updates._stale.update(at=0.0, value=None)  # «файлы новее запущенного» — проверка заново в каждом тесте
    yield _HOME


@pytest.fixture
def settings_with(clean_home):
    def apply(**sections):
        for name, values in sections.items():
            config.update_settings(name, values)
        return config.load_settings()

    return apply
