"""Thin sqlite layer. No ORM on purpose: one file, zero deps, easy to back up.

Swap DB_PATH to /home/data/... on Azure App Service (persistent mount).
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from . import config

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS deals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at REAL NOT NULL,
  source TEXT,                  -- discord channel / manual / api
  raw_message TEXT,
  url TEXT,
  final_url TEXT,
  merchant TEXT,
  trust_verdict TEXT,           -- TRUSTED | UNKNOWN | SUSPICIOUS | BLOCKED
  trust_score REAL,
  trust_reasons TEXT,           -- json list
  product_name TEXT,
  brand TEXT,
  sku TEXT,
  size TEXT,
  category TEXT,
  listed_price REAL,
  currency TEXT,
  market_price REAL,
  market_price_source TEXT,
  demand_score REAL,
  returnable INTEGER,
  return_window_days INTEGER,
  discount_pct REAL,
  est_profit_unit REAL,
  score REAL,
  decision TEXT,                -- BUY | SKIP | HOLD | BLOCKED
  decision_reasons TEXT,        -- json list
  target_qty INTEGER DEFAULT 0,
  status TEXT,                  -- new|researching|ready|ordering|ordered|failed|skipped
  latency_ms INTEGER,
  error TEXT,
  raw_product TEXT              -- json blob of scraped facts
);
CREATE INDEX IF NOT EXISTS idx_deals_created ON deals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id INTEGER,
  ts REAL NOT NULL,
  stage TEXT,
  level TEXT,                   -- info|warn|error|ok
  message TEXT,
  data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_deal ON events(deal_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nickname TEXT NOT NULL,
  network TEXT,                 -- visa|mastercard|amex|discover
  last4 TEXT,
  credit_limit REAL DEFAULT 0,
  available REAL DEFAULT 0,     -- remaining credit you can spend
  priority INTEGER DEFAULT 100, -- global fallback order, lower = earlier
  statement_day INTEGER,        -- day of month the limit frees up
  max_per_txn REAL DEFAULT 0,   -- 0 = no cap
  daily_cap REAL DEFAULT 0,     -- 0 = no cap
  enabled INTEGER DEFAULT 1,
  cooldown_until REAL DEFAULT 0,-- set after a decline
  notes TEXT,
  updated_at REAL
);

CREATE TABLE IF NOT EXISTS card_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant TEXT NOT NULL,       -- domain, or '*' for default
  card_id INTEGER NOT NULL,
  rank INTEGER DEFAULT 0,       -- order to try, lower first
  enabled INTEGER DEFAULT 1,
  shipping_method TEXT,         -- preferred shipping for this merchant
  FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cardrules_merchant ON card_rules(merchant, rank);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id INTEGER,
  created_at REAL,
  merchant TEXT,
  product_name TEXT,
  card_id INTEGER,
  card_label TEXT,
  qty INTEGER,
  unit_price REAL,
  total REAL,
  shipping_method TEXT,
  status TEXT,                  -- pending|placed|failed|cancelled|refunded
  failure_stage TEXT,
  failure_reason TEXT,
  order_number TEXT,
  placed_at REAL,
  carrier TEXT,
  tracking_number TEXT,
  ship_status TEXT,             -- label_created|in_transit|out_for_delivery|delivered|exception
  eta TEXT,
  delivered_at REAL,
  last_tracking_update REAL,
  attempts INTEGER DEFAULT 0,
  raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_ship ON orders(ship_status);

CREATE TABLE IF NOT EXISTS shipping_updates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER,
  ts REAL,
  status TEXT,
  carrier TEXT,
  tracking_number TEXT,
  eta TEXT,
  note TEXT,
  source TEXT,
  raw TEXT
);

CREATE TABLE IF NOT EXISTS merchants (
  domain TEXT PRIMARY KEY,
  name TEXT,
  trusted INTEGER DEFAULT 0,
  return_window_days INTEGER,
  free_returns INTEGER,
  restocking_fee_pct REAL DEFAULT 0,
  cancel_risk TEXT,             -- low|medium|high  (how often they cancel price errors)
  checkout_difficulty TEXT,     -- easy|medium|hard
  notes TEXT,
  updated_at REAL
);

CREATE TABLE IF NOT EXISTS playbooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merchant TEXT NOT NULL,
  version TEXT NOT NULL,
  created_at REAL,
  status TEXT,                  -- active|candidate|retired|rolled_back
  confidence REAL,
  smoke_status TEXT,            -- untested|pass|fail
  smoke_detail TEXT,
  generated_by TEXT,
  json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playbooks_merchant ON playbooks(merchant, status);

CREATE TABLE IF NOT EXISTS filters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE,
  active INTEGER DEFAULT 0,
  json TEXT NOT NULL,
  updated_at REAL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS comps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT,                     -- normalized product key
  market_price REAL,
  source TEXT,
  sample_size INTEGER,
  updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_comps_key ON comps(key);

CREATE TABLE IF NOT EXISTS task_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  task TEXT,                    -- recon | shipping | comps | discord | worker
  source TEXT,                  -- who reported it
  status TEXT,                  -- ok | partial | rejected | error
  summary TEXT,
  items INTEGER DEFAULT 0,
  errors TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_taskruns_task ON task_runs(task, ts DESC);

CREATE TABLE IF NOT EXISTS merchant_sessions (
  merchant TEXT PRIMARY KEY,
  signed_in INTEGER DEFAULT 0,
  has_credentials INTEGER DEFAULT 0,
  last_login REAL,
  last_checked REAL,
  needs_manual_login INTEGER DEFAULT 0,
  note TEXT
);

CREATE TABLE IF NOT EXISTS job_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at REAL,
  deal_id INTEGER,
  kind TEXT,                    -- prep | place | verify
  payload TEXT,
  state TEXT,                   -- queued|leased|done|failed
  leased_by TEXT,
  leased_at REAL,
  result TEXT,
  attempts INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON job_queue(state, created_at);
"""


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=8000")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    migrate()
    seed()


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def q(sql: str, args: Iterable = ()) -> list[dict]:
    cur = connect().execute(sql, tuple(args))
    return [dict(r) for r in cur.fetchall()]


