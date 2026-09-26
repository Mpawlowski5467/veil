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
