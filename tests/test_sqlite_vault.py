import os
import pickle
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import closing
from datetime import timedelta

import pytest

import veil.vault.sqlite as sqlite_module
from veil import MemoryVault, Shield, ShieldError, SQLiteVault

SRC = os.path.dirname(os.path.dirname(os.path.dirname(sqlite_module.__file__)))


@pytest.fixture
def path(tmp_path):
    return tmp_path / "vault.db"


def open_vault(path, session="default"):
    return SQLiteVault(path, session=session)


class TestPersistence:
    def test_a_new_instance_sees_what_an_earlier_one_stored(self, path):
        with open_vault(path) as first:
            first.get_or_create("jan.n@example.com", "EMAIL")
            first.get_or_create("555-555-0123", "PHONE")
        with open_vault(path) as second:
            assert second.items() == [
                ("[EMAIL_1]", "jan.n@example.com"),
                ("[PHONE_1]", "555-555-0123"),
            ]
            assert second.get_or_create("anna.k@example.com", "EMAIL") == "[EMAIL_2]"

    @pytest.mark.parametrize(
        "value",
        ["a\x00b", "\ud800 lone surrogate", "\U0001f600 emoji", "x" * 100_000],
    )
    def test_any_string_round_trips(self, path, value):
        with open_vault(path) as vault:
            placeholder = vault.get_or_create(value, "V")
        with open_vault(path) as vault:
            assert vault.get_value(placeholder) == value
            assert vault.get_placeholder(value) == placeholder

    def test_remembered_values_persist(self, path):
        with open_vault(path) as vault:
            placeholder = vault.get_or_create("555-555-0123", "PHONE")
            vault._remember("(555) 555-0123", "PHONE", placeholder)
        with open_vault(path) as vault:
            assert vault._remembered() == [("(555) 555-0123", "PHONE", "[PHONE_1]")]

    def test_file_is_owner_only(self, path):
        if os.name != "posix":
            pytest.skip("POSIX permissions")
        with open_vault(path) as vault:
            vault.get_or_create("jan.n@example.com", "EMAIL")
            for name in (path, f"{path}-wal", f"{path}-shm"):
                if os.path.exists(name):
                    assert stat.S_IMODE(os.stat(name).st_mode) == 0o600, name

    def test_existing_file_permissions_are_kept(self, path):
        if os.name != "posix":
            pytest.skip("POSIX permissions")
        path.touch(mode=0o640)
        os.chmod(path, 0o640)
        open_vault(path).close()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o640

    def test_newer_schema_is_refused(self, path):
        open_vault(path).close()
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "UPDATE vault_meta SET value = 99 WHERE key = 'schema_version'"
            )
        with pytest.raises(ValueError, match="schema version 99"):
            open_vault(path)

    def test_closed_vault_cant_be_used(self, path):
        vault = open_vault(path)
        vault.close()
        with pytest.raises(sqlite3.ProgrammingError):
            vault.get_or_create("a@example.com", "EMAIL")

    def test_repr_hides_values_and_session(self, path):
        with open_vault(path, session="jan.n@example.com") as vault:
            vault.get_or_create("555-555-0123", "PHONE")
            assert repr(vault) == "SQLiteVault(<1 values>)"


class TestSessions:
    def test_sessions_are_independent(self, path):
        with open_vault(path, "a") as a, open_vault(path, "b") as b:
            assert a.get_or_create("jan.n@example.com", "EMAIL") == "[EMAIL_1]"
            assert b.get_or_create("anna.k@example.com", "EMAIL") == "[EMAIL_1]"
            assert b.get_placeholder("jan.n@example.com") is None
            a.clear()
            assert len(a) == 0
            assert b.items() == [("[EMAIL_1]", "anna.k@example.com")]

    def test_session_name_must_be_a_non_empty_string(self, path):
        for session in ("", None, 42):
            with pytest.raises(ValueError, match="session"):
                SQLiteVault(path, session=session)

    def test_purge_deletes_sessions_unused_for_a_while(self, path, monkeypatch):
        now = time.time()
        monkeypatch.setattr(sqlite_module.time, "time", lambda: now - 40 * 86400)
        with open_vault(path, "old") as old:
            old.get_or_create("jan.n@example.com", "EMAIL")
        monkeypatch.setattr(sqlite_module.time, "time", lambda: now)
        with open_vault(path, "new") as new:
            new.get_or_create("anna.k@example.com", "EMAIL")
            assert new.purge(timedelta(days=30)) == 1
            assert new.items() == [("[EMAIL_1]", "anna.k@example.com")]
        with open_vault(path, "old") as old:
            assert len(old) == 0

    def test_purging_its_own_session_empties_the_vault(self, path, monkeypatch):
        with open_vault(path) as vault:
            vault.get_or_create("jan.n@example.com", "EMAIL")
            later = time.time() + 3600
            monkeypatch.setattr(sqlite_module.time, "time", lambda: later)
            assert vault.purge(timedelta(minutes=1)) == 1
            assert len(vault) == 0
            assert vault.get_or_create("anna.k@example.com", "EMAIL") == "[EMAIL_1]"

    def test_opening_a_session_counts_as_using_it(self, path, monkeypatch):
        now = time.time()
        monkeypatch.setattr(sqlite_module.time, "time", lambda: now - 40 * 86400)
        open_vault(path, "chat").close()
        monkeypatch.setattr(sqlite_module.time, "time", lambda: now)
        with open_vault(path, "chat") as vault:  # opened again: used now
            assert vault.purge(timedelta(days=30)) == 0