def q1(sql: str, args: Iterable = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: Iterable = ()) -> int:
    with tx() as conn:
        cur = conn.execute(sql, tuple(args))
        return cur.lastrowid


def execute_count(sql: str, args: Iterable = ()) -> int:
    """Like execute() but returns rows affected — used for atomic job leasing."""
    with tx() as conn:
        return conn.execute(sql, tuple(args)).rowcount


def insert(table: str, data: dict) -> int:
    cols = ",".join(data.keys())
    marks = ",".join("?" for _ in data)
    return execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(data.values()))


def update(table: str, row_id: int, data: dict) -> None:
    if not data:
        return
    sets = ",".join(f"{k}=?" for k in data)
    execute(f"UPDATE {table} SET {sets} WHERE id=?", list(data.values()) + [row_id])


def get_setting(key: str, default: Any = None) -> Any:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


DEFAULT_FILTERS = {
    "min_discount_pct": 40,
    "min_profit_per_unit": 25,
    "min_resale_ratio": 1.35,
    "max_unit_price": 900,
    "max_spend_per_deal": 1500,
    "max_qty_per_deal": 4,
    "min_demand_score": 0.35,
    "min_trust": "TRUSTED",
    "require_returnable": True,
    "min_return_window_days": 14,
    "max_restocking_fee_pct": 10,
    "allow_categories": [],
    "deny_categories": ["gift_card", "digital", "perishable", "adult"],
    "allow_sizes": [],
    "deny_sizes": [],
    "deny_merchants": [],
    "allow_merchants": [],
    "min_price_floor": 5,
    "require_in_stock": True,
    "block_final_sale": True,
    "max_cancel_risk": "high",
    "resale_fee_pct": 12,          # eBay/StockX style fee when you flip it
    "ship_cost_est": 9,            # what it costs you to ship one out
    "tax_rate": 0.08,              # used for credit-limit math, not for profit
    "allow_unknown_market": False, # no comp + no MSRP => don't guess, hold it
    "hold_instead_of_skip": True,  # near-misses land in HOLD for a human
}

