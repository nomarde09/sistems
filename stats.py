"""Сквозная статистика по заявкам. Одна и та же для руководителя и технического раздела.

Считается за выбранный период (сегодня, вчера, позавчера, 7 дней, месяц, конкретный день
или всё время). «Сейчас в работе» и «сейчас новых» — всегда текущее положение.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

PERIODS = {
    "today": "Сегодня",
    "yesterday": "Вчера",
    "day_before": "Позавчера",
    "week": "7 дней",
    "month": "Месяц",
    "all": "Всё время",
}
DEFAULT_PERIOD = "today"

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def resolve_period(key, day_text, today):
    """Период из адреса страницы → (ключ, подпись, первый день, последний день).

    Для «всё время» первый и последний день — None.
    """
    if key == "day":
        try:
            day = date.fromisoformat(day_text or "")
            return "day", day.strftime("%d.%m.%Y"), day, day
        except ValueError:
            key = DEFAULT_PERIOD
    if key not in PERIODS:
        key = DEFAULT_PERIOD
    if key == "today":
        return key, "сегодня", today, today
    if key == "yesterday":
        day = today - timedelta(days=1)
        return key, "вчера", day, day
    if key == "day_before":
        day = today - timedelta(days=2)
        return key, "позавчера", day, day
    if key == "week":
        return key, "за 7 дней", today - timedelta(days=6), today
    if key == "month":
        return key, "в этом месяце", today.replace(day=1), today
    return "all", "за всё время", None, None


def page_context(db, USERS, PAYMENTS, name_of, today, args, endpoint,
                 REQUEST_TYPES=None, EQUIPMENT=None):
    """Всё, что нужно шаблону _stats_body.html. args — параметры адреса (p — период, d — день)."""
    key, label, start, end = resolve_period(args.get("p", ""), args.get("d", ""), today)
    return {
        "s": collect(db, USERS, PAYMENTS, name_of, start, end, REQUEST_TYPES, EQUIPMENT),
        "period": {"key": key, "label": label, "start": start, "end": end},
        "periods": PERIODS,
        "stats_endpoint": endpoint,
        "today": today,
    }


def _day(iso):
    return datetime.fromisoformat(iso).date() if iso else None


def _hours_between(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600


def _average(values):
    return sum(values) / len(values) if values else None


def _breakdown(created, done, key_of, labels, unknown="не указано"):
    """Разбивка по полю: [(подпись, создано, выполнено, сумма)] в порядке labels."""
    table = defaultdict(lambda: {"created": 0, "done": 0, "sum": 0})
    for r in created:
        table[key_of(r)]["created"] += 1
    for r in done:
        table[key_of(r)]["done"] += 1
        table[key_of(r)]["sum"] += r["report_price"] or 0
    order = list(labels) + [k for k in table if k not in labels]
    return [(labels.get(k, unknown), table[k]) for k in order if k in table]


def collect(db, USERS, PAYMENTS, name_of, start=None, end=None, REQUEST_TYPES=None, EQUIPMENT=None):
    """Считает статистику. start/end — первый и последний день периода (или None = всё время).

    name_of(user_id) — как называть человека (имя из .env или должность).
    """
    def in_period(iso):
        if start is None:
            return True
        d = _day(iso)
        return d is not None and start <= d <= end

    rows = db.execute("SELECT * FROM requests").fetchall()
    events = db.execute("SELECT * FROM events ORDER BY at, id").fetchall()
    period_events = [e for e in events if in_period(e["at"])]

    created = [r for r in rows if in_period(r["created_at"])]
    done = [r for r in rows if r["status"] == "done" and in_period(r["done_at"])]
    cancelled = [r for r in rows if r["status"] == "cancelled" and in_period(r["cancelled_at"])]
    taken_events = [e for e in period_events if e["action"] == "taken"]

    # Сколько ждали до первого взятия — для заявок, впервые взятых в этот период.
    created_at = {r["id"]: r["created_at"] for r in rows}
    first_taken = {}
    for e in events:
        if e["action"] == "taken" and e["request_id"] not in first_taken:
            first_taken[e["request_id"]] = e["at"]
    waits = [_hours_between(created_at[rid], at) for rid, at in first_taken.items()
             if rid in created_at and in_period(at)]

    action_count = Counter((e["action"], e["user_id"]) for e in period_events)
    brigades = []
    for uid, u in USERS.items():
        if u["role"] != "brigade":
            continue
        mine_done = [r for r in done if r["taken_by"] == uid]
        brigades.append({
            "name": name_of(uid),
            "taken": action_count[("taken", uid)],
            "done": len(mine_done),
            "sum": sum(r["report_price"] or 0 for r in mine_done),
            "avg_hours": _average([_hours_between(r["taken_at"], r["done_at"])
                                   for r in mine_done if r["taken_at"]]),
            "returned": action_count[("returned", uid)],
            "in_work_now": sum(r["status"] == "in_work" and r["taken_by"] == uid for r in rows),
        })

    authors = []
    for uid, u in USERS.items():
        if u["role"] == "brigade":
            continue
        authors.append({
            "name": name_of(uid),
            "created": action_count[("created", uid)],
            "edited": action_count[("edited", uid)],
            "cancelled": action_count[("cancelled", uid)],
        })

    payments = Counter()
    for r in done:
        payments[PAYMENTS.get(r["report_payment"], "не указана")] += r["report_price"] or 0

    # По дням — для периодов длиннее одного дня (кроме «всё время»: там по месяцам).
    days = []
    if start is not None and end > start:
        per_day = {start + timedelta(days=i): {"created": 0, "done": 0, "sum": 0}
                   for i in range((end - start).days + 1)}
        for r in created:
            per_day[_day(r["created_at"])]["created"] += 1
        for r in done:
            per_day[_day(r["done_at"])]["done"] += 1
            per_day[_day(r["done_at"])]["sum"] += r["report_price"] or 0
        days = [(d, WEEKDAYS[d.weekday()], v) for d, v in sorted(per_day.items(), reverse=True)]

    months = []
    if start is None:
        per_month = defaultdict(lambda: {"created": 0, "done": 0, "sum": 0})
        for r in created:
            per_month[r["created_at"][:7]]["created"] += 1
        for r in done:
            per_month[r["done_at"][:7]]["done"] += 1
            per_month[r["done_at"][:7]]["sum"] += r["report_price"] or 0
        months = sorted(per_month.items(), reverse=True)[:12]

    by_type = _breakdown(created, done, lambda r: r["request_type"], REQUEST_TYPES or {})
    by_equipment = _breakdown(created, done, lambda r: r["equipment"], EQUIPMENT or {})
    by_warranty = _breakdown(created, done, lambda r: "da" if r["warranty"] else "net",
                             {"net": "Платные", "da": "Гарантийные"})

    return {
        "by_type": by_type,
        "by_equipment": by_equipment,
        "by_warranty": by_warranty,
        "created": len(created),
        "taken": len(taken_events),
        "done": len(done),
        "cancelled": len(cancelled),
        "done_sum": sum(r["report_price"] or 0 for r in done),
        "avg_wait": _average(waits),
        "new_now": sum(r["status"] == "new" for r in rows),
        "in_work_now": sum(r["status"] == "in_work" for r in rows),
        "brigades": brigades,
        "authors": authors,
        "payments": payments.most_common(),
        "days": days,
        "months": months,
        "total_requests": len(rows),
        "events_count": len(events),
    }
