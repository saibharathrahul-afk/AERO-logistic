import copy
import random

from ortools.sat.python import cp_model

AIRCRAFT = [
    {"id": "KF-402", "status": "Serviceable", "hours": 1840}, {"id": "KF-405", "status": "Serviceable", "hours": 2210},
    {"id": "KF-407", "status": "Serviceable", "hours": 1320}, {"id": "KF-410", "status": "Serviceable", "hours": 980},
    {"id": "KF-411", "status": "Serviceable", "hours": 2005}, {"id": "KF-415", "status": "Unserviceable", "hours": 2650}]
TASKS = [
    {"id": "T1", "aircraft": "KF-402", "kind": "Scheduled inspection", "due": 8, "tol": 2, "dur": 2, "part": "PN-401", "qty": 1, "prio": 2},
    {"id": "T2", "aircraft": "KF-405", "kind": "DD expiry rectification", "due": 6, "tol": 0, "dur": 2, "part": "PN-212", "qty": 1, "prio": 3},
    {"id": "T3", "aircraft": "KF-411", "kind": "Scheduled inspection", "due": 9, "tol": 1, "dur": 2, "part": "PN-212", "qty": 1, "prio": 1}]
PARTS = {
    "PN-401": {"desc": "Hydraulic actuator", "on_hand": 0, "reserved": 0,
               "repair": [{"qty": 1, "promised": 10, "realistic": 13}],
               "procurement": [{"qty": 1, "promised": 11, "realistic": 14}],
               "urgent_indent": {"available": True, "eta": 7, "cost": 8, "qty": 2},
               "installed_on": [{"aircraft": "KF-407", "authorized_transfer": True, "labour_days": 2, "donor_replace_eta": 13}]},
    "PN-212": {"desc": "Fuel control valve", "on_hand": 1, "reserved": 0, "repair": [],
               "procurement": [{"qty": 2, "promised": 7, "realistic": 10}],
               "urgent_indent": {"available": True, "eta": 8, "cost": 6, "qty": 2}, "installed_on": []},
    "PN-303": {"desc": "Synthetic landing gear sensor", "on_hand": 2, "reserved": 1,
               "repair": [{"qty": 1, "promised": 6, "realistic": 8}],
               "procurement": [{"qty": 2, "promised": 9, "realistic": 12}],
               "urgent_indent": {"available": True, "eta": 5, "cost": 4, "qty": 1},
               "installed_on": []}}
RES = {"bays": 2, "horizon": 30}
W = {"deferral_day": 30, "donor_down_day": 15, "expedite_cost": 5}


def build_options(t, parts, disabled):
    """Candidate actions for a task; options using the same lot share finite capacity."""
    pn, p, opts, groups = t["part"], parts[t["part"]], [], {}

    def add(kind, group, quantity, ready, cost, **kwargs):
        groups[group] = quantity
        opts.append(dict(kind=kind, group=group, ready=ready, cost=cost, **kwargs))

    if p["on_hand"] - p["reserved"] > 0:
        add("use_stock", f"{pn}:stock", p["on_hand"] - p["reserved"], 0, 0)
    for i, repair in enumerate(p["repair"]):
        add("await_repair", f"{pn}:repair{i}", repair["qty"], repair["realistic"], 0)
        add("expedite_repair", f"{pn}:repair{i}", repair["qty"], max(0, repair["realistic"] - 2), W["expedite_cost"])
    for i, delivery in enumerate(p["procurement"]):
        add("await_delivery", f"{pn}:proc{i}", delivery["qty"], delivery["realistic"], 0)
    indent = p["urgent_indent"]
    if indent["available"]:
        add("urgent_indent", f"{pn}:indent", indent["qty"], indent["eta"], indent["cost"])
    for donor in p["installed_on"]:
        if not donor["authorized_transfer"]:
            continue
        if any(task["aircraft"] == donor["aircraft"] and task["due"] < donor["donor_replace_eta"] for task in TASKS):
            continue
        down = max(0, donor["donor_replace_eta"] - donor["labour_days"])
        add("cannibalize", f"{pn}:donor:{donor['aircraft']}", 1, donor["labour_days"],
            down * W["donor_down_day"], donor=donor["aircraft"], donor_down_days=down)
    return [option for option in opts if option["kind"] not in disabled and f"{pn}:{option['kind']}" not in disabled], groups


