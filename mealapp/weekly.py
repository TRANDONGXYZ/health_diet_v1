"""Current-week generation and saved weekly plans with explicit meal replacements."""
import copy
import json
import random
from datetime import date, datetime, timedelta, timezone
from . import ai, store


def today(offset=None):
    if offset is None:
        return date.today()
    try:
        minutes = int(offset)
    except (TypeError, ValueError):
        raise ValueError("Invalid local timezone offset.") from None
    if not -840 <= minutes <= 720:
        raise ValueError("Invalid local timezone offset.")
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).date()


def calendar(offset=None):
    current = today(offset)
    monday = current - timedelta(days=current.weekday())
    sunday = monday + timedelta(days=6)
    return {"today":current.isoformat(), "week_start":monday.isoformat(), "week_end":sunday.isoformat(), "advance_limit":(monday+timedelta(weeks=8)).isoformat(),
        "dates":[(current+timedelta(days=i)).isoformat() for i in range((sunday-current).days+1)]}


def week_date(week):
    try:
        parsed=date.fromisoformat(week)
        if parsed.weekday()!=0 or parsed.isoformat()!=week:
            raise ValueError()
        return parsed
    except (ValueError,TypeError):
        raise ValueError('Choose a week starting on Monday.') from None


def generation_context(data):
    context=calendar(data.get('timezone_offset'))
    target=data.get('week_start') or context['week_start']
    monday=week_date(target)
    if target<context['week_start']:
        raise ValueError('Generate only for the current week or a future week. Past weeks allow manual editing.')
    if target>context['advance_limit']:
        raise ValueError('Advance planning is limited to eight weeks ahead of the current week.')
    if target!=context['week_start']:
        if not data.get('client_only') and not store.get('plans',target):
            raise ValueError('Add this future week before generating its meals.')
        context=dict(context,week_start=target,week_end=(monday+timedelta(days=6)).isoformat(),
                     dates=[(monday+timedelta(days=i)).isoformat() for i in range(7)])
    return context


def add_week(data):
    context=calendar(data.get('timezone_offset'))
    week=data.get('week_start');monday=week_date(week)
    if not context['week_start']<week<=context['advance_limit']:
        raise ValueError('Choose one of the next eight weeks; the current week does not count.')
    if store.get('plans',week):
        raise ValueError('This week already exists.')
    plan={'week_start':week,'generated_on':context['today'],'method':'manual','vegan_only':False,'prioritize_running':False,'warnings':[],
          'days':[{'date':(monday+timedelta(days=i)).isoformat(),'recipe_id':None} for i in range(7)]}
    store.put('plans',week,plan)
    return {'plan':plan}


def snapshot(recipe):
    result = copy.deepcopy(recipe)
    result.pop("source_text", None)
    result.pop("legacy_groups", None)
    result["calories"] = store.calories(recipe)
    return result


def history():
    # Migrate the old singleton exactly once, without discarding its dates or recipes.
    if not store.get("state", "weekly_history_migrated"):
        legacy = store.get("state", "plan")
        if legacy and legacy.get("week_start"):
            start = date.fromisoformat(legacy["week_start"])
            grouped = {}
            for i,day in enumerate(legacy.get("days", [])):
                scheduled = date.fromisoformat(day["date"]) if day.get("date") else start+timedelta(days=i)
                week = (scheduled-timedelta(days=scheduled.weekday())).isoformat()
                item = copy.deepcopy(day); item["date"] = scheduled.isoformat()
                recipe = store.get("recipes", item["recipe_id"])
                if recipe and not item.get("deleted"):
                    item["recipe"] = snapshot(recipe)
                grouped.setdefault(week, []).append(item)
            with store.db() as connection:
                for week, days in grouped.items():
                    plan = dict(legacy, week_start=week, days=days, generated_on=start.isoformat())
                    connection.execute("INSERT OR IGNORE INTO plans VALUES (?, ?)", (week,json.dumps(plan,ensure_ascii=False)))
        store.put("state","weekly_history_migrated",True)
    return sorted(store.all_items("plans"),key=lambda p:p["week_start"],reverse=True)


