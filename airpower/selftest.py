"""Self-test harness for the Air Power prototype.

Important: the suite intentionally exercises the database-backed workflow, including
plan creation and audit-chain validation, so it mutates SQLite state while running.
The in-memory test mode below isolates those side effects from the user's working DB.
"""

import hashlib
import json
import os
import sqlite3
import time

os.environ.setdefault("AIRPOWER_TESTING", "1")

from fastapi import HTTPException

from .app import CmpReq, ModReq, Req, app, audit, audit_verify, compare, decide, health, modify_endpoint, optimize, plan_get, plans_list
from .core import PARTS, TASKS, apply_whatif, assess, baseline, metrics, robustness, solve, verify
from .persistence import DB, report_html


def run_tests():
    P = apply_whatif
    ok = True
    checks_run = 0
    passed = 0
    failed = 0
    skipped = 0

    def chk(name, condition):
        nonlocal ok, checks_run, passed, failed
        checks_run += 1
        result = bool(condition)
        ok = ok and result
        if result:
            passed += 1
        else:
            failed += 1
        print(("PASS " if result else "FAIL ") + name)

    def skip(label):
        nonlocal skipped
        skipped += 1
        print(f"SKIP {label}")

    b = solve()
    chk("base plan optimal + verified", b["status"] == "OPTIMAL" and b["verified"])
    chk("base: T1 does not rely on donor", next(a for a in b["actions"] if a["task"] == "T1")["action"] == "urgent_indent")
    chk("base: all finish within limits", all(a["slack_to_limit"] >= 0 for a in b["actions"]))
    n = solve(disabled={"PN-401:urgent_indent"})
    chk("no indent -> cannibalize, verified", n["verified"] and next(a for a in n["actions"] if a["task"] == "T1")["donor"] == "KF-407")
    chk("no indent/transfer/expedite -> infeasible (not invented)", solve(disabled={"PN-401:urgent_indent", "PN-401:cannibalize", "PN-401:expedite_repair"})["status"] == "INFEASIBLE")
    d = solve(P(delay={"PN-401": 3}))
    chk("delayed indent (+3d) -> infeasible or donor", d["status"] == "INFEASIBLE" or d["verified"])
    s = solve(P(stock={"PN-212": 2}))
    chk("extra PN-212 stock -> both T2,T3 use stock", [a["action"] for a in s["actions"] if a["part"] == "PN-212"] == ["use_stock"] * 2)
    s0 = solve(P(stock={"PN-212": 0}))
    chk("PN-212 stock 0 -> T2 (due 6) cannot be served -> infeasible", s0["status"] == "INFEASIBLE")
    ld = solve(P(stock={"PN-212": 1}, delay={"PN-212": 0}))
    idc = [a for a in ld["actions"] if a["action"] == "urgent_indent" and a["part"] == "PN-212"]
    chk("PN-212 indent lot qty respected (verified)", ld["verified"])
    bad = json.loads(json.dumps(b))
    bad["actions"][0]["finish"] += 5
    chk("verifier catches a late finish", not verify(bad, PARTS)[0])
    bad = json.loads(json.dumps(b))
    bad["actions"][0]["group"] = bad["actions"][1]["group"]
    chk("verifier catches over-allocated supply lot", not verify(bad, PARTS)[0])
    chk("risk board flags T1 and T3 (T3 loses PN-212 stock to T2)", [r["at_risk"] for r in assess()] == [True, False, True])

    forged = json.loads(json.dumps(b))
    action = forged["actions"][0]
    action["part_ready_day"] = 0
    action["start"] = 0
    action["finish"] = action["start"] + 2
    chk("verifier catches forged part-ready day", any("part-ready" in v for v in verify(forged, PARTS)[1]))
    forged = json.loads(json.dumps(b))
    forged["disabled"] = ["PN-401:urgent_indent"]
    chk("verifier catches a disabled/forbidden action", any("not a permitted" in v for v in verify(forged, PARTS)[1]))
    forged = json.loads(json.dumps(b))
    forged["actions"][0]["group"] = "PN-401:madeup"
    chk("verifier catches an invented supply lot", not verify(forged, PARTS)[0])
    forged = json.loads(json.dumps(b))
    forged["actions"].pop()
    chk("verifier catches a missing task", not verify(forged, PARTS)[0])

    forged = json.loads(json.dumps(b))
    forged["actions"][0]["start"] = 97
    forged["actions"][0]["finish"] = 99
    chk("grounded days computed from schedule", metrics(forged, baseline(PARTS))["plan"]["grounded_days"] > 0 and b["metrics"]["plan"]["grounded_days"] == 0)

    def code(function, *args):
        try:
            function(*args)
            return 200
        except HTTPException as error:
            return error.status_code

    analyst, officer = ("tester", "analyst"), ("officer", "maint_officer")
    p1 = optimize(Req(), analyst)
    chk("optimize stores a PENDING plan", p1["decision"] == "PENDING")
    m1 = modify_endpoint(p1["id"], ModReq(edits={"T1": {"action": "cannibalize", "start": 2}}), officer)
    chk("valid modification -> new verified PENDING plan", m1["verified"] and m1["decision"] == "PENDING" and m1["parent"] == p1["id"]
        and next(a for a in m1["actions"] if a["task"] == "T1")["donor"] == "KF-407")
    chk("original plan is superseded (cannot be decided)", code(decide, p1["id"], "APPROVED", officer) == 409)
    chk("modified plan can be approved", decide(m1["id"], "APPROVED", officer)["decision"] == "APPROVED")
    p2 = optimize(Req(), analyst)
    m2 = modify_endpoint(p2["id"], ModReq(edits={"T1": {"start": 0}}), officer)
    chk("modification starting before part is ready -> unverified", not m2["verified"])
    chk("unverified modified plan cannot be approved", code(decide, m2["id"], "APPROVED", officer) == 409)
    p3 = optimize(Req(), analyst)
    chk("modification with unavailable action is rejected (400)", code(modify_endpoint, p3["id"], ModReq(edits={"T1": {"action": "use_stock"}}), officer) == 400)
    chk("'MODIFIED' is no longer a plain decision (400)", code(decide, p3["id"], "MODIFIED", officer) == 400)

    from pydantic import ValidationError

    def bad_req(**kwargs):
        try:
            Req(**kwargs)
            return False
        except ValidationError:
            return True

    chk("input validation rejects unknown part / negative values", bad_req(stock={"PN-999": 1}) and bad_req(delay={"PN-401": -1}) and bad_req(stock={"PN-401": -3}))
    chk("infeasible plan explains why", any("T2" in item for item in s0["diagnosis"]))
    first, second = robustness(b, PARTS), robustness(b, PARTS)
    chk("stress test is deterministic and bounded", first == second and 0 <= first["p_all_within_limits"] <= 1 and set(first["per_task"]) == {"T1", "T2", "T3"})
    all_stock = solve(P(stock={"PN-401": 1, "PN-212": 2}))
    chk("all-stock plan has no supply-slip risk", all_stock["robustness"]["p_all_within_limits"] == 1.0)
    chk("indent-dependent plan is flagged fragile", b["robustness"]["p_all_within_limits"] < 1.0)
    malformed = json.loads(json.dumps(b))
    del malformed["actions"][0]["start"]
    malformed["actions"][1]["cost"] = "free"
    try:
        result = verify(malformed, PARTS)[0]
    except Exception:
        result = "crashed"
    chk("verifier rejects malformed actions without crashing", result is False)
    comparison = compare(CmpReq(base=Req(), alt=Req(stock={"PN-401": 1})), analyst)
    chk("compare: extra stock changes T1 and lowers cost", comparison["diff"]["comparable"] and comparison["diff"]["delta"]["extra_cost"] < 0 and any(item["changed"] for item in comparison["diff"]["rows"]))
    chk("compare: infeasible side is reported, not hidden", not compare(CmpReq(alt=Req(stock={"PN-212": 0})), analyst)["diff"]["comparable"])

    plan = optimize(Req(), analyst)
    forged = json.loads(DB.execute("SELECT body FROM plans WHERE id=?", (plan["id"],)).fetchone()[0])
    forged["actions"][0]["start"] = 0
    forged["actions"][0]["finish"] = 2
    DB.execute("UPDATE plans SET body=? WHERE id=?", (json.dumps(forged), plan["id"]))
    DB.commit()
    chk("tampered stored plan fails re-verification at approval (409)", code(decide, plan["id"], "APPROVED", officer) == 409)
    chk("disable list rejects HTML/script payloads", bad_req(disable=["<script>alert(1)</script>"]) and not bad_req(disable=["PN-401:urgent_indent"]))
    plan = optimize(Req(), analyst)
    body = json.loads(DB.execute("SELECT body FROM plans WHERE id=?", (plan["id"],)).fetchone()[0])
    body["scenario"]["disable"] = ["<script>alert(1)</script>"]
    DB.execute("UPDATE plans SET body=? WHERE id=?", (json.dumps(body), plan["id"]))
    DB.commit()
    rendered_report = report_html(plan["id"])
    chk("report escapes user-supplied text and carries a fingerprint", "<script>alert(1)" not in rendered_report and "&lt;script&gt;" in rendered_report and "fingerprint" in rendered_report)
    chk("report includes audit trail for its plan", "created " + plan["id"] in rendered_report)

    original_tasks = list(TASKS)
    try:
        TASKS[:] = [{"id": f"X{i}", "aircraft": f"AC-{i}", "kind": "Scheduled inspection", "due": 6 + 2 * i,
                     "tol": 2, "dur": 2, "part": "PN-300", "qty": 1, "prio": 1 + i % 3} for i in range(10)]
        large_parts = {"PN-300": {"desc": "Test part", "on_hand": 6, "reserved": 0, "repair": [],
                                   "procurement": [{"qty": 6, "promised": 8, "realistic": 9}],
                                   "urgent_indent": {"available": False, "eta": 99, "cost": 0, "qty": 0}, "installed_on": []}}
        start_time = time.time()
        large_plan = solve(large_parts)
        chk("10-aircraft plan solves quickly, verified, lots/bays respected", large_plan["status"] in ("OPTIMAL", "FEASIBLE") and large_plan["verified"] and time.time() - start_time < 10)
        chk("scale: stock lot never over-allocated", sum(1 for action in large_plan["actions"] if action["action"] == "use_stock") <= 6)
    finally:
        TASKS[:] = original_tasks

    try:
        from fastapi.testclient import TestClient
        client, headers = TestClient(app), lambda key: {"X-API-Key": key}
        chk("HTTP: missing key -> 401", client.post("/api/optimize", json={}).status_code == 401)
        chk("HTTP: negative stock -> 422", client.post("/api/optimize", json={"stock": {"PN-401": -1}}, headers=headers("demo-analyst")).status_code == 422)
        response = client.post("/api/optimize", json={}, headers=headers("demo-analyst"))
        chk("HTTP: analyst can plan", response.status_code == 200 and response.json()["verified"])
        chk("HTTP: analyst cannot approve -> 403", client.post(f"/api/plans/{response.json()['id']}/decide?decision=APPROVED", headers=headers("demo-analyst")).status_code == 403)
        chk("HTTP: officer approves", client.post(f"/api/plans/{response.json()['id']}/decide?decision=APPROVED", headers=headers("demo-officer")).json()["decision"] == "APPROVED")
        chk("HTTP: report served", "decision record" in client.get(f"/api/plans/{response.json()['id']}/report", headers=headers("demo-analyst")).text)
        ui = client.get("/").text
        chk("HTTP: UI served + escapes server text", "Maintenance timeline" in ui and "const esc=" in ui)
        chk("HTTP: script payload in disable -> 422", client.post("/api/optimize", json={"disable": ["<img src=x onerror=alert(1)>"]}, headers=headers("demo-analyst")).status_code == 422)
    except ImportError:
        skip("HTTP tests (pip install httpx)")

    plans = plans_list(analyst)
    chk("plan list returns stored plans, newest first", len(plans) >= 5 and int(plans[0]["id"][1:]) > int(plans[-1]["id"][1:]))
    chk("plan_get returns a plan; unknown id -> 404", plan_get(plans[0]["id"], analyst)["id"] == plans[0]["id"] and code(plan_get, "P9999", analyst) == 404)
    chk("health ok", health()["ok"])
    chk("audit chain intact", audit_verify(analyst)["intact"])
    try:
        DB.execute("UPDATE audit_log SET act='x' WHERE n=1")
        updated = False
    except sqlite3.DatabaseError:
        updated = True
    chk("audit log rejects UPDATE (append-only trigger)", updated)
    DB.execute("DROP TRIGGER audit_no_update")
    DB.execute("UPDATE audit_log SET act='forged' WHERE n=1")
    DB.commit()
    chk("tampering detected after bypassing trigger", not audit_verify(analyst)["intact"])
    previous = "GENESIS"
    rows = DB.execute("SELECT n,ts,who,role,act FROM audit_log ORDER BY n").fetchall()
    for number, timestamp, who, role, action in rows:
        signature = hashlib.sha256(f"{previous}|{timestamp}|{who}|{role}|{action}".encode()).hexdigest()
        DB.execute("UPDATE audit_log SET prev=?, h=? WHERE n=?", (previous, signature, number))
        previous = signature
    DB.commit()
    chk("re-hashed chain without the key still fails", not audit_verify(analyst)["intact"])
    print(f"SUMMARY: {checks_run} checks executed, {passed} passed, {failed} failed, {skipped} skipped")
    print("ALL PASSED" if ok else "SOME FAILED")
    return ok


if __name__ == "__main__":
    result = run_tests()
    raise SystemExit(0 if result else 1)