def apply_whatif(stock=None, delay=None):
    parts = copy.deepcopy(PARTS)
    for part_number, quantity in (stock or {}).items():
        if part_number in parts:
            parts[part_number]["on_hand"] = max(0, int(quantity))
    for part_number, days in (delay or {}).items():
        if part_number in parts:
            for delivery in parts[part_number]["repair"] + parts[part_number]["procurement"]:
                delivery["realistic"] += max(0, int(days))
            parts[part_number]["urgent_indent"]["eta"] += max(0, int(days))
    return parts


def baseline(parts):
    """Do nothing new: allocate stock and deliveries in due-date order."""
    capacity, rows = {}, {}
    for task in sorted(TASKS, key=lambda item: item["due"]):
        options, groups = build_options(task, parts, {"urgent_indent", "cannibalize", "expedite_repair"})
        for group, quantity in groups.items():
            capacity.setdefault(group, quantity)
        available = [option for option in options if capacity[option["group"]] >= task["qty"]]
        if available:
            selected = min(available, key=lambda option: option["ready"])
            capacity[selected["group"]] -= task["qty"]
            ready, source = selected["ready"], selected["kind"]
        else:
            ready, source = RES["horizon"], "no supply"
        finish = ready + task["dur"]
        rows[task["id"]] = {"ready": ready, "source": source, "end": finish,
                             "late": max(0, finish - task["due"]),
                             "grounded": max(0, finish - task["due"] - task["tol"])}
    return rows


def _part_incoming_events(part):
    events = []
    for stage, key in ((part.get("repair", []), "repair"), (part.get("procurement", []), "procurement")):
        for item in stage:
            qty = int(item.get("qty", 0))
            if qty <= 0:
                continue
            arrival_day = int(item.get("realistic", item.get("promised", 0)))
            events.append({
                "source": key,
                "qty": qty,
                "day": arrival_day,
                "eta_assumed": arrival_day > int(item.get("promised", arrival_day)),
                "label": f"{key} qty {qty} arrives D{arrival_day}",
            })
    indent = part.get("urgent_indent") or {}
    if indent.get("available"):
        qty = int(indent.get("qty", 0))
        eta = int(indent.get("eta", 0))
        events.append({
            "source": "urgent_indent",
            "qty": qty,
            "day": eta,
            "eta_assumed": False,
            "label": f"urgent indent qty {qty} arrives D{eta}",
        })
    return sorted(events, key=lambda entry: (entry["day"], entry["source"], entry["qty"]))


def _part_demand_events(tasks, part_number):
    demand = {}
    for task in tasks:
        if task.get("part") != part_number:
            continue
        day = int(task.get("due", 0))
        demand[day] = demand.get(day, 0) + int(task.get("qty", 1))
    return {day: qty for day, qty in sorted(demand.items())}