def build(data):
    context = generation_context(data)
    count = len(context["dates"])
    data = dict(data, week_start=context["week_start"])
    recipes = data['client_recipes'] if data.get('client_only') else store.all_items("recipes")
    if data.get("vegan_only"):
        recipes = [r for r in recipes if r.get("vegan") is True]
    if not recipes:
        raise ValueError("Save at least one matching dish before generating a plan.")
    prioritize = data.get("prioritize_running", False)
    if not isinstance(prioritize, bool):
        raise ValueError("Running preference must be true or false.")
    running = [r for r in recipes if r.get("good_for_running")]
    other = [r for r in recipes if not r.get("good_for_running")]
    warnings = ["No matching saved dishes are marked Good for Running. Other saved dishes will be used."] if prioritize and not running else []
    def practical_day(recipe, preferred=False):
        minutes = recipe.get("measured_minutes") or recipe.get("estimated_minutes")
        timing = "night before" if minutes and minutes > 25 else "morning"
        return {"recipe_id":recipe["id"], "prep":"Review the original source and weigh the listed ingredients ahead of time.",
                "timing":timing, "reason":"Prioritized because you marked this dish Good for Running." if preferred else "Simple rotation. Choose timing to suit the dish and your schedule."}
    method = data.get("method") or ("rotation" if data.get("simple") else "AI")
    if method not in ("AI", "rotation", "random"):
        raise ValueError("Choose AI, rotation, or random generation.")
    if method in ("rotation", "random"):
        ordered = running + other if prioritize else recipes
        if method == "random":
            ordered = random.sample(running, len(running)) + random.sample(other, len(other)) if prioritize else random.sample(recipes, len(recipes))
        days = [practical_day(ordered[i % len(ordered)], prioritize and bool(ordered[i % len(ordered)].get("good_for_running"))) for i in range(count)]
    else:
        if prioritize and running:
            candidates = random.sample(running, min(12, len(running)))
            candidates += random.sample(other, min(12-len(candidates), len(other)))
        else:
            candidates = random.sample(recipes, min(12,len(recipes)))
        ids = {f"r{i+1}":r["id"] for i,r in enumerate(candidates)}
        compact = [{"id":f"r{i+1}", "name":r["name"][:100], "good_for_running":bool(r.get("good_for_running")),
            "minutes":r.get("measured_minutes") or r.get("estimated_minutes"),
            "ingredients":[item["name"][:45] for item in store.recipe_ingredients(r)[:12]]} for i,r in enumerate(candidates)]
        day_schema = ai.obj({"recipe_id":{"type":"string", "enum":list(ids)}, "prep":{"type":"string", "maxLength":200},
            "timing":{"type":"string", "enum":["night before", "morning"]}, "reason":{"type":"string", "maxLength":160}})
        preference = "Prioritize dishes marked good_for_running=true; use all available marked dishes before unmarked ones, and prefer variety. " if prioritize else "Prefer variety among all saved dishes. "
        result = ai.ask(f"Suggest exactly {count} days with ONE saved dish per day, for dates {context['dates']}. Choose only the given IDs; repeat dishes if necessary. "
            + preference + "Give concise advance preparation and choose cooking the night before or fresh in the morning. "
            "Use only the listed ingredients; do not invent ingredients or cooking instructions. Refer to the original source when details are missing. "
            "Do not assume room-temperature storage. Explain your suggestions in English, retaining each original dish name. LIBRARY:\n"
            + json.dumps(compact,ensure_ascii=False), ai.obj({"days":{"type":"array", "minItems":count, "maxItems":count, "items":day_schema}}), max_tokens=250*count+100, language="English")
        days = [dict(d,recipe_id=ids.get(d.get("recipe_id"),d.get("recipe_id"))) for d in result.get("days",[])]
        valid = {r["id"] for r in recipes}
        if len(days) != count or any(d.get("recipe_id") not in valid or d.get("timing") not in ("morning","night before") for d in days):
            raise ValueError("The model returned an invalid plan. Nothing was saved. Try Generate again or use Simple rotation.")
        if prioritize and running:
            preferred_ids = {r["id"] for r in running}
            present = {d["recipe_id"] for d in days if d["recipe_id"] in preferred_ids}
            required = min(count,len(running))
            replacements = [r for r in candidates if r["id"] in preferred_ids and r["id"] not in present][:max(0,required-len(present))]
            seen, replaceable = set(), []
            for i,day in enumerate(days):
                if day["recipe_id"] not in preferred_ids or day["recipe_id"] in seen:
                    replaceable.append(i)
                seen.add(day["recipe_id"])
            for index,recipe in zip(replaceable,replacements):
                days[index] = practical_day(recipe,True)
    for day, scheduled in zip(days, context["dates"]):
        day["date"] = scheduled
    return {"week_start":context["week_start"], "generated_on":context["today"], "days":days,
        "vegan_only":bool(data.get("vegan_only")), "prioritize_running":prioritize,
        "method":method, "warnings":warnings}