class TestSharing:
    """Several vault objects (in one process or several) on one file."""

    def test_another_instance_sees_new_values_and_clears(self, path):
        with open_vault(path) as a, open_vault(path) as b:
            a.get_or_create("jan.n@example.com", "EMAIL")
            assert b.get_value("[EMAIL_1]") == "jan.n@example.com"
            assert b.get_or_create("jan.n@example.com", "PERSON") == "[EMAIL_1]"
            b.clear()
            assert a.items() == []
            assert a.get_or_create("anna.k@example.com", "EMAIL") == "[EMAIL_1]"
            assert b.get_value("[EMAIL_1]") == "anna.k@example.com"

    def test_purge_elsewhere_then_reuse_starts_over(self, path, monkeypatch):
        with open_vault(path) as a, open_vault(path) as b:
            a.get_or_create("jan.n@example.com", "EMAIL")
            later = time.time() + 3600
            monkeypatch.setattr(sqlite_module.time, "time", lambda: later)
            b.purge(timedelta(minutes=1))  # deletes the session row too
            assert b.get_or_create("anna.k@example.com", "EMAIL") == "[EMAIL_1]"
            assert a.items() == [("[EMAIL_1]", "anna.k@example.com")]

    def test_threads_sharing_one_vault(self, path):
        with open_vault(path) as vault:
            results = [[] for _ in range(8)]

            def work(i):
                for n in range(200):
                    results[i].append(vault.get_or_create(f"u{n}@example.com", "EMAIL"))

            threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert all(r == results[0] for r in results)
            assert sorted(results[0]) == sorted(f"[EMAIL_{n}]" for n in range(1, 201))

    def test_processes_never_hand_out_a_number_twice(self, path):
        worker = textwrap.dedent(
            f"""
            import random, sys
            sys.path.insert(0, {SRC!r})
            from veil import SQLiteVault
            r = random.Random(int(sys.argv[1]))
            values = [f"user{{n}}@example.com" for n in range(150)]
            values += [f"only{{sys.argv[1]}}_{{n}}@example.com" for n in range(20)]
            r.shuffle(values)
            with SQLiteVault(sys.argv[2], timeout=30) as vault:
                for value in values:
                    print(value, vault.get_or_create(value, "EMAIL"))
            """
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", worker, str(i), os.fspath(path)],
                stdout=subprocess.PIPE,
                text=True,
            )
            for i in range(6)
        ]
        outputs = [proc.communicate(timeout=120)[0] for proc in procs]
        assert [proc.returncode for proc in procs] == [0] * len(procs)
        seen: dict[str, str] = {}
        for out in outputs:
            for line in out.splitlines():
                value, placeholder = line.split()
                assert seen.setdefault(value, placeholder) == placeholder
        placeholders = sorted(seen.values(), key=lambda p: int(p[7:-1]))
        assert placeholders == [f"[EMAIL_{n}]" for n in range(1, 150 + 6 * 20 + 1)]
        with open_vault(path) as vault:
            assert {v: p for p, v in vault.items()} == seen