def spare_position_14d(parts=None, tasks=None, horizon=14):
    """Project daily on-hand position, demand, incoming supply and shortage for each part."""
    parts = copy.deepcopy(parts or PARTS)
    tasks = tasks or TASKS
    days = list(range(max(0, int(horizon)) + 1))
    all_parts = {}
    for part_number, part in sorted(parts.items()):
        available = max(0, int(part.get("on_hand", 0)) - int(part.get("reserved", 0)))
        demand_by_day = _part_demand_events(tasks, part_number)
        incoming_events = _part_incoming_events(part)
        incoming_by_day = {}
        for event in incoming_events:
            incoming_by_day[event["day"]] = incoming_by_day.get(event["day"], 0) + event["qty"]
        daily = []
        shortage_events = []
        for day in days:
            scheduled_in = incoming_by_day.get(day, 0)
            required = demand_by_day.get(day, 0)
            available_after = max(0, available + scheduled_in - required)
            shortage = max(0, required - (available + scheduled_in))
            if shortage > 0:
                shortage_events.append({
                    "day": day,
                    "required": required,
                    "available": max(0, available + scheduled_in),
                    "shortage": shortage,
                    "demand_sources": [task["id"] for task in tasks if task.get("part") == part_number and int(task.get("due", 0)) == day],
                    "incoming_sources": [event["label"] for event in incoming_events if event["day"] == day],
                })
            daily.append({
                "day": day,
                "start_available": available,
                "incoming": scheduled_in,
                "demand": required,
                "end_available": available_after,
                "shortage": shortage,
                "demand_sources": [task["id"] for task in tasks if task.get("part") == part_number and int(task.get("due", 0)) == day],
                "incoming_sources": [event["label"] for event in incoming_events if event["day"] == day],
            })
            available = available_after
        all_parts[part_number] = {
            "desc": part.get("desc", ""),
            "on_hand": int(part.get("on_hand", 0)),
            "reserved": int(part.get("reserved", 0)),
            "projected_available": max(0, int(part.get("on_hand", 0)) - int(part.get("reserved", 0))),
            "incoming_events": incoming_events,
            "daily": daily,
            "shortages": shortage_events,
        }
    return {"synthetic_data": True, "horizon_days": int(horizon), "parts": all_parts}


def _task_shortage(task, parts, horizon=14):
    part_number = task.get("part")
    part = parts.get(part_number)
    if not part:
        return None
    position = spare_position_14d(parts={part_number: part}, tasks=[task], horizon=horizon)
    daily = position["parts"][part_number]["daily"]
    target_day = int(task.get("due", 0))
    if target_day > horizon:
        return None
    record = daily[target_day]
    if record["shortage"] <= 0:
        return None
    demand_sources = [event["label"] for event in _part_incoming_events(part) if event["day"] == target_day]
    return {
        "task": task["id"],
        "aircraft": task["aircraft"],
        "part": part_number,
        "required_qty": int(task.get("qty", 1)),
        "available_qty": int(record["end_available"]),
        "shortage_day": target_day,
        "evidence": [
            f"Demand: {task['id']} requires {task['qty']} {part_number} by day {target_day}",
            *([f"Incoming: {item}" for item in demand_sources] if demand_sources else []),
            *([f"Supply: {event['label']}" for event in _part_incoming_events(part) if event["day"] <= target_day] if _part_incoming_events(part) else []),
        ],
        "eta_assumed": any(event["eta_assumed"] for event in _part_incoming_events(part) if event["day"] <= target_day),
    }


def risk_14_day(parts=None, tasks=None, horizon=14, part=None):
    """Summarize evidence-backed shortage risk over the next 14 days."""
    parts = copy.deepcopy(parts or PARTS)
    tasks = list(tasks or TASKS)
    if part is not None and part not in parts:
        raise KeyError(f"unknown part {part}")
    selected_tasks = [task for task in tasks if (part is None or task.get("part") == part)]
    risks = []
    for task in selected_tasks:
        part_number = task.get("part")
        if part_number not in parts:
            continue
        shortcut = _task_shortage(task, parts, horizon=horizon)
        if shortcut is None:
            continue
        risks.append({
            "task": task["id"],
            "aircraft": task["aircraft"],
            "part": part_number,
            "required_qty": int(shortcut["required_qty"]),
            "available_qty_on_day": int(shortcut["available_qty"]),
            "required_qty_on_day": int(shortcut["required_qty"]),
            "first_shortage_day": int(shortcut["shortage_day"]),
            "evidence": shortcut["evidence"],
            "eta_assumed": bool(shortcut["eta_assumed"]),
        })
    return {"synthetic_data": True, "horizon_days": max(0, int(horizon)), "risks": risks}


