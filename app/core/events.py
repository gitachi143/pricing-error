"""Append-only console log. Every decision the bot makes lands here."""
import json
import time

from .. import db


def log(deal_id: int | None, stage: str, message: str, level: str = "info", **data) -> None:
    db.insert("events", {
        "deal_id": deal_id, "ts": time.time(), "stage": stage, "level": level,
        "message": message, "data": json.dumps(data, default=str) if data else None,
    })


def recent(limit: int = 200, deal_id: int | None = None, level: str | None = None) -> list[dict]:
    sql = "SELECT * FROM events WHERE 1=1"
    args: list = []
    if deal_id:
        sql += " AND deal_id=?"; args.append(deal_id)
    if level:
        sql += " AND level=?"; args.append(level)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    rows = db.q(sql, args)
    for r in rows:
        if r.get("data"):
            try:
                r["data"] = json.loads(r["data"])
            except Exception:
                pass
    return rows
