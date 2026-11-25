"""`locus feedback`: a prefilled issue address with non-sensitive facts only; opens a browser only on request."""

from __future__ import annotations

import webbrowser
from urllib.parse import parse_qs, urlparse

from locus import cli


def test_print_shows_the_url_and_does_not_open_a_browser(monkeypatch, capsys):
    monkeypatch.setattr(webbrowser, "open", lambda *_: (_ for _ in ()).throw(AssertionError("opened")))
    args = cli.build_parser().parse_args(["feedback", "--print"])
    assert cli.cmd_feedback(args) == 0
    url = capsys.readouterr().out.strip()
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == cli.FEEDBACK_URL
    body = parse_qs(parsed.query)["body"][0]
    for label in ("locus:", "python:", "os:", "embedder:"):
        assert label in body


def test_the_notice_does_not_claim_nothing_is_sent_until_submitting(monkeypatch, capsys):
    monkeypatch.setattr(webbrowser, "open", lambda u: True)
    cli.cmd_feedback(cli.build_parser().parse_args(["feedback"]))
    out = " ".join(capsys.readouterr().out.split())
    assert "Nothing is sent until you submit" not in out
    assert "GitHub receives that address when the page opens" in out and "no issue is created unless you submit" in out


def test_url_holds_no_path_or_query_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = cli.feedback_url("local")
    assert str(tmp_path) not in url and "/Users" not in url and "query" not in url.lower()


def test_without_print_it_opens_the_browser_once(monkeypatch, capsys):
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u) or True)
    assert cli.cmd_feedback(cli.build_parser().parse_args(["feedback"])) == 0
    assert len(opened) == 1 and opened[0].startswith(cli.FEEDBACK_URL)
