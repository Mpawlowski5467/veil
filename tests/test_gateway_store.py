"""The gateway's settings file, data folder, and conversations on disk."""

import json
import os
import stat
import subprocess
from datetime import timedelta

import pytest

from veil.gateway import (
    Settings,
    SettingsError,
    SQLiteLedger,
    default_data_dir,
    git_identity,
    load_settings,
    open_sessions,
    prepare_data_dir,
)
from veil.gateway.store import literal_types

NAME = "Jan Nowak"
EMAIL = "jane.doe@example.com"


def write(tmp_path, content):
    path = tmp_path / "config.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


BAD_SETTINGS = [
    ("{not json", "not valid JSON"),
    ([1, 2], "must be a JSON object"),
    ({"entites": {}}, "unknown key 'entites'"),
    ({"my secret key!": 1}, "unknown key '<key>'"),
    ({"version": 2}, "version: must be 1"),
    ({"entities": []}, "entities: must be a JSON object"),
    ({"entities": {"person": [NAME]}}, "entities.person: isn't a valid type name"),
    (
        {"entities": {"PERSON": NAME}},
        "entities.PERSON: must be a list of non-blank",
    ),
    (
        {"entities": {"PERSON": [" "]}},
        "entities.PERSON: must be a list of non-blank",
    ),
    ({"entities": {"LITERAL": ["x"]}}, "entities.LITERAL: is reserved"),
    ({"patterns": {"ORDER": "(unclosed"}}, "patterns.ORDER: isn't a valid regex"),
    ({"patterns": {"ORDER": ""}}, "patterns.ORDER: must be a non-empty string"),
    ({"identity": "yes"}, "identity: must be true or false"),
    (
        {"entities": {"ADDRESS": ["line one\nline two"]}},
        "entities.ADDRESS: values must be on one line",
    ),
    ({"retention_days": 0}, "retention_days: must be from 1 to 3650"),
    ({"retention_days": True}, "retention_days: must be a whole number"),
    (
        {"allow_mcp_tools": ["Bash"]},
        "allow_mcp_tools: must be a list of tool names",
    ),
]


class TestSettings:
    def test_a_missing_file_means_the_defaults(self, tmp_path):
        assert load_settings(tmp_path / "absent.json") == Settings()
        defaults = Settings()
        assert defaults.identity is True
        assert defaults.retention_days == 30
        assert defaults.note is True

    def test_a_full_file(self, tmp_path):
        path = write(
            tmp_path,
            {
                "version": 1,
                "$comment": "fictional sample",
                "entities": {"PERSON": [NAME, "Ada Quill"], "CLIENT": ["Example Corp"]},
                "patterns": {"ORDER": r"#\d{5}"},
                "identity": False,
                "retention_days": 7,
                "note": False,
                "allow_mcp_tools": ["mcp__crm__lookup"],
            },
        )
        assert load_settings(path) == Settings(
            entities={"PERSON": (NAME, "Ada Quill"), "CLIENT": ("Example Corp",)},
            patterns={"ORDER": r"#\d{5}"},
            identity=False,
            retention_days=7,
            note=False,
            allow_mcp_tools=("mcp__crm__lookup",),
        )

    @pytest.mark.parametrize(("content", "problem"), BAD_SETTINGS)
    def test_problems_name_the_key(self, tmp_path, content, problem):
        path = write(tmp_path, content)
        with pytest.raises(SettingsError) as info:
            load_settings(path)
        assert str(info.value).startswith(f"{path}: ")
        assert problem in str(info.value)

    def test_a_file_that_isnt_utf8(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_bytes(b'{"entities": {"PERSON": ["\xff"]}}')
        with pytest.raises(SettingsError, match="not UTF-8"):
            load_settings(path)

    def test_problems_never_quote_a_value(self, tmp_path):
        path = write(tmp_path, {"entities": {"PERSON": [NAME, 5]}})
        with pytest.raises(SettingsError) as info:
            load_settings(path)
        assert NAME not in str(info.value)
        path = write(tmp_path, {"entities": {f"{NAME}!": ["x"]}})
        with pytest.raises(SettingsError) as info:
            load_settings(path)
        assert NAME not in str(info.value)

    def test_the_data_folder_is_named_after_the_package(self):
        import veil

        assert default_data_dir().name == f".{veil.__name__}"


class TestDataFolder:
    def test_created_for_the_owner_only(self, tmp_path):
        folder = prepare_data_dir(tmp_path / "data")
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700

    def test_a_folder_others_can_use_is_refused(self, tmp_path):
        folder = tmp_path / "data"
        folder.mkdir(mode=0o755)
        folder.chmod(0o755)
        with pytest.raises(SettingsError, match="other users can use it"):
            prepare_data_dir(folder)

    def test_a_symlink_is_refused(self, tmp_path):
        (tmp_path / "real").mkdir(mode=0o700)
        (tmp_path / "data").symlink_to(tmp_path / "real")
        with pytest.raises(SettingsError, match="not a link"):
            prepare_data_dir(tmp_path / "data")

    def test_a_folder_owned_by_someone_else_is_refused(self, tmp_path, monkeypatch):
        folder = tmp_path / "data"
        folder.mkdir(mode=0o700)
        monkeypatch.setattr(os, "getuid", lambda: os.stat(folder).st_uid + 1)
        with pytest.raises(SettingsError, match="owned by you"):
            prepare_data_dir(folder)


class TestGitIdentity:
    def test_reads_the_repositorys_name_and_email(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Ada Quill"], cwd=tmp_path, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "ada.q@example.com"],
            cwd=tmp_path,
            check=True,
        )
        assert git_identity(tmp_path) == {
            "Ada Quill": "PERSON",
            "ada.q@example.com": "EMAIL",
        }

    def test_nothing_without_git(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))
        assert git_identity(tmp_path) == {}