def assess(parts=None):
    parts, base, out = parts or PARTS, None, []
    base = baseline(parts)
    for task in TASKS:
        result, part = base[task["id"]], parts[task["part"]]
        pipeline = part["repair"] + part["procurement"]
        slip = sum(item["realistic"] - item["promised"] for item in pipeline) / len(pipeline) if pipeline else 0
        shortage = _task_shortage(task, parts, horizon=14)
        at_risk = result["late"] > 0 or shortage is not None
        score = round(min(0.99, 0.5 + 0.05 * result["late"] + 0.05 * slip), 2) if at_risk else 0.1
        out.append({"task": task["id"], "aircraft": task["aircraft"], "kind": task["kind"], "due": task["due"], "part": task["part"],
                    "window": ("0-7 d" if task["due"] <= 7 else "8-14 d" if task["due"] <= 14 else "15-30 d"),
                    "baseline_ready": result["ready"], "baseline_finish": result["end"], "source": result["source"], "at_risk": at_risk,
                    "will_be_lost": result["grounded"] > 0, "cause": "Part shortage" if at_risk else "None", "risk_score": score,
                    "method": "rule-based heuristic (not a trained/validated model)",
                    "why": (f"Without new action {task['part']} arrives via {result['source']} on day {result['ready']}; finish day {result['end']} vs due day {task['due']} (+{task['tol']} approved)" if at_risk else "Supply covers due date"),
                    "first_shortage_day": shortage["shortage_day"] if shortage else None,
                    "available_qty_on_shortage_day": shortage["available_qty"] if shortage else None,
                    "required_qty_on_shortage_day": shortage["required_qty"] if shortage else None,
                    "evidence": shortage["evidence"] if shortage else [],
                    "eta_assumed": shortage["eta_assumed"] if shortage else False})
    return out


def make_action(task, option, start, choices=()):
    """Build a plan action from a task, supply option, and start day."""
    finish = start + task["dur"]
    return {"task": task["id"], "aircraft": task["aircraft"], "part": task["part"], "action": option["kind"],
            "group": option["group"], "donor": option.get("donor"), "donor_down_days": option.get("donor_down_days", 0),
            "part_ready_day": option["ready"], "start": start, "finish": finish,
            "deferral_days": max(0, finish - task["due"]), "slack_to_limit": task["due"] + task["tol"] - finish,
            "due": task["due"], "limit": task["due"] + task["tol"], "cost": option["cost"],
            "choices": [{"action": choice["kind"], "group": choice["group"], "donor": choice.get("donor"), "ready": choice["ready"]} for choice in choices]}