class TestWithShield:
    """A new Shield per call (as in a hook or a web request) on one file."""

    def test_placeholders_and_restore_carry_over(self, path):
        first = Shield(vault=SQLiteVault(path, session="chat"))
        masked = first.mask("Mail jan.n@example.com").text
        second = Shield(vault=SQLiteVault(path, session="chat"))
        assert second.mask("cc jan.n@example.com").text == "cc [EMAIL_1]"
        assert second.restore(masked).text == "Mail jan.n@example.com"

    def test_leak_check_remembers_spellings_across_shields(self, path):
        Shield(vault=SQLiteVault(path), normalize=True).mask(
            "Call 555-555-0123 or (555) 555-0123."
        )
        later = Shield(vault=SQLiteVault(path), normalize=True)
        assert later.mask("Old note: (555) 555-01234").warnings == [
            "Leak check: known value '(555) 555-0123' ([PHONE_1]) still appears "
            "in the masked text."
        ]
        with pytest.raises(ShieldError):
            later.wrap(lambda prompt: prompt, strict=True)("Old note: (555) 555-01234")

    def test_merged_parts_are_remembered_across_shields(self, path):
        Shield(vault=SQLiteVault(path)).mask("Pay +1 4111 1111 1111 1111 today")
        warnings = (
            Shield(vault=SQLiteVault(path)).mask("card x4111 1111 1111 1111").warnings
        )
        assert len(warnings) == 1
        assert "[CREDIT_CARD_1]" in warnings[0]

    def test_reset_clears_only_this_session(self, path):
        a = Shield(vault=SQLiteVault(path, session="a"))
        b = Shield(vault=SQLiteVault(path, session="b"))
        a.mask("jan.n@example.com")
        b.mask("anna.k@example.com")
        a.reset()
        assert a.vault.items() == []
        assert b.restore("[EMAIL_1]").text == "anna.k@example.com"

    def test_pickled_shield_reopens_the_file(self, path):
        shield = Shield(vault=SQLiteVault(path))
        shield.mask("Mail jan.n@example.com")
        data = pickle.dumps(shield)
        assert b"jan.n@example.com" not in data
        clone = pickle.loads(data)
        assert clone.restore("[EMAIL_1]").text == "jan.n@example.com"

    def test_in_memory_vault_cant_be_pickled(self):
        with pytest.raises(TypeError, match="in-memory"):
            pickle.dumps(Shield(vault=SQLiteVault(":memory:")))

    def test_about_as_fast_as_memory_vault(self, path):
        values = [f"user{n}@example.com" for n in range(20_000)]
        sqlite, memory = SQLiteVault(path), MemoryVault()
        for vault in (sqlite, memory):
            Shield(vault=vault).mask(" ".join(values))
        text = "Mail user7@example.com and new@example.com. " + "filler " * 300

        def timed(vault):
            shield = Shield(vault=vault)
            shield.mask(text)  # warm up
            start = time.perf_counter()
            for _ in range(20):
                shield.mask(text)
            return time.perf_counter() - start

        assert timed(sqlite) < 3 * timed(memory) + 0.05


class FlakyConnection:
    """Wraps a connection; the first statement ``fail`` matches raises ``error``."""

    def __init__(self, conn, fail, error, *, rollback_first=False):
        self._conn = conn
        self._fail = fail
        self._error = error
        self._rollback_first = rollback_first  # as SQLite does after an I/O error

    def execute(self, sql, *args):
        if self._fail is not None and self._fail(sql):
            self._fail = None
            if self._rollback_first and self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise self._error
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def fresh_items(path, session="default"):
    with open_vault(path, session) as vault:
        return vault.items()


class TestFailures:
    """A failed write or read must leave the vault agreeing with its file."""

    @pytest.mark.parametrize("rollback_first", [False, True])
    def test_failed_commit_leaves_nothing_behind(self, path, rollback_first):
        with open_vault(path) as vault:
            vault.get_or_create("jan.n@example.com", "EMAIL")
            vault._conn = FlakyConnection(
                vault._conn,
                lambda sql: sql == "COMMIT",
                sqlite3.OperationalError("disk I/O error"),
                rollback_first=rollback_first,
            )
            with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
                vault.get_or_create("anna.k@example.com", "EMAIL")
            assert vault.get_placeholder("anna.k@example.com") is None
            assert vault.get_or_create("piotr.w@example.com", "EMAIL") == "[EMAIL_2]"
            assert vault.get_or_create("anna.k@example.com", "EMAIL") == "[EMAIL_3]"
            assert vault.items() == fresh_items(path)

    def test_failed_clear_keeps_the_values(self, path):
        with open_vault(path) as vault:
            vault.get_or_create("jan.n@example.com", "EMAIL")
            vault._conn = FlakyConnection(
                vault._conn,
                lambda sql: sql == "COMMIT",
                sqlite3.OperationalError("disk I/O error"),
            )
            with pytest.raises(sqlite3.OperationalError):
                vault.clear()
            assert vault.get_or_create("jan.n@example.com", "EMAIL") == "[EMAIL_1]"
            assert vault.get_value("[EMAIL_1]") == "jan.n@example.com"

    def test_failed_write_in_a_mask_call(self, path):
        shield = Shield(vault=SQLiteVault(path))
        shield.mask("Mail jan.n@example.com")
        vault = shield.vault
        vault._conn = FlakyConnection(
            vault._conn,
            lambda sql: sql == "COMMIT",
            sqlite3.OperationalError("disk I/O error"),
        )
        with pytest.raises(sqlite3.OperationalError):
            shield.mask("Mail anna.k@example.com and piotr.w@example.com")
        assert shield.mask("Mail piotr.w@example.com").text == "Mail [EMAIL_2]"
        assert vault.items() == fresh_items(path)

    def test_interrupted_refresh_is_finished_later(self, path):
        with open_vault(path) as reader, open_vault(path) as writer:
            placeholder = writer.get_or_create("555-555-0123", "PHONE")
            writer._remember("(555) 555-0123", "PHONE", placeholder)
            reader._conn = FlakyConnection(
                reader._conn,
                lambda sql: "FROM vault_remembered" in sql,
                KeyboardInterrupt(),
            )
            with pytest.raises(KeyboardInterrupt):
                reader.get_value(placeholder)
            assert reader.get_value(placeholder) == "555-555-0123"
            assert reader._remembered() == [("(555) 555-0123", "PHONE", "[PHONE_1]")]


