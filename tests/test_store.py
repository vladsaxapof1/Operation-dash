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
    store.close_all()
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


@pytest.mark.skipif(os.name == "nt", reason="на Windows открытый файл базы не подменить — сначала store.close_all()")
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


def test_close_all_releases_connections_of_every_thread(connects):
    store.set_state("a", 1)
    ready, go = threading.Event(), threading.Event()

    def worker():  # поток держит своё постоянное соединение и ждёт
        store.get_state("a")
        ready.set()
        go.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    ready.wait(5)
    assert len(store._open) == 2  # главный поток и рабочий
    store.close_all()
    assert not store._open  # файл базы свободен: на Windows его теперь можно удалить
    go.set()
    t.join()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(config.DB_FILE) + suffix)
        except FileNotFoundError:
            pass
    store._initialized.clear()
    store.set_state("b", 2)  # новая база с нуля, старое соединение не мешает
    assert store.get_state("b") == 2 and store.get_state("a") is None


def test_connections_of_finished_threads_are_closed():
    store.close_all()
    for _ in range(20):
        t = threading.Thread(target=lambda: store.get_state("x"))
        t.start()
        t.join()
    assert len(store._open) <= 1  # остаётся только последний: прежние закрыты при регистрации следующих