def solve(parts=None, disabled=()):
    parts, disabled = parts or PARTS, set(disabled)
    model, options_by_task, groups, variables, intervals, objective = cp_model.CpModel(), {}, {}, {}, [], []
    for task in TASKS:
        options, task_groups = build_options(task, parts, disabled)
        options_by_task[task["id"]] = options
        groups.update(task_groups)
        upper_bound = task["due"] + task["tol"] - task["dur"]
        if not options or upper_bound < 0:
            return {"status": "INFEASIBLE", "actions": [], "reason": f"No feasible action or window for {task['id']} ({task['aircraft']}).", "diagnosis": diagnose(parts, disabled)}
        start = model.NewIntVar(0, upper_bound, f"s{task['id']}")
        late = model.NewIntVar(0, task["tol"], f"l{task['id']}")
        model.Add(late >= start + task["dur"] - task["due"])
        choices = [model.NewBoolVar(f"x{task['id']}_{index}") for index in range(len(options))]
        model.AddExactlyOne(choices)
        for selected, option in zip(choices, options):
            model.Add(start >= option["ready"]).OnlyEnforceIf(selected)
        variables[task["id"]] = (start, late, choices)
        intervals.append(model.NewFixedSizeIntervalVar(start, task["dur"], f"i{task['id']}"))
        objective.append(W["deferral_day"] * task["prio"] * late)
        objective += [option["cost"] * selected for selected, option in zip(choices, options)]
    model.AddCumulative(intervals, [1] * len(intervals), RES["bays"])
    for group, quantity in groups.items():
        model.Add(sum(selected * task["qty"] for task in TASKS
                      for selected, option in zip(variables[task["id"]][2], options_by_task[task["id"]])
                      if option["group"] == group) <= quantity)
    model.Minimize(sum(objective))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "INFEASIBLE", "actions": [],
                "reason": "No plan satisfies completion limits, finite supply, bay capacity and authorizations. Escalate to commander.",
                "diagnosis": diagnose(parts, disabled)}
    chosen = {}
    for task in TASKS:
        choices = variables[task["id"]][2]
        selected_index = next(index for index, choice in enumerate(choices) if solver.Value(choice))
        chosen[task["id"]] = options_by_task[task["id"]][selected_index]
    actions = []
    for task in TASKS:
        start = solver.Value(variables[task["id"]][0])
        option = chosen[task["id"]]
        rejected = []
        for alternative in options_by_task[task["id"]]:
            if alternative is option:
                continue
            if alternative["ready"] + task["dur"] > task["due"] + task["tol"]:
                reason = f"part ready day {alternative['ready']} -> finish day {alternative['ready']+task['dur']} beyond limit day {task['due']+task['tol']}"
            else:
                other_tasks = [item["id"] for item in TASKS if item["id"] != task["id"] and chosen[item["id"]]["group"] == alternative["group"]]
                reason = (f"supply lot already allocated to {','.join(other_tasks)}"
                          if other_tasks and groups[alternative["group"]] <= len(other_tasks)
                          else "feasible but higher total cost/impact")
            rejected.append({"kind": alternative["kind"], "why": reason})
        actions.append({**make_action(task, option, start, options_by_task[task["id"]]), "rejected": rejected})
    plan = {"status": "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
            "objective": solver.ObjectiveValue(), "actions": actions, "disabled": sorted(disabled)}
    return finalize(plan, parts)


def find_option(task, parts, disabled, action):
    """Find the scenario option matching an action, or None if it is unavailable."""
    return next((option for option in build_options(task, parts, disabled)[0]
                 if option["kind"] == action.get("action") and option["group"] == action.get("group")
                 and option.get("donor") == action.get("donor")), None)


NEEDED = ("aircraft", "part", "action", "group", "part_ready_day", "start", "finish", "cost", "donor_down_days", "deferral_days")
NUM = ("part_ready_day", "start", "finish", "cost", "donor_down_days", "deferral_days")