class TestLedger:
    def test_round_trip_and_persistence(self, tmp_path):
        path = tmp_path / "ledger.db"
        ledger = SQLiteLedger(path, "s1")
        ledger.record_text(f"Hi {NAME}", "Hi [PERSON_1]")
        ledger.record_tool_input(
            "t1", {"command": f"echo {NAME}"}, {"command": "echo [PERSON_1]"}
        )
        ledger.close()
        again = SQLiteLedger(path, "s1")
        assert again.masked_text(f"Hi {NAME}") == "Hi [PERSON_1]"
        assert again.masked_tool_input("t1", {"command": f"echo {NAME}"}) == {
            "command": "echo [PERSON_1]"
        }
        assert again.masked_tool_input("t1", {"command": "echo other"}) is None
        assert again.masked_text("unknown") is None
        other = SQLiteLedger(path, "s2")
        assert other.masked_text(f"Hi {NAME}") is None
        again.close()
        other.close()

    def test_the_file_is_private_and_holds_no_real_value(self, tmp_path):
        path = tmp_path / "ledger.db"
        ledger = SQLiteLedger(path, "s1")
        ledger.record_text(f"Hi {NAME} at {EMAIL}", "Hi [PERSON_1] at [EMAIL_1]")
        ledger.record_tool_input("t1", {"to": EMAIL}, {"to": "[EMAIL_1]"})
        ledger.close()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        raw = b"".join(p.read_bytes() for p in tmp_path.iterdir())
        assert NAME.encode() not in raw
        assert EMAIL.encode() not in raw

    def test_many_gateways_can_open_a_new_file_at_once(self, tmp_path):
        import multiprocessing

        path = tmp_path / "ledger.db"
        context = multiprocessing.get_context("spawn")
        with context.Pool(8) as pool:
            results = pool.map(_open_ledger, [(str(path), f"s{i}") for i in range(24)])
        assert results == ["ok"] * 24

    def test_purge_by_age(self, tmp_path, monkeypatch):
        ledger = SQLiteLedger(tmp_path / "ledger.db", "s1")
        ledger.record_text("a", "A")
        ledger.record_tool_input("t", {}, {})
        assert ledger.purge(timedelta(days=1)) == 0
        import veil.gateway.store as store

        now = store.time.time()
        monkeypatch.setattr(store.time, "time", lambda: now + 2 * 86400)
        assert ledger.purge(timedelta(days=1)) == 2
        assert ledger.masked_text("a") is None
        ledger.close()


