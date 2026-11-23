"""Regeln: anlegen, ändern, sortieren, löschen, jetzt anwenden. Nur für die eigenen Konten – auch Admins sehen
fremde Regeln nicht (sie verraten viel über die Mails eines Benutzers)."""
import json
import sqlite3
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import rules as R
from .. import sync
from ..db import audit, get_db
from ..security import current_user
from ..timeutil import local_iso, now_utc_str
from .accounts import get_account
from .messages import _imap_fail

router = APIRouter(prefix="/api/v1/rules", tags=["Regeln"])


class Condition(BaseModel):
    field: Literal["from", "to", "subject", "body", "attachment"]
    op: Optional[Literal["contains", "not_contains", "is", "ends_with"]] = "contains"
    value: Optional[str] = Field("", max_length=200)


class Action(BaseModel):
    type: Literal["move", "forward", "trash"]
    folder: Optional[str] = Field(None, max_length=500, description="Ordnerpfad am Server (bei move)")
    to: Optional[str] = Field(None, max_length=200, description="Adresse (bei forward)")


class RuleIn(BaseModel):
    name: Optional[str] = Field(None, max_length=80)
    account_id: Optional[int] = Field(None, description="leer = für alle eigenen Konten")
    match_all: Optional[bool] = True
    conditions: Optional[List[Condition]] = None
    actions: Optional[List[Action]] = None
    stop: Optional[bool] = False
    active: Optional[bool] = True
    position: Optional[int] = Field(None, ge=0, le=100000)


def _own(conn, user) -> set:
    return {r[0].lower() for r in conn.execute("SELECT email FROM accounts WHERE owner_id = ?", (user["id"],))}


def _folder_names(conn, user) -> dict:
    return {r["path"]: r["name"] for r in conn.execute(
        "SELECT f.path, f.name FROM folders f JOIN accounts a ON a.id = f.account_id WHERE a.owner_id = ?", (user["id"],))}


def rule_out(r: sqlite3.Row, names: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "account_id": r["account_id"], "position": r["position"],
            "active": bool(r["active"]), "match_all": bool(r["match_all"]), "stop": bool(r["stop"]),
            "conditions": json.loads(r["conditions"]), "actions": json.loads(r["actions"]),
            "hits": r["hits"], "last_hit": local_iso(r["last_hit"]), "last_error": r["last_error"],
            "summary": R.describe(r, names)}


def _get(conn, user, rule_id: int) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM rules WHERE id = ? AND owner_id = ?", (rule_id, user["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Regel nicht gefunden")
    return r


def _check(conn, user, account_id, conditions, actions) -> None:
    if account_id is not None:
        get_account(conn, user, account_id)  # nur eigene Konten
    try:
        R.validate([c.model_dump() for c in conditions], [a.model_dump() for a in actions], account_id, _own(conn, user))
    except R.RuleError as e:
        raise HTTPException(400, str(e))
    for a in actions:
        if a.type == "move":
            ok = conn.execute("SELECT 1 FROM folders WHERE account_id = ? AND path = ?", (account_id, a.folder)).fetchone()
            if not ok:
                raise HTTPException(400, "Zielordner gehört nicht zu diesem Konto")