def commit(plan, offset=None):
    context = calendar(offset)
    if plan['week_start']<context['week_start']:
        raise ValueError('The week changed while generating; this week now allows only manual editing.')
    if plan['week_start']>context['advance_limit']:
        raise ValueError('Advance planning is limited to eight weeks.')
    if plan['week_start']>context['week_start'] and not store.get('plans',plan['week_start']):
        raise ValueError('This future week was deleted. Add it again before generating.')
    saved = copy.deepcopy(plan)
    # A new current-week result replaces only dates that are still today/future.
    # Earlier entries (including null slots and old snapshots) are copied verbatim.
    existing = store.get('plans',plan['week_start'])
    earlier = []
    if plan['week_start']==context['week_start']:
        earlier = copy.deepcopy([day for day in (existing or {}).get('days',[]) if day['date']<context['today']])
        saved['days']=[day for day in saved['days'] if day['date']>=context['today']]
    for day in saved["days"]:
        recipe = store.get("recipes",day["recipe_id"])
        if not recipe or saved.get("vegan_only") and recipe.get("vegan") is not True:
            raise ValueError("A selected dish changed or was deleted. Generate again; your saved plan was kept.")
        day["recipe"] = snapshot(recipe)
    saved['days']=earlier+saved['days']
    store.put("plans",saved["week_start"],saved)
    return saved


def save_expansion(data):
    week=data.get('week_start')
    week_date(week)
    if not isinstance(data.get('expanded'),bool):
        raise ValueError('Expanded must be true or false.')
    with store.LOCK:
        choices=store.get('state','week_expansion',{})
        choices[week]=data['expanded']
        store.put('state','week_expansion',choices)
    return {'ok':True}


def saved_week(week):
    week_date(week)
    plan = store.get('plans',week)
    if not plan:
        raise ValueError('This weekly plan no longer exists. Reload the page.')
    return plan


def delete(week):
    saved_week(week)
    with store.db() as connection:
        connection.execute('DELETE FROM plans WHERE id=?',(week,))
    return {'ok':True}


def replace(week,scheduled,recipe_id):
    plan = saved_week(week)
    day = next((d for d in plan['days'] if d.get('date')==scheduled),None)
    if not day:
        raise ValueError('This date is not in the saved plan. Reload the page.')
    if not isinstance(recipe_id,str) or not recipe_id:
        raise ValueError('Select a saved dish to replace this meal.')
    recipe = store.get('recipes',recipe_id)
    if not recipe:
        raise ValueError('That dish was deleted. Select another saved dish.')
    minutes = recipe.get('measured_minutes') or recipe.get('estimated_minutes')
    # Replace only this date; never resnapshot other meals or reuse their advice.
    day.clear()
    day.update(date=scheduled,recipe_id=recipe_id,recipe=snapshot(recipe),
        prep='Review the original source and weigh the listed ingredients ahead of time.',
        timing='night before' if minutes and minutes>25 else 'morning',
        reason='Manually selected from your saved dishes.')
    if plan.get('vegan_only') and recipe.get('vegan') is not True:
        plan['vegan_only'] = False
    store.put('plans',week,plan)
    return {'plan':plan}
