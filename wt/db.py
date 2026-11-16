import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
  sig TEXT, wallet TEXT, mint TEXT, ts INTEGER, side TEXT,
  token_amt REAL, sol_amt REAL, source TEXT,
  PRIMARY KEY(sig, wallet, mint));
CREATE INDEX IF NOT EXISTS trades_wallet_ts ON trades(wallet, ts);
CREATE TABLE IF NOT EXISTS wallet_meta(
  wallet TEXT PRIMARY KEY, newest_ts INTEGER, oldest_ts INTEGER, n_tx INTEGER, collected_at INTEGER);
CREATE TABLE IF NOT EXISTS pools(mint TEXT PRIMARY KEY, pool TEXT, fetched_at INTEGER);
CREATE TABLE IF NOT EXISTS candles(
  pool TEXT, mint TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
  PRIMARY KEY(pool, mint, ts));
CREATE TABLE IF NOT EXISTS candle_fetches(pool TEXT, mint TEXT, before_ts INTEGER,
  PRIMARY KEY(pool, mint, before_ts));
CREATE TABLE IF NOT EXISTS paper_account(
  id INTEGER PRIMARY KEY CHECK(id=1), cash_sol REAL, start_sol REAL, started_at INTEGER);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, wallet TEXT, signal_sig TEXT,
  signal_ts INTEGER, entry_ts INTEGER, cost_sol REAL, token_raw TEXT,
  peak_value REAL, last_value REAL, last_quote_ts INTEGER, no_route_since INTEGER,
  status TEXT, exit_ts INTEGER, proceeds_sol REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS whale_holdings(
  wallet TEXT, mint TEXT, bought REAL DEFAULT 0, sold REAL DEFAULT 0,
  PRIMARY KEY(wallet, mint));
CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS equity_snapshots(ts INTEGER PRIMARY KEY, cash REAL, open_value REAL);
"""


def connect(path):
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def insert_trades(con, trades):
    con.executemany(
        "INSERT OR IGNORE INTO trades VALUES(:sig,:wallet,:mint,:ts,:side,:token_amt,:sol_amt,:source)",
        trades,
    )
    con.commit()


def wallet_trades(con, wallet):
    rows = con.execute(
        "SELECT * FROM trades WHERE wallet=? ORDER BY ts, sig", (wallet,)
    ).fetchall()
    return [dict(r) for r in rows]


def all_wallets(con):
    return [r[0] for r in con.execute("SELECT DISTINCT wallet FROM trades")]