@router.get("")
def list_rules(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    names = _folder_names(conn, user)
    rows = conn.execute("SELECT * FROM rules WHERE owner_id = ? ORDER BY position, id", (user["id"],)).fetchall()
    return {"rules": [rule_out(r, names) for r in rows], "fields": R.FIELDS, "ops": R.OPS, "actions": R.ACTIONS}


@router.post("", status_code=201)
def create_rule(data: RuleIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    conds, acts = data.conditions or [], data.actions or []
    _check(conn, user, data.account_id, conds, acts)
    pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM rules WHERE owner_id = ?", (user["id"],)).fetchone()[0]
    name = (data.name or "").strip() or "Regel " + str(pos)
    cur = conn.execute(
        "INSERT INTO rules(owner_id, account_id, name, position, active, match_all, conditions, actions, stop, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user["id"], data.account_id, name, pos, int(data.active is not False), int(data.match_all is not False),
         json.dumps([c.model_dump() for c in conds]), json.dumps([a.model_dump(exclude_none=True) for a in acts]),
         int(bool(data.stop)), now_utc_str()))
    audit(conn, user["id"], "rule_saved", f"user:{user['id']}", name)
    return rule_out(_get(conn, user, cur.lastrowid), _folder_names(conn, user))


@router.patch("/{rule_id}")
def update_rule(rule_id: int, data: RuleIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    old = _get(conn, user, rule_id)
    ch = data.model_dump(exclude_unset=True)
    account_id = ch["account_id"] if "account_id" in ch else old["account_id"]
    conds = data.conditions if "conditions" in ch else [Condition(**c) for c in json.loads(old["conditions"])]
    acts = data.actions if "actions" in ch else [Action(**a) for a in json.loads(old["actions"])]
    _check(conn, user, account_id, conds, acts)
    fields = {"account_id": account_id, "conditions": json.dumps([c.model_dump() for c in conds]),
              "actions": json.dumps([a.model_dump(exclude_none=True) for a in acts])}
    if ch.get("name") is not None:
        fields["name"] = data.name.strip() or old["name"]
    for k in ("active", "match_all", "stop"):
        if ch.get(k) is not None:
            fields[k] = int(ch[k])
    if ch.get("position") is not None:
        fields["position"] = data.position
    if "conditions" in ch or "actions" in ch:
        fields["last_error"] = None
    conn.execute(f"UPDATE rules SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", list(fields.values()) + [rule_id])
    audit(conn, user["id"], "rule_saved", f"user:{user['id']}", fields.get("name", old["name"]))
    return rule_out(_get(conn, user, rule_id), _folder_names(conn, user))


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    r = _get(conn, user, rule_id)
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    audit(conn, user["id"], "rule_deleted", f"user:{user['id']}", r["name"])
    return {"ok": True}


class OrderIn(BaseModel):
    ids: List[int] = Field(..., max_length=500)


@router.post("/order")
def reorder(data: OrderIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    own = {r[0] for r in conn.execute("SELECT id FROM rules WHERE owner_id = ?", (user["id"],))}
    if set(data.ids) != own:
        raise HTTPException(400, "Bitte alle eigenen Regeln in der neuen Reihenfolge angeben")
    for pos, rid in enumerate(data.ids, 1):
        conn.execute("UPDATE rules SET position = ? WHERE id = ?", (pos, rid))
    return list_rules(user, conn)


class ApplyIn(BaseModel):
    dry_run: bool = True


@router.post("/{rule_id}/apply")
def apply_now(rule_id: int, data: ApplyIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Regel auf die Mails anwenden, die schon im Posteingang liegen (zwischengespeicherter Zeitraum).
    Weiterleiten passiert dabei nie – nur Verschieben und Papierkorb. dry_run: nur zählen."""
    rule = _get(conn, user, rule_id)
    accounts = [get_account(conn, user, rule["account_id"])] if rule["account_id"] else conn.execute(
        "SELECT * FROM accounts WHERE owner_id = ? AND active = 1", (user["id"],)).fetchall()
    acts = json.loads(rule["actions"])
    if not any(a["type"] in ("move", "trash") for a in acts):
        raise HTTPException(400, "Diese Regel leitet nur weiter – das passiert nur bei neuen Mails, nicht nachträglich")
    total, sample, done = 0, [], {"moved": 0, "trashed": 0, "errors": 0}
    for account in accounts:
        inbox = sync.folder_by_role(conn, account["id"], "inbox")
        if not inbox:
            continue
        msgs = [m for m in conn.execute("SELECT * FROM messages WHERE folder_id = ? ORDER BY date_utc DESC", (inbox["id"],))
                if R.matches(rule, m)]
        total += len(msgs)
        sample += [{"subject": m["subject"], "from": m["from_name"] or m["from_addr"]} for m in msgs[:5 - len(sample)]]
        if not data.dry_run and msgs:
            try:
                with sync.open_client(account) as client:
                    stats = R.apply(conn, account, client, inbox, msgs, [rule], allow_forward=False)
            except Exception as e:  # noqa: BLE001
                _imap_fail(e)
            for k in done:
                done[k] += stats[k]
            for f in conn.execute("SELECT id FROM folders WHERE account_id = ?", (account["id"],)):
                sync._update_counts(conn, f["id"])
            sync.start_sync(account["id"])
    if not data.dry_run:
        audit(conn, user["id"], "rule_applied", f"user:{user['id']}", f"{rule['name']}: {total} Mails")
    return {"matches": total, "sample": sample, "applied": not data.dry_run, **done}
