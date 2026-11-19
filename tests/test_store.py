"""Простые запросы store (rows, row, scalar, execute) на постоянном соединении потока (2.0.62): то же поведение,
что у прежнего «новое соединение на каждый запрос», только без переоткрытия."""

import os
import sqlite3
import threading

import pytest

from opsdash import config, store


@pytest.fixture
def connects(monkeypatch):
    calls = []
    real = store.connect

    def counting():
        calls.append(threading.get_ident())
        return real()

    monkeypatch.setattr(store, "connect", counting)
    store._local.conn = None
    return calls


def test_one_connection_per_thread(connects):
    for i in range(20):
        store.set_state(f"k{i}", i)
        assert store.get_state(f"k{i}") == i
    assert store.scalar("SELECT COUNT(*) FROM state WHERE key LIKE 'k%'") == 20
    assert len(connects) == 1
    seen = []
    t = threading.Thread(target=lambda: seen.append(store.get_state("k3")))
    t.start()
    t.join()
    assert seen == [3] and len(connects) == 2 and connects[0] != connects[1]  # у другого потока — своё


def test_each_call_is_committed_and_visible_to_other_connections():
    store.set_state("x", "1")
    other = sqlite3.connect(str(config.DB_FILE))
    assert other.execute("SELECT value FROM state WHERE key='x'").fetchone() == ('"1"',)
    other.execute("UPDATE state SET value='\"2\"' WHERE key='x'")
    other.commit()
    other.close()
    assert store.get_state("x") == "2"  # чужая запись видна сразу: снимок базы не залип


def test_helper_inside_db_block_is_a_separate_connection_as_before():
    with store.db() as conn:
        conn.execute("INSERT INTO state(key, value) VALUES('inner', '\"y\"')")
        assert store.get_state("inner") is None  # незафиксированное в блоке простой запрос не видит — как раньше
    assert store.get_state("inner") == "y"


def test_row_does_not_keep_a_snapshot():
    for i in range(5):
        store.set_state(f"r{i}", i)
    assert store.row("SELECT * FROM state WHERE key LIKE 'r%' ORDER BY key")["key"] == "r0"  # строк больше одной
    assert store.scalar("SELECT key FROM state WHERE key LIKE 'r%' ORDER BY key") == "r0"
    other = sqlite3.connect(str(config.DB_FILE))
    other.execute("INSERT INTO state(key, value) VALUES('w', '1')")
    other.commit()
    busy, _, _ = other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    other.close()
    assert busy == 0  # журнал сброшен целиком: недочитанных запросов, держащих базу, нет


def test_error_rolls_back_and_connection_keeps_working():
    with pytest.raises(sqlite3.OperationalError):
        store.execute("INSERT INTO no_such_table VALUES(1)")
    store.set_state("dup", 1)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("INSERT INTO state(key, value) VALUES('dup', '2')")
    store.set_state("after", 1)
    assert store.get_state("after") == 1 and store.get_state("dup") == 1


def test_reconnects_when_database_file_is_replaced(connects, tmp_path):
    store.set_state("which", "old")
    copy = tmp_path / "restored.db"
    src = sqlite3.connect(str(config.DB_FILE))
    dst = sqlite3.connect(str(copy))
    src.backup(dst)
    src.close()
    dst.execute("UPDATE state SET value='\"restored\"' WHERE key='which'")
    dst.commit()
    dst.close()
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(str(config.DB_FILE) + suffix)
        except FileNotFoundError:
            pass
    os.replace(copy, config.DB_FILE)  # базу восстановили из копии поверх
    assert store.get_state("which") == "restored"
    assert len(connects) == 2


def test_threads_read_and_write_together():
    errors = []

    def worker(n):
        try:
            for i in range(50):
                store.set_state(f"t{n}", i)
                assert store.get_state(f"t{n}") == i
                store.rows("SELECT key FROM state LIMIT 5")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert [store.get_state(f"t{n}") for n in range(6)] == [49] * 6