def verify(plan, parts):
    """Independently verify options, deadlines, lot capacities, and bay capacity."""
    bad, use, days, disabled = [], {}, {}, set(plan.get("disabled", []))
    tasks = {task["id"]: task for task in TASKS}
    actions = plan.get("actions") or []
    ids = [action.get("task") if isinstance(action, dict) else None for action in actions]
    for task_id in tasks:
        if ids.count(task_id) != 1:
            bad.append(f"task {task_id} appears {ids.count(task_id)} times (must be exactly once)")
    for task_id in set(ids) - set(tasks):
        bad.append(f"unknown task {task_id}")
    capacity = {}
    for task in TASKS:
        capacity.update(build_options(task, parts, disabled)[1])
    for action in actions:
        task = tasks.get(action.get("task")) if isinstance(action, dict) else None
        if not task:
            continue
        missing = [key for key in NEEDED if key not in action]
        if missing or not all(isinstance(action[key], (int, float)) and not isinstance(action[key], bool) for key in NUM):
            bad.append(f"{task['id']}: missing or non-numeric field(s) {missing or 'type'}")
            continue
        option = find_option(task, parts, disabled, action)
        if option is None:
            bad.append(f"{task['id']}: '{action.get('action')}' from lot {action.get('group')} is not a permitted option in this scenario")
            continue
        if action["aircraft"] != task["aircraft"] or action["part"] != task["part"]:
            bad.append(f"{task['id']}: aircraft/part do not match the task")
        if action["part_ready_day"] != option["ready"]:
            bad.append(f"{task['id']}: recorded part-ready day {action['part_ready_day']} != scenario day {option['ready']}")
        if action["cost"] != option["cost"] or action["donor_down_days"] != option.get("donor_down_days", 0):
            bad.append(f"{task['id']}: recorded cost/donor impact do not match scenario")
        if action["start"] < 0:
            bad.append(f"{task['id']} starts before day 0")
        if action["start"] < option["ready"]:
            bad.append(f"{task['id']} starts day {action['start']} before part is ready (day {option['ready']})")
        if action["finish"] - action["start"] != task["dur"]:
            bad.append(f"{task['id']} wrong duration")
        finish = action["start"] + task["dur"]
        if finish > task["due"] + task["tol"]:
            bad.append(f"{task['id']} finishes day {finish} after limit {task['due']+task['tol']}")
        if action["deferral_days"] != max(0, finish - task["due"]):
            bad.append(f"{task['id']}: recorded deferral does not match schedule")
        use[action["group"]] = use.get(action["group"], 0) + task["qty"]
        for day in range(action["start"], finish):
            days[day] = days.get(day, 0) + 1
    for group, quantity in use.items():
        if quantity > capacity.get(group, 0):
            bad.append(f"supply lot {group} over-allocated ({quantity}>{capacity.get(group, 0)})")
    for day, quantity in days.items():
        if quantity > RES["bays"]:
            bad.append(f"day {day}: {quantity} aircraft in {RES['bays']} bays")
    return not bad, bad


def metrics(plan, base):
    """Compute schedule impact from the plan rather than hardcoded totals."""
    tasks = {task["id"]: task for task in TASKS}
    finishes = [(tasks[action["task"]], action["start"] + tasks[action["task"]]["dur"]) for action in plan["actions"]]
    on_time = sum(1 for task, finish in finishes if finish <= task["due"])
    return {"baseline": {"on_time": sum(1 for item in base.values() if item["late"] == 0),
                         "grounded_days": sum(item["grounded"] for item in base.values()), "total": len(TASKS)},
            "plan": {"on_time": on_time, "deferred": len(TASKS) - on_time,
                     "grounded_days": sum(max(0, finish - task["due"] - task["tol"]) for task, finish in finishes),
                     "donor_down_days": sum(action["donor_down_days"] for action in plan["actions"]),
                     "extra_cost": sum(action["cost"] for action in plan["actions"]), "total": len(TASKS)}}


def diagnose(parts, disabled):
    """Explain task-level infeasibility or shared-resource contention."""
    out = []
    for task in TASKS:
        options, _ = build_options(task, parts, disabled)
        limit = task["due"] + task["tol"]
        if not options:
            out.append(f"{task['id']} ({task['aircraft']}): no supply option available for {task['part']}")
            continue
        best = min(options, key=lambda option: option["ready"])
        finish = best["ready"] + task["dur"]
        if finish > limit:
            out.append(f"{task['id']} ({task['aircraft']}): earliest possible finish is day {finish} ({best['kind'].replace('_', ' ')}) but the limit is day {limit}")
    return out or ["Each task is feasible on its own; the conflict comes from shared supply lots or hangar-bay capacity."]


SLIP = {"await_repair": 4, "expedite_repair": 3, "await_delivery": 4, "urgent_indent": 3}