class TestOtherConnections:
    def test_clear_after_another_connection_purged_the_session(self, path, monkeypatch):
        with open_vault(path, "chat") as a, open_vault(path, "other") as b:
            a.get_or_create("jan.n@example.com", "EMAIL")
            later = time.time() + 3600
            monkeypatch.setattr(sqlite_module.time, "time", lambda: later)
            assert b.purge(timedelta(minutes=1)) == 2
            a.clear()
            assert a.items() == []
            assert a.get_or_create("anna.k@example.com", "EMAIL") == "[EMAIL_1]"
            assert a.get_placeholder("jan.n@example.com") is None
            assert a.items() == fresh_items(path, "chat")

    def test_shield_shared_by_threads_with_normalize(self, path):
        shield = Shield(vault=SQLiteVault(path), normalize=True)
        errors = []

        def work(i):
            try:
                for n in range(60):
                    text = f"Mail user{n}@example.com or USER{n}@EXAMPLE.COM ({i})"
                    masked = shield.mask(text).text
                    assert masked.count("[EMAIL_") == 2
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert len(shield.vault) == 60

    def test_opening_a_session_does_not_hold_the_write_lock_while_loading(
        self, path, monkeypatch
    ):
        with open_vault(path) as vault:
            for n in range(100):
                vault.get_or_create(f"user{n}@example.com", "EMAIL")
        held = []
        real = SQLiteVault._refresh

        def watching(self):
            held.append(self._conn.in_transaction and self._batch_depth == 0)
            with closing(sqlite3.connect(path, timeout=0)) as other:
                other.execute("BEGIN IMMEDIATE")  # fails if the lock is held
                other.execute("ROLLBACK")
            return real(self)

        monkeypatch.setattr(SQLiteVault, "_refresh", watching)
        open_vault(path).close()
        assert held


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork()")
def test_forked_child_writes_survive_the_parent_closing(path):
    import warnings

    vault = open_vault(path)
    vault.get_or_create("jan.n@example.com", "EMAIL")
    ready_read, ready_write = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads
        pid = os.fork()
    if pid == 0:  # the child: wait for the parent to close, then write
        try:
            os.close(ready_write)
            os.read(ready_read, 1)
            for n in range(10):
                vault.get_or_create(f"child{n}@example.com", "EMAIL")
        finally:
            os._exit(0)  # without closing anything, as a worker may
    os.close(ready_read)
    vault.close()  # the parent closes while the child still has the vault
    os.write(ready_write, b"x")
    os.close(ready_write)
    os.waitpid(pid, 0)
    with open_vault(path) as later:
        assert len(later) == 11


class TestPrivacy:
    def test_deleted_values_are_gone_from_the_file(self, path, monkeypatch):
        with open_vault(path, "a") as a, open_vault(path, "b") as b:
            a.get_or_create("jan.n@example.com", "EMAIL")
            b.get_or_create("4111 1111 1111 1111", "CREDIT_CARD")
            a.clear()
            later = time.time() + 3600
            monkeypatch.setattr(sqlite_module.time, "time", lambda: later)
            b.purge(timedelta(minutes=1))
        for name in (path, path.with_name(path.name + "-wal")):
            if name.exists():
                data = name.read_bytes()
                assert b"jan.n@example.com" not in data, name
                assert b"4111 1111 1111 1111" not in data, name

    def test_pickled_normalizing_shield_holds_no_values(self, path):
        shield = Shield(vault=SQLiteVault(path), normalize=True)
        shield.mask("Mail jan.n@example.com or JAN.N@EXAMPLE.COM, call 555-555-0123")
        data = pickle.dumps(shield)
        assert b"jan.n" not in data.lower()
        assert b"555-555-0123" not in data
        clone = pickle.loads(data)
        assert clone.mask("Mail Jan.N@Example.com").text == "Mail [EMAIL_1]"


