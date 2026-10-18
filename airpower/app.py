import hmac
import json
import os
import time
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from .core import AIRCRAFT, PARTS, TASKS, apply_whatif, assess, diff_plans, modify_plan, risk_14_day, solve, spare_position_14d, verify
from .persistence import DB, LOCK, _mac, auth, locked, log, report_html
from .ui import HTML

load_dotenv()

app = FastAPI(title="Air Power Decision Support")


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


allowed_origins = _split_csv(os.getenv("ALLOWED_ORIGINS")) or [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "https://lillyboy-aero-logistics.hf.space",
    "https://huggingface.co",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

NN = Annotated[int, Field(ge=0, le=365)]


class Req(BaseModel):
    disable: list[Annotated[str, Field(max_length=64, pattern=r"^[A-Za-z0-9_:.\-]+$")]] = Field(default=[], max_length=20)
    stock: dict[str, NN] = {}
    delay: dict[str, NN] = {}

    @field_validator("stock", "delay")
    @classmethod
    def _known_parts(cls, value):
        unknown = [part_number for part_number in value if part_number not in PARTS]
        if unknown:
            raise ValueError(f"unknown part number(s): {unknown}")
        return value


class CmpReq(BaseModel):
    base: Req = Req()
    alt: Req = Req()


class ModReq(BaseModel):
    edits: dict[str, dict] = {}


@app.get("/api/fleet")
def fleet(user=Depends(auth())):
    return {"aircraft": AIRCRAFT, "tasks": TASKS}


@app.post("/api/risk")
def risk(request: Req, user=Depends(auth())):
    return assess(apply_whatif(request.stock, request.delay))


@app.post("/api/parts")
def parts_(request: Req, user=Depends(auth())):
    return apply_whatif(request.stock, request.delay)


@app.post("/api/optimize")
def optimize(request: Req, user=Depends(auth())):
    plan = solve(apply_whatif(request.stock, request.delay), request.disable)
    with LOCK:
        plan["id"] = f"P{DB.execute('SELECT COUNT(*) FROM plans').fetchone()[0]+1}"
        plan["decision"] = "PENDING"
        plan["scenario"] = request.model_dump()
        DB.execute("INSERT INTO plans VALUES(?,?,?,?,?)",
                   (plan["id"], json.dumps(plan), "PENDING", None, time.time()))
        DB.commit()
        log(user[0], user[1], f"created {plan['id']} [{plan['status']}] scenario={json.dumps(request.model_dump())}")
    return plan


@app.post("/api/compare")
def compare(request: CmpReq, user=Depends(auth())):
    base, alternative = (solve(apply_whatif(item.stock, item.delay), item.disable)
                         for item in (request.base, request.alt))
    return {"base": base, "alt": alternative, "diff": diff_plans(base, alternative)}


@app.post("/api/plans/{pid}/decide")
@locked
def decide(pid: str, decision: str, user=Depends(auth("maint_officer", "commander"))):
    row = DB.execute("SELECT body,decision FROM plans WHERE id=?", (pid,)).fetchone()
    if not row or decision not in ("APPROVED", "REJECTED"):
        raise HTTPException(400, "bad request (use /modify to change a plan)")
    if row[1] != "PENDING":
        raise HTTPException(409, f"already {row[1]}")
    body = json.loads(row[0])
    body["decision"] = decision
    if decision == "APPROVED":
        scenario = body.get("scenario") or {}
        try:
            valid_now, violations = verify(body, apply_whatif(scenario.get("stock"), scenario.get("delay")))
        except Exception as error:
            valid_now, violations = False, [f"verification error: {error}"]
        if not body.get("verified") or not valid_now:
            raise HTTPException(409, "plan failed verification and cannot be approved: " + "; ".join(violations[:3]))
    DB.execute("UPDATE plans SET body=?,decision=?,decided_by=?,ts=? WHERE id=?",
               (json.dumps(body), decision, user[0], time.time(), pid))
    DB.commit()
    log(user[0], user[1], f"{decision} {pid}")
    return body


@app.post("/api/plans/{pid}/modify")
@locked
def modify_endpoint(pid: str, request: ModReq, user=Depends(auth("maint_officer", "commander"))):
    row = DB.execute("SELECT body,decision FROM plans WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "no such plan")
    if row[1] != "PENDING":
        raise HTTPException(409, f"already {row[1]}")
    old = json.loads(row[0])
    if not old.get("actions"):
        raise HTTPException(400, "plan has no actions to modify")
    scenario = old["scenario"]
    try:
        new = modify_plan(old, request.edits, apply_whatif(scenario["stock"], scenario["delay"]))
    except (ValueError, TypeError, KeyError) as error:
        raise HTTPException(400, f"invalid modification: {error}")
    new["id"] = f"P{DB.execute('SELECT COUNT(*) FROM plans').fetchone()[0]+1}"
    new["decision"] = "PENDING"
    new["scenario"] = scenario
    new["parent"] = pid
    old["decision"] = "MODIFIED"
    old["superseded_by"] = new["id"]
    DB.execute("UPDATE plans SET body=?,decision=?,decided_by=?,ts=? WHERE id=?",
               (json.dumps(old), "MODIFIED", user[0], time.time(), pid))
    DB.execute("INSERT INTO plans VALUES(?,?,?,?,?)", (new["id"], json.dumps(new), "PENDING", None, time.time()))
    DB.commit()
    log(user[0], user[1], f"MODIFIED {pid} -> {new['id']} verified={new['verified']} edits={json.dumps(request.edits)}")
    return new


@app.get("/health")
def health():
    return {"ok": True, "tasks": len(TASKS), "aircraft": len(AIRCRAFT)}


@app.get("/api/plans")
@locked
def plans_list(user=Depends(auth())):
    output = []
    for plan_id, body, decision, decided_by, timestamp in DB.execute(
            "SELECT id,body,decision,decided_by,ts FROM plans ORDER BY rowid DESC LIMIT 100"):
        plan = json.loads(body)
        output.append({"id": plan_id, "status": plan.get("status"), "decision": decision,
                       "decided_by": decided_by, "ts": timestamp, "verified": plan.get("verified"),
                       "parent": plan.get("parent")})
    return output


@app.get("/api/plans/{pid}")
@locked
def plan_get(pid: str, user=Depends(auth())):
    row = DB.execute("SELECT body FROM plans WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "no such plan")
    return json.loads(row[0])


@app.get("/api/audit")
@locked
def audit(user=Depends(auth())):
    return [dict(zip(("n", "ts", "who", "role", "act"), row))
            for row in DB.execute("SELECT n,ts,who,role,act FROM audit_log ORDER BY n DESC LIMIT 500")]


@app.get("/api/audit/verify")
@locked
def audit_verify(user=Depends(auth())):
    previous = "GENESIS"
    rows = DB.execute("SELECT n,ts,who,role,act,prev,h FROM audit_log ORDER BY n").fetchall()
    for number, timestamp, who, role, action, stored_previous, stored_hash in rows:
        if stored_previous != previous or not hmac.compare_digest(
            stored_hash, _mac(previous, timestamp, who, role, action)):
            return {"intact": False, "broken_at": number}
        previous = stored_hash
    return {"intact": True}


@app.get("/api/v1/spares/position")
@locked
def spares_position(part: str | None = None, horizon: int = Query(default=14, ge=0, le=30), user=Depends(auth())):
    data = spare_position_14d(parts=PARTS, tasks=TASKS, horizon=horizon)
    if part is None:
        return data
    if part not in data["parts"]:
        raise HTTPException(404, f"no such part {part}")
    return {"synthetic_data": True, "horizon_days": horizon, "parts": {part: data["parts"][part]}}


@app.get("/api/v1/risk/14-day")
@locked
def risk_14_day_api(part: str | None = None, horizon: int = Query(default=14, ge=0, le=30), user=Depends(auth())):
    try:
        return risk_14_day(parts=PARTS, tasks=TASKS, horizon=horizon, part=part)
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.get("/api/plans/{pid}/report", response_class=HTMLResponse)
@locked
def report(pid: str, user=Depends(auth())):
    return report_html(pid)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML