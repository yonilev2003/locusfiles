"""The licence: PolyForm Noncommercial 1.0.0, with its official text unchanged, the Required Notice, no leftover FSL
wording, and the soft notices (one first-run question, `locus license`, a help article, one sentence in the server
instructions). Soft means no lock: nothing here reads a key, contacts a server or refuses to work."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import re
import tomllib
import zipfile
from pathlib import Path

import pytest

from locus import cli, licence, mcp_server, support

ROOT = Path(__file__).resolve().parents[1]
NOTICE_LINE = "Required Notice: Copyright 2026 Yonathan Levy (https://locusfiles.com)"
# SHA-256 of the official text of the PolyForm Noncommercial License 1.0.0, file PolyForm-Noncommercial-1.0.0.md at tag
# 1.0.0 of github.com/polyformproject/polyform-licenses (checked 2026-11-14; the SPDX data and spdx.org carry the same
# 686 words, wrapped differently). LICENSE is this text, byte for byte, after the Required Notice line and a blank line.
OFFICIAL_SHA256 = "c0ea4a896d2c8c394b29f9427589996db826cd501c512279ff0ed3ef48fabbe5"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# --- the licence text -------------------------------------------------------------------------------------------


def test_license_is_the_official_text_byte_for_byte_after_the_required_notice():
    raw = (ROOT / "LICENSE").read_bytes()
    first, blank, rest = raw.split(b"\n", 2)
    assert first.decode() == NOTICE_LINE and blank == b""
    assert hashlib.sha256(rest).hexdigest() == OFFICIAL_SHA256
    assert rest.startswith(b"# PolyForm Noncommercial License 1.0.0\n")


def test_the_package_copy_is_identical_to_the_root_copy():
    assert (ROOT / "LICENSE").read_bytes() == (ROOT / "src" / "locus" / "LICENSE").read_bytes()


def test_the_declared_licence_is_the_spdx_id():
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    assert project["license"] == licence.SPDX_ID == "PolyForm-Noncommercial-1.0.0"
    assert "src/locus/LICENSE" in project["license-files"]
    assert not any(c.startswith("License ::") for c in project.get("classifiers", []))  # PEP 639


def test_the_spdx_id_is_valid_for_the_build_tools():
    packaging_licenses = pytest.importorskip("packaging.licenses")
    assert packaging_licenses.canonicalize_license_expression(licence.SPDX_ID) == licence.SPDX_ID


def test_notice_and_inventory_name_the_licence_and_carry_the_required_notice():
    notice = _read("src/locus/NOTICE")
    assert NOTICE_LINE in notice and "PolyForm Noncommercial License 1.0.0" in notice
    assert "https://locusfiles.com/commercial" in notice
    assert "PolyForm Noncommercial License 1.0.0" in _read("src/locus/THIRD_PARTY_LICENSES.md")
    spec = importlib.util.spec_from_file_location("licence_inventory", ROOT / "scripts" / "licence_inventory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "PolyForm Noncommercial License 1.0.0" in module.render([], "2026-11-14")


def test_no_current_text_still_describes_the_earlier_licence():
    """FSL may be named only to say that releases before 0.2.0 were released under it."""
    allowed_fsl = {
        "README.md", "LICENSE-FAQ.md", "docs/package/README.md", "CHANGELOG.md", "pyproject.toml",
        "src/locus/support_kb/licence.md", "tests/test_licence.py", "scripts/check_dist_contents.py",
    }  # fmt: skip
    for path in sorted(p for p in ROOT.rglob("*") if p.is_file() and ".venv" not in p.parts and ".git" not in p.parts):
        rel = str(path.relative_to(ROOT))
        if path.suffix not in {".md", ".toml", ".json", ".yml", ".py", ".svg", ".txt"} or rel in ("uv.lock",):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if rel not in allowed_fsl and rel != "LICENSE":
            assert "FSL-1.1" not in text and "Functional Source" not in text, rel
        if rel != "CHANGELOG.md" and rel != "tests/test_licence.py":
            assert "Competing Use" not in text and "Grant of Future License" not in text, rel
    for rel in ("README.md", "docs/package/README.md", "LICENSE-FAQ.md"):
        for line in _read(rel).splitlines():
            if "FSL" in line:
                assert re.search(r"before 0\.2\.0|Releases before|earlier|0\.1\.", line), (rel, line)


# --- what the public texts say about commercial use ----------------------------------------------------------------


@pytest.mark.parametrize("rel", ["README.md", "docs/package/README.md", "LICENSE-FAQ.md", "COMMERCIAL-LICENSE.md"])
def test_every_public_licence_text_says_commercial_use_needs_a_paid_licence_that_cannot_be_bought_yet(rel):
    text = " ".join(_read(rel).split())
    assert "commercial" in text.lower() and "https://locusfiles.com/commercial" in text
    not_yet = r"cannot be bought yet|none can be bought yet|no commercial licence can be bought yet"
    assert re.search(not_yet, text, re.I), rel


def test_the_faq_defines_commercial_in_the_licensors_words_and_says_it_is_not_legal_advice():
    faq = " ".join(_read("LICENSE-FAQ.md").split())
    assert "work for a company, paid client work, or use inside a product or service" in faq
    assert "including when you are one person" in faq
    assert "not legal advice" in faq and "no lock" in faq.lower()
    assert "personal" in faq and "educational" in faq and "charitable" in faq


def test_the_commercial_licence_page_is_short_and_points_to_the_page():
    text = _read("COMMERCIAL-LICENSE.md")
    assert len(text.split()) < 250 and "https://locusfiles.com/commercial" in text


# --- locus license, the help article, the instruction sentence -------------------------------------------------------


@pytest.mark.parametrize("command", ["license", "licence"])
def test_the_license_command_prints_two_sentences_and_the_page(capsys, command):
    with pytest.raises(SystemExit) as exc:
        cli.main([command])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert licence.SHORT in out and "Page: https://locusfiles.com/commercial" in out and "not legal advice" in out
    assert len(re.findall(r"[.!?](?:\s|$)", licence.SHORT)) == 2


def test_the_help_tool_has_a_licence_article_and_finds_it_for_licence_questions():
    assert "licence" in {a.id for a in support.load_articles()}
    for question in (
        "can I use Locus at my company",
        "is Locus free for commercial use",
        "I am a freelancer using this for a client",
        "what licence is Locus under",
        "how much is a commercial licence",
    ):
        results = support.help_index().search(question, limit=3)
        assert results and results[0][0].id == "licence", (question, [a.id for a, _ in results])


def test_the_help_article_repeats_the_cli_text():
    body = " ".join(next(a for a in support.load_articles() if a.id == "licence").body.split())
    assert licence.SHORT in body and licence.COMMERCIAL_URL in body


def test_the_server_instructions_carry_the_one_sentence_and_stay_in_budget():
    instructions = mcp_server.mcp.instructions or ""
    assert licence.INSTRUCTIONS_SENTENCE in instructions
    assert instructions.count("locusfiles.com/commercial") == 1
    assert "never refuse to search because of it" in instructions
    assert len(instructions) < 4096
    lead = instructions.split("\n\n")[0]
    assert "commercial" not in lead  # the measured first paragraph is unchanged


def test_the_sentence_never_tells_the_model_to_refuse_or_to_stop():
    sentence = licence.INSTRUCTIONS_SENTENCE.lower()
    assert "tell them once" in sentence and "then keep answering" in sentence
    assert not re.search(r"\b(refuse to (answer|help)|do not answer|stop answering)\b", sentence)


# --- the first-run question -------------------------------------------------------------------------------------


class _Tty(io.StringIO):
    def __init__(self, answer: str = "", tty: bool = True) -> None:
        super().__init__(answer)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_without_a_terminal_nothing_is_asked_and_nothing_is_written(tmp_path):
    out = io.StringIO()
    assert licence.ask_once(tmp_path / "data", stdin=_Tty("y\n", tty=False), out=out) is True
    assert out.getvalue() == "" and not (tmp_path / "data").exists()


@pytest.mark.parametrize("answer", ["n\n", "\n", "no\n", "maybe\n", ""])
def test_a_no_goes_on_and_is_remembered_so_it_is_asked_once(tmp_path, answer):
    data = tmp_path / "data"
    out = io.StringIO()
    assert licence.ask_once(data, stdin=_Tty(answer), out=out) is True
    assert out.getvalue() == licence.QUESTION
    record = (data / licence.ACK_FILE).read_text()
    assert record.startswith("noncommercial ") and "\n" == record[-1]
    again = io.StringIO()
    assert licence.ask_once(data, stdin=_Tty("y\n"), out=again) is True and again.getvalue() == ""  # not asked again


@pytest.mark.parametrize("answer", ["y\n", "Y\n", "yes\n", " YES \n"])
def test_a_yes_prints_the_stop_text_and_stops_and_is_not_remembered(tmp_path, answer):
    data = tmp_path / "data"
    out = io.StringIO()
    assert licence.ask_once(data, stdin=_Tty(answer), out=out) is False
    assert out.getvalue() == licence.QUESTION + licence.COMMERCIAL_STOP + "\n"
    assert not (data / licence.ACK_FILE).exists()  # it will be asked again next time


def test_the_question_and_the_stop_text_are_the_licensors_words():
    assert licence.QUESTION == (
        "Will you use Locus for work at a company, for paid client work, or in a product or service? [y/N] "
    )
    assert licence.COMMERCIAL_STOP == (
        "Commercial use needs a separate paid licence. None can be bought yet. "
        "Leave your details at https://locusfiles.com/commercial"
    )


def test_a_data_folder_that_cannot_be_written_never_makes_the_notice_an_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    assert licence.ask_once(blocker / "data", stdin=_Tty("n\n"), out=io.StringIO()) is True


@pytest.fixture
def terminal(monkeypatch, tmp_path):
    from locus.config import get_settings

    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()

    def answer(text: str) -> None:
        monkeypatch.setattr("sys.stdin", _Tty(text))

    yield answer
    get_settings.cache_clear()


def test_setup_stops_on_a_yes_and_writes_nothing(terminal, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    terminal("y\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["setup", "--local"])
    assert exc.value.code == cli.EXIT_NEEDS_YES
    err = capsys.readouterr().err
    assert licence.COMMERCIAL_STOP in err
    assert not (tmp_path / ".env").exists() and not (tmp_path / "data" / licence.ACK_FILE).exists()


def test_doctor_stops_on_a_yes_before_it_checks_anything(terminal, capsys):
    terminal("yes\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor"])
    assert exc.value.code == cli.EXIT_NEEDS_YES
    captured = capsys.readouterr()
    assert licence.COMMERCIAL_STOP in captured.err and "[OK" not in captured.out and "[FAIL" not in captured.out


def test_doctor_goes_on_after_a_no_and_asks_once(terminal, capsys, tmp_path):
    terminal("n\n")
    with pytest.raises(SystemExit):
        cli.main(["doctor"])
    first = capsys.readouterr()
    assert licence.QUESTION in first.err and "Python interpreter" in first.out
    assert (tmp_path / "data" / licence.ACK_FILE).is_file()
    terminal("y\n")  # would stop it if it were asked again
    with pytest.raises(SystemExit):
        cli.main(["doctor"])
    second = capsys.readouterr()
    assert licence.QUESTION not in second.err and "Python interpreter" in second.out


def test_other_commands_never_ask(terminal, capsys):
    terminal("y\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["stats"])
    assert licence.QUESTION not in capsys.readouterr().err and exc.value.code in (0, 2)


# --- the release check -------------------------------------------------------------------------------------------


def _release_module():
    spec = importlib.util.spec_from_file_location("check_dist_contents", ROOT / "scripts" / "check_dist_contents.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel(tmp_path: Path, *, expression: str, licence_text: str) -> Path:
    path = tmp_path / "locusfiles-0.2.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as whl:
        whl.writestr(
            "locusfiles-0.2.0.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: locusfiles\nLicense-Expression: {expression}\n",
        )
        whl.writestr("locusfiles-0.2.0.dist-info/licenses/src/locus/LICENSE", licence_text)
    return path


def test_the_release_check_accepts_the_real_licence_and_refuses_anything_else(tmp_path):
    check = _release_module()
    real = _read("LICENSE")
    assert check.release_errors(_wheel(tmp_path, expression="PolyForm-Noncommercial-1.0.0", licence_text=real)) == []
    wrong_id = check.release_errors(_wheel(tmp_path, expression="FSL-1.1-ALv2", licence_text=real))
    assert any("expected 'PolyForm-Noncommercial-1.0.0'" in e for e in wrong_id)
    fsl_text = "# Functional Source License, Version 1.1, ALv2 Future License\n"
    old = check.release_errors(_wheel(tmp_path, expression="PolyForm-Noncommercial-1.0.0", licence_text=fsl_text))
    assert any("not the PolyForm" in e for e in old) and any("earlier FSL" in e for e in old)
    no_notice = real.replace("Required Notice: Copyright", "Notice: Copyright")
    errors = check.release_errors(_wheel(tmp_path, expression="PolyForm-Noncommercial-1.0.0", licence_text=no_notice))
    assert any("Required Notice" in e for e in errors)