class TestCopies:
    def test_relative_path_is_found_after_a_directory_change(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "app").mkdir()
        (tmp_path / "elsewhere").mkdir()
        monkeypatch.chdir(tmp_path / "app")
        shield = Shield(vault=SQLiteVault("conversations.db"))
        shield.mask("Mail jan.n@example.com")
        data = pickle.dumps(shield)
        monkeypatch.chdir(tmp_path / "elsewhere")
        clone = pickle.loads(data)
        assert clone.restore("[EMAIL_1]").text == "jan.n@example.com"
        assert not (tmp_path / "elsewhere" / "conversations.db").exists()

    def test_unpickling_does_not_create_a_missing_file(self, path):
        data = pickle.dumps(open_vault(path))
        for name in (path, f"{path}-wal", f"{path}-shm"):
            if os.path.exists(name):
                os.remove(name)
        with pytest.raises(FileNotFoundError):
            pickle.loads(data)

    def test_a_copy_shares_the_file(self, path):
        import copy

        shield = Shield(vault=SQLiteVault(path))
        shield.mask("Mail jan.n@example.com")
        clone = copy.deepcopy(shield)
        clone.mask("Mail anna.k@example.com")
        assert shield.restore("[EMAIL_2]").text == "anna.k@example.com"
        with pytest.raises(TypeError, match="pickled or copied"):
            copy.deepcopy(SQLiteVault(":memory:"))

    def test_bytes_path(self, path):
        vault = SQLiteVault(os.fsencode(path))
        vault.get_or_create("jan.n@example.com", "EMAIL")
        assert pickle.loads(pickle.dumps(vault)).items() == vault.items()


class TestValidation:
    def test_purge_needs_a_non_negative_timedelta(self, path):
        with open_vault(path) as vault:
            with pytest.raises(ValueError, match="negative"):
                vault.purge(timedelta(days=-30))
            with pytest.raises(TypeError, match="timedelta"):
                vault.purge(30)

    def test_newer_layout_is_refused_before_anything_changes(self, path):
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "CREATE TABLE vault_meta (key TEXT PRIMARY KEY, value INTEGER)"
            )
            conn.execute("INSERT INTO vault_meta VALUES ('schema_version', 2)")
            conn.execute("CREATE TABLE vault_values (id INTEGER PRIMARY KEY, x BLOB)")
        with pytest.raises(ValueError, match="schema version 2"):
            open_vault(path)
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    def test_repr_of_a_closed_vault(self, path):
        vault = open_vault(path)
        vault.close()
        assert repr(vault) == "SQLiteVault(<closed>)"


class TestBatching:
    def test_restore_checks_the_file_once(self, path):
        shield = Shield(vault=SQLiteVault(path))
        shield.mask(" ".join(f"user{n}@example.com" for n in range(300)))
        reply = " ".join(f"[EMAIL_{n}]" for n in range(1, 301))
        vault = shield.vault
        checks = []
        vault._conn = FlakyConnection(
            vault._conn, lambda sql: checks.append(sql) and False, None
        )
        shield.restore(reply)
        assert checks.count("PRAGMA data_version") == 1

    def test_new_values_of_one_call_are_written_together(self, path):
        shield = Shield(vault=SQLiteVault(path))
        vault = shield.vault
        statements = []
        vault._conn = FlakyConnection(
            vault._conn, lambda sql: statements.append(sql) and False, None
        )
        shield.mask(" ".join(f"user{n}@example.com" for n in range(200)))
        assert statements.count("BEGIN IMMEDIATE") == 1
        assert len(vault) == 200


def test_a_new_shield_never_merges_into_a_merged_placeholder(path):
    first = "Account DE89 3704 0044 0532 0130 00 BX closed."  # IBAN + a REF
    second = "New account DE89370400440532013000BX opened."  # one whole IBAN

    def shield():
        s = Shield(vault=SQLiteVault(path, session="c"), normalize=True)
        s.add_entity("00 BX", "REF")
        return s

    assert shield().mask(first).text == "Account [IBAN_1] closed."
    later = shield()
    masked = later.mask(second)
    assert masked.text == "New account [IBAN_2] opened."
    assert later.restore(masked.text).text == second
