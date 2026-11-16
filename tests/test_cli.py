"""End-to-end checks of the `python -m wt` command line (no network, no keys)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wt import __main__ as cli
from wt import db

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Run in an empty directory with a settings file pointing everything into it."""
    (tmp_path / "settings.toml").write_text(
        '[paths]\ndb = "data/test.db"\nwallets = "wallets.txt"\n[live]\nfollow_file = "follow.txt"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.delenv("JUP_API_KEY", raising=False)
    return tmp_path


def test_demo_runs_as_module_and_filters_wallets(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-m", "wt", "demo"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    text = out.stdout
    assert "COPY-TRADING RESULT" in text and "BASELINE" in text
    follow_lines = [line for line in text.splitlines() if line.rstrip().endswith("FOLLOW")]
    assert any(line.startswith("skille") for line in follow_lines)
    assert not any(line.startswith(("bot_", "lucky")) for line in follow_lines)


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("collect", "backtest", "live", "report", "reset-paper", "demo"):
        assert cmd in out


def test_unknown_command_fails():
    with pytest.raises(SystemExit) as e:
        cli.main(["nope"])
    assert e.value.code == 2


def test_collect_without_wallets_explains(workdir):
    with pytest.raises(SystemExit, match="wallets.txt"):
        cli.main(["collect"])


def test_collect_without_helius_key_explains(workdir):
    (workdir / "wallets.txt").write_text("SomeWallet111  # comment\n")
    with pytest.raises(SystemExit, match="HELIUS_API_KEY"):
        cli.main(["collect"])


def test_live_without_follow_file_explains(workdir):
    with pytest.raises(SystemExit, match="follow.txt"):
        cli.main(["live"])


def test_backtest_on_empty_db_explains(workdir):
    with pytest.raises(SystemExit, match="collect"):
        cli.main(["backtest"])


def test_report_and_reset_paper(workdir, capsys):
    cli.main(["report"])
    assert "No paper account yet" in capsys.readouterr().out

    con = db.connect(str(workdir / "data/test.db"))
    con.execute("INSERT INTO paper_account VALUES(1, 9.0, 10.0, 0)")
    con.execute("INSERT INTO positions(mint,wallet,entry_ts,cost_sol,token_raw,last_value,status,"
                "exit_ts,proceeds_sol,exit_reason) VALUES('M','W1234567890ab',0,0.5,'1',NULL,'closed',60,1.0,'take_profit')")
    con.commit()
    cli.main(["report"])
    out = capsys.readouterr().out
    assert "Closed trades 1" in out and "take_profit" in out

    cli.main(["reset-paper"])
    assert "Paper account reset." in capsys.readouterr().out
    assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM paper_account").fetchone()[0] == 0