def robustness(plan, parts, n=1000, seed=7):
    """Estimate deadline sensitivity under assumed supply delays, not a forecast."""
    rng, tasks = random.Random(seed), {task["id"]: task for task in TASKS}
    actions = plan.get("actions") or []
    if not actions:
        return None
    all_ok, per_task = 0, {action["task"]: 0 for action in actions}
    for _ in range(n):
        good = True
        for action in actions:
            task = tasks[action["task"]]
            start = max(action["start"], action["part_ready_day"] + rng.randint(0, SLIP.get(action["action"], 0)))
            if start + task["dur"] <= task["due"] + task["tol"]:
                per_task[action["task"]] += 1
            else:
                good = False
        all_ok += good
    return {"trials": n, "p_all_within_limits": round(all_ok / n, 3),
            "per_task": {key: round(value / n, 3) for key, value in per_task.items()},
            "assumed_max_slip_days": SLIP,
            "note": "sensitivity check under assumed supply slip; not a validated forecast"}


def finalize(plan, parts):
    plan["verified"], plan["violations"] = verify(plan, parts)
    plan["metrics"] = metrics(plan, baseline(parts))
    plan["robustness"] = robustness(plan, parts)
    plan["bays"] = RES["bays"]
    return plan


def diff_plans(a, b):
    """Compare two plans row by row."""
    if not a.get("actions") or not b.get("actions"):
        return {"comparable": False, "base_status": a["status"], "alt_status": b["status"]}
    format_action = lambda action: f"{action['action'].replace('_', ' ')}{' from ' + action['donor'] if action['donor'] else ''} D{action['start']}→D{action['finish']}"
    base_tasks, alt_tasks = ({action["task"]: action for action in plan["actions"]} for plan in (a, b))
    rows = [{"task": task_id, "aircraft": base_tasks[task_id]["aircraft"],
             "base": format_action(base_tasks[task_id]), "alt": format_action(alt_tasks[task_id]),
             "changed": (base_tasks[task_id]["action"], base_tasks[task_id]["donor"], base_tasks[task_id]["start"])
             != (alt_tasks[task_id]["action"], alt_tasks[task_id]["donor"], alt_tasks[task_id]["start"])}
            for task_id in base_tasks]
    base_metrics, alt_metrics = a["metrics"]["plan"], b["metrics"]["plan"]
    delta = {key: alt_metrics[key] - base_metrics[key] for key in ("on_time", "grounded_days", "donor_down_days", "extra_cost")}
    delta["robustness_pct"] = round((b["robustness"]["p_all_within_limits"] - a["robustness"]["p_all_within_limits"]) * 100)
    return {"comparable": True, "rows": rows, "delta": delta, "objective_delta": b["objective"] - a["objective"]}


def modify_plan(base_plan, edits, parts):
    """Rebuild reviewer edits from allowed scenario options, then verify the new plan."""
    disabled, actions = set(base_plan.get("disabled", [])), []
    tasks = {task["id"]: task for task in TASKS}
    for task_id in edits:
        if task_id not in tasks:
            raise ValueError(f"unknown task {task_id}")
    for task in TASKS:
        current = next(action for action in base_plan["actions"] if action["task"] == task["id"])
        edit = edits.get(task["id"], {})
        kind = edit.get("action", current["action"])
        group = edit.get("group", current["group"] if kind == current["action"] else None)
        options = build_options(task, parts, disabled)[0]
        option = next((item for item in options if item["kind"] == kind and (group is None or item["group"] == group)), None)
        if option is None:
            raise ValueError(f"{task['id']}: '{kind}' is not available in this scenario")
        actions.append({**make_action(task, option, int(edit.get("start", current["start"])), options),
                        "rejected": [{"kind": item["kind"], "why": "not selected by reviewer"} for item in options if item is not option]})
    plan = {"status": "MODIFIED", "actions": actions, "disabled": sorted(disabled)}
    plan["objective"] = sum(W["deferral_day"] * tasks[action["task"]]["prio"] * action["deferral_days"] + action["cost"] for action in actions)
    return finalize(plan, parts)