SEED_MERCHANTS = [
    # domain, name, return_days, free_returns, cancel_risk, difficulty
    ("amazon.com", "Amazon", 30, 1, "medium", "medium"),
    ("walmart.com", "Walmart", 90, 1, "high", "medium"),
    ("target.com", "Target", 90, 1, "medium", "medium"),
    ("bestbuy.com", "Best Buy", 15, 1, "medium", "medium"),
    ("costco.com", "Costco", 90, 1, "low", "medium"),
    ("homedepot.com", "Home Depot", 90, 1, "medium", "easy"),
    ("lowes.com", "Lowe's", 90, 1, "medium", "easy"),
    ("nike.com", "Nike", 60, 1, "medium", "hard"),
    ("adidas.com", "Adidas", 30, 1, "medium", "hard"),
    ("macys.com", "Macy's", 30, 1, "medium", "easy"),
    ("nordstrom.com", "Nordstrom", 365, 1, "low", "easy"),
    ("dickssportinggoods.com", "Dick's", 90, 1, "medium", "easy"),
    ("newegg.com", "Newegg", 30, 0, "high", "medium"),
    ("bhphotovideo.com", "B&H", 30, 0, "low", "easy"),
    ("rei.com", "REI", 365, 1, "low", "easy"),
    ("zappos.com", "Zappos", 365, 1, "low", "easy"),
    ("samsclub.com", "Sam's Club", 90, 1, "medium", "medium"),
    ("kohls.com", "Kohl's", 180, 1, "medium", "easy"),
    ("staples.com", "Staples", 30, 1, "medium", "easy"),
    ("gamestop.com", "GameStop", 30, 0, "high", "easy"),
]


MIGRATIONS = [
    # (table, column, ddl)
    ("merchants", "shipping_pref", "TEXT DEFAULT 'cheapest'"),      # cheapest|standard|expedited|fastest|pickup
    ("merchants", "allow_paid_shipping", "INTEGER DEFAULT 0"),
    ("merchants", "max_shipping_cost", "REAL DEFAULT 0"),
    ("merchants", "expedite_over_value", "REAL DEFAULT 0"),         # pay to expedite above this order value
    ("merchants", "return_method", "TEXT DEFAULT ''"),              # mail_free|mail_paid|in_store|carrier_pickup
    ("merchants", "return_notes", "TEXT DEFAULT ''"),
    ("merchants", "login_required", "INTEGER DEFAULT 0"),
    ("deals", "is_test", "INTEGER DEFAULT 0"),
    ("orders", "shipping_cost", "REAL DEFAULT 0"),
    ("orders", "shipping_choice_reason", "TEXT DEFAULT ''"),
]


def migrate() -> None:
    """Add columns to tables that already exist in a deployed database."""
    conn = connect()
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in q(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.commit()


def seed() -> None:
    if not q1("SELECT id FROM filters WHERE active=1"):
        insert("filters", {"name": "default", "active": 1,
                           "json": json.dumps(DEFAULT_FILTERS), "updated_at": time.time()})
    if not q1("SELECT domain FROM merchants LIMIT 1"):
        with tx() as conn:
            for d, n, rw, fr, risk, diff in SEED_MERCHANTS:
                conn.execute(
                    "INSERT OR IGNORE INTO merchants(domain,name,trusted,return_window_days,"
                    "free_returns,restocking_fee_pct,cancel_risk,checkout_difficulty,updated_at) "
                    "VALUES(?,?,1,?,?,0,?,?,?)",
                    (d, n, rw, fr, risk, diff, time.time()),
                )
    if db_get_settings_missing():
        set_setting("auto_buy", False)
        set_setting("daily_spend_cap", config.MAX_SPEND_PER_DAY)


def db_get_settings_missing() -> bool:
    return q1("SELECT key FROM settings WHERE key='auto_buy'") is None