class TestSessions:
    def settings(self):
        return Settings(entities={"PERSON": (NAME,)}, patterns={"ORDER": r"#\d{5}"})

    def test_placeholders_last_across_gateway_launches(self, tmp_path):
        first = open_sessions(tmp_path, self.settings(), {})
        masked = first.get("conv-1").shield.mask(f"{NAME} ordered #12345").text
        assert masked == "[PERSON_1] ordered [ORDER_1]"
        later = open_sessions(tmp_path, self.settings(), {})
        assert (
            later.get("conv-1").shield.restore(masked).text == f"{NAME} ordered #12345"
        )
        # Another conversation has placeholders of its own.
        assert later.get("conv-2").shield.mask("#54321").text == "[ORDER_1]"

    def test_the_ledger_lasts_across_launches_too(self, tmp_path):
        with open_sessions(tmp_path, self.settings(), {}) as first:
            first.get("conv-1").ledger.record_text(f"Hi {NAME}", "Hi [PERSON_1]em")
        with open_sessions(tmp_path, self.settings(), {}) as later:
            masked = later.get("conv-1").ledger.masked_text(f"Hi {NAME}")
        assert masked == "Hi [PERSON_1]em"

    def test_a_local_git_email_is_masked_too(self, tmp_path):
        identity = {"Ada Quill": "PERSON", "aquill@corp": "EMAIL"}
        with open_sessions(tmp_path, Settings(), identity) as sessions:
            masked = (
                sessions.get("c").shield.mask("Author: Ada Quill <aquill@corp>").text
            )
        assert masked == "Author: [PERSON_1] <[EMAIL_1]>"

    def test_the_git_identity_is_masked(self, tmp_path):
        identity = {"Ada Quill": "PERSON", "ada.q@example.com": "EMAIL"}
        with open_sessions(tmp_path, Settings(), identity) as sessions:
            masked = sessions.get("c").shield.mask("Ada Quill <ada.q@example.com>").text
        assert masked == "[PERSON_1] <[EMAIL_1]>"

    def test_literal_text_is_escaped_for_every_type_in_use(self, tmp_path):
        settings = self.settings()
        types = literal_types(settings, {"Ada Quill": "PERSON"})
        assert {
            "EMAIL",
            "PHONE",
            "IPV4",
            "IPV6",
            "CREDIT_CARD",
            "IBAN",
            "SSN",
            "ORDER",
            "PERSON",
        } <= types
        with open_sessions(tmp_path, settings, {}) as sessions:
            masked = sessions.get("c").shield.mask("Use [ORDER_1] and row[COL_1]").text
        assert masked == "Use [LITERAL_1] and row[COL_1]"

    def test_literal_text_stays_literal_after_a_pattern_is_removed(self, tmp_path):
        with open_sessions(tmp_path, self.settings(), {}) as sessions:
            sessions.get("c").shield.mask("order #12345")  # [ORDER_1]
        without = Settings(entities={"PERSON": (NAME,)})
        with open_sessions(tmp_path, without, {}) as sessions:
            shield = sessions.get("c").shield
            masked = shield.mask("Template: your order [ORDER_1] shipped").text
            assert masked == "Template: your order [LITERAL_1] shipped"
            assert shield.restore(masked, tolerant=False).text == (
                "Template: your order [ORDER_1] shipped"
            )

    def test_registered_values_reach_the_masker(self, tmp_path):
        with open_sessions(
            tmp_path, self.settings(), {"Ada Quill": "PERSON"}
        ) as sessions:
            masker = sessions.get("c").masker
            body = {
                "messages": [{"role": "user", "content": f"{NAME}em and Ada Quills"}]
            }
            assert (
                masker.mask(body)["messages"][0]["content"]
                == "[PERSON_1]em and [PERSON_2]s"
            )

    def test_the_note_setting(self, tmp_path):
        with open_sessions(tmp_path, Settings(), {}) as sessions:
            assert sessions.get("c").masker.mask({"messages": []}).get("system")
        with open_sessions(tmp_path, Settings(note=False), {}) as sessions:
            assert "system" not in sessions.get("d").masker.mask({"messages": []})

    def test_old_conversations_are_purged_on_open(self, tmp_path, monkeypatch):
        with open_sessions(tmp_path, Settings(retention_days=1), {}) as sessions:
            sessions.get("old").shield.mask(EMAIL)
        import veil.vault.sqlite as sqlite_vault

        now = sqlite_vault.time.time()
        monkeypatch.setattr(sqlite_vault.time, "time", lambda: now + 3 * 86400)
        with open_sessions(tmp_path, Settings(retention_days=1), {}) as fresh:
            assert fresh.get("old").shield.restore("[EMAIL_1]").text == "[EMAIL_1]"


def _open_ledger(args):
    path, session = args
    ledger = SQLiteLedger(path, session)
    ledger.record_text(session, session.upper())
    ledger.close()
    return "ok"
