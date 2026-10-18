import functools
import hashlib
import html
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import Header, HTTPException

TESTING = os.environ.get("AIRPOWER_TESTING") == "1"
LOCK = threading.RLock()
APP_DIR = Path(__file__).resolve().parent.parent
DB = sqlite3.connect(":memory:" if TESTING else APP_DIR / "airpower.db", check_same_thread=False)
DB.executescript(
    "CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, body TEXT, decision TEXT, decided_by TEXT, ts REAL);"
    "CREATE TABLE IF NOT EXISTS audit_log(n INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, who TEXT, role TEXT, act TEXT, prev TEXT, h TEXT);"
    "CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;"
    "CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;"
)


def _audit_key():
    if os.environ.get("AIRPOWER_AUDIT_KEY"):
        return os.environ["AIRPOWER_AUDIT_KEY"].encode()
    if TESTING:
        return b"test-only-key"
    path = APP_DIR / "airpower.auditkey"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_bytes()


AUDIT_KEY = _audit_key()


def _mac(previous, timestamp, who, role, action):
    payload = f"{previous}|{timestamp}|{who}|{role}|{action}".encode()
    return hmac.new(AUDIT_KEY, payload, hashlib.sha256).hexdigest()


def _key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _load_users():
    raw = os.environ.get("AIRPOWER_USERS")
    if raw:
        source = json.loads(raw)
    else:
        source = {
            "demo-analyst": ["analyst", "analyst"],
            "demo-officer": ["maint_officer", "maint_officer"],
            "demo-commander": ["commander", "commander"],
        }
        if not TESTING:
            print("WARNING: using DEMO API keys. Set AIRPOWER_USERS for anything beyond a demo.")
    return {_key_hash(key): tuple(value) for key, value in source.items()}


USERS = _load_users()


def _log(who, role, action):
    row = DB.execute("SELECT h FROM audit_log ORDER BY n DESC LIMIT 1").fetchone()
    previous = row[0] if row else "GENESIS"
    timestamp = time.time()
    DB.execute("INSERT INTO audit_log(ts,who,role,act,prev,h) VALUES(?,?,?,?,?,?)",
               (timestamp, who, role, action, previous, _mac(previous, timestamp, who, role, action)))
    DB.commit()


def log(who, role, action):
    with LOCK:
        return _log(who, role, action)


def locked(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with LOCK:
            return function(*args, **kwargs)
    return wrapper


def auth(*roles):
    def dependency(x_api_key: str = Header(default="")):
        user = USERS.get(_key_hash(x_api_key))
        if not user:
            raise HTTPException(401, "Missing/invalid API key")
        if roles and user[1] not in roles:
            raise HTTPException(403, f"Role '{user[1]}' may not do this")
        return user
    return dependency


def report_html(plan_id):
    """Build a printable, self-contained decision record for one plan."""
    row = DB.execute("SELECT body,decision,decided_by,ts FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such plan")
    plan, escape = json.loads(row[0]), html.escape
    fingerprint = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:16]
    when = lambda timestamp: time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(timestamp))
    trail = [entry for entry in DB.execute("SELECT ts,who,role,act FROM audit_log ORDER BY n")
             if re.search(rf"\b{re.escape(plan_id)}\b", entry[3])]
    metrics, robustness = plan.get("metrics"), plan.get("robustness")
    actions = "".join(
        f"<tr><td>{escape(action['aircraft'])}<td>{escape(action['part'])}<td>{escape(action['action'].replace('_', ' '))}"
        f"{' from ' + escape(action['donor']) if action.get('donor') else ''}"
        f"<td>D{action['part_ready_day']}<td>D{action['start']}→D{action['finish']}<td>{action['deferral_days']}d"
        f"<td>{action['slack_to_limit']}d<td>{escape('; '.join(item['kind'] + ': ' + item['why'] for item in action.get('rejected', [])) or '-')}</tr>"
        for action in plan.get("actions", []))
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>Decision record {escape(plan_id)}</title>"
        f"<style>body{{font:13px system-ui;max-width:980px;margin:24px auto;color:#111}}"
        f"table{{border-collapse:collapse;width:100%;margin:8px 0}}td,th{{border:1px solid #bbb;padding:5px 7px;text-align:left;vertical-align:top}}"
        f"h1{{font-size:20px}}h2{{font-size:14px;margin-top:20px}}.bad{{color:#b00020;font-weight:700}}"
        f".ok{{color:#0a7a2f;font-weight:700}}@media print{{button{{display:none}}}}</style></head><body>"
        f"<button onclick='window.print()'>Print / Save as PDF</button><h1>Air Power - plan decision record {escape(plan_id)}</h1>"
        f"<p><b>ADVISORY - synthetic demo data.</b> Not an authorization to perform maintenance.</p>"
        f"<p>Status: <b>{escape(str(plan.get('status')))}</b> · Decision: <b>{escape(row[1])}</b>"
        f"{' by ' + escape(row[2]) if row[2] else ''} · last updated {when(row[3])} · report generated {when(time.time())} · plan fingerprint <code>{fingerprint}</code></p>"
        f"<p>Independent verification: <span class='{'ok' if plan.get('verified') else 'bad'}'>"
        f"{'PASSED' if plan.get('verified') else 'FAILED / NOT APPLICABLE'}</span>"
        f"{'<br>' + escape('; '.join(plan.get('violations', []))) if plan.get('violations') else ''}</p>"
        f"<h2>Scenario (re-run these inputs to reproduce)</h2><pre>{escape(json.dumps(plan.get('scenario'), indent=1))}</pre>"
        f"<h2>Selected actions</h2><table><tr><th>Aircraft<th>Part<th>Action<th>Part ready<th>Work<th>Deferral<th>Slack to limit<th>Rejected alternatives</tr>"
        f"{actions or '<tr><td colspan=8>No actions: ' + escape(str(plan.get('reason', ''))) + '</tr>'}</table>"
        + (f"<h2>Impact vs no-action baseline</h2><p>Tasks on time {metrics['baseline']['on_time']}/{metrics['baseline']['total']} → "
           f"{metrics['plan']['on_time']}/{metrics['plan']['total']} · aircraft-days grounded {metrics['baseline']['grounded_days']} → "
           f"{metrics['plan']['grounded_days']} · donor-aircraft down days {metrics['plan']['donor_down_days']} · modeled extra cost {metrics['plan']['extra_cost']}</p>"
           if metrics else "")
        + (f"<h2>Supply-slip stress test</h2><p>{round(robustness['p_all_within_limits'] * 100)}% of {robustness['trials']} seeded trials kept every task inside its limit. "
           f"{escape(robustness['note'])}.</p>" if robustness else "")
        + f"<h2>Audit entries for this plan</h2><table>{''.join('<tr><td>' + when(entry[0]) + '<td>' + escape(entry[1]) + ' (' + escape(entry[2]) + ')<td>' + escape(entry[3]) + '</tr>' for entry in trail) or '<tr><td>none</tr>'}</table></body></html>"
    )