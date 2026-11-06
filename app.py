"""Система заявок: монтаж камер и автоматических ворот."""

import hmac
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# Кто может войти. Ключ — внутреннее имя, дальше: как показывать, роль.
# Роли: boss — руководитель, office — менеджер, brigade — бригада.
USERS = {
    "rukovoditel": {"name": "Руководитель", "role": "boss"},
    "manager1": {"name": "Менеджер 1", "role": "office"},
    "manager2": {"name": "Менеджер 2", "role": "office"},
    "brigada1": {"name": "Бригада 1", "role": "brigade"},
    "brigada2": {"name": "Бригада 2", "role": "brigade"},
}

STATUSES = {
    "new": "Новая",
    "in_work": "В работе",
    "done": "Выполнена",
    "cancelled": "Отменена",
}

# Раньше «что монтируем» выбиралось из трёх пунктов; старые записи переводим в текст.
OLD_WORK_TYPES = {
    "cameras": "Камеры",
    "gates": "Ворота",
    "both": "Камеры и ворота",
}


# ---------------------------------------------------------------- настройки


def pin_env_name(user_id):
    return f"PIN_{user_id.upper()}"


def ensure_env_file():
    """При первом запуске создаёт .env с секретным ключом и случайными паролями."""
    if ENV_FILE.exists():
        return
    lines = [
        "# Настройки системы заявок. Этот файл НЕ попадает в Git.",
        "# Пароли входа (4 цифры). Можно поменять и перезапустить сайт.",
        f"SECRET_KEY={secrets.token_hex(32)}",
    ]
    for user_id in USERS:
        lines.append(f"{pin_env_name(user_id)}={secrets.randbelow(10000):04d}")
    offset = datetime.now().astimezone().utcoffset().total_seconds() / 3600
    lines.append("# Часовой пояс: сколько часов прибавить к UTC (Москва = 3, Астана = 5).")
    lines.append(f"TZ_OFFSET_HOURS={offset:g}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_env_file():
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


ensure_env_file()
load_env_file()

LOCAL_TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET_HOURS", "3"))))
DATABASE = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "data" / "zayavki.db"))

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ["SECRET_KEY"],
    PERMANENT_SESSION_LIFETIME=timedelta(days=180),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


# ---------------------------------------------------------------- база данных

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_name    TEXT NOT NULL,
    phone          TEXT NOT NULL,
    address        TEXT NOT NULL,
    work_type      TEXT NOT NULL,   -- что монтируем, своими словами
    agreed_date    TEXT,            -- ГГГГ-ММ-ДД, день, о котором договорились
    urgent         INTEGER NOT NULL DEFAULT 0,
    description    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'new',
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,   -- время в формате ISO, местное
    taken_by       TEXT,
    taken_at       TEXT,
    done_at        TEXT,
    report_work    TEXT,
    report_price   INTEGER,
    report_comment TEXT,
    cancelled_by   TEXT,
    cancelled_at   TEXT,
    updated_at     TEXT
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE)
    try:
        db.executescript(SCHEMA)
        for key, label in OLD_WORK_TYPES.items():
            db.execute("UPDATE requests SET work_type = ? WHERE work_type = ?", (label, key))
        db.commit()
    finally:
        db.close()


init_db()


def now_local():
    return datetime.now(LOCAL_TZ).replace(microsecond=0, tzinfo=None)


# ---------------------------------------------------------------- вход


def current_user():
    user_id = session.get("user_id")
    if user_id in USERS:
        return {"id": user_id, **USERS[user_id]}
    return None


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.before_request
def check_csrf():
    """Каждая отправка формы должна нести наш скрытый ключ — защита от чужих сайтов."""
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400)


@app.context_processor
def inject_globals():
    return {
        "user": current_user(),
        "csrf_token": csrf_token,
        "USERS": USERS,
        "STATUSES": STATUSES,
    }


def login_required(*roles):
    """Пускает только вошедших; если указаны роли — только с этими ролями."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login"))
            if roles and user["role"] not in roles:
                return redirect(url_for("index"))
            return view(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------- оформление


@app.template_filter("dt")
def format_datetime(value):
    if not value:
        return ""
    return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")


MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
          "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
MONTHS_OF = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]


@app.template_filter("d")
def format_date(value):
    """«12 ноября»; год пишем, только если он не текущий."""
    if not value:
        return ""
    d = date.fromisoformat(value)
    text = f"{d.day} {MONTHS_OF[d.month - 1]}"
    if d.year != now_local().year:
        text += f" {d.year}"
    return text


def month_options():
    """Ближайшие 12 месяцев, начиная с текущего: [("2026-11", "Ноябрь"), ...]."""
    today = now_local().date()
    options = []
    for i in range(12):
        year, month = divmod(today.month - 1 + i, 12)
        year += today.year
        label = MONTHS[month]
        if year != today.year:
            label += f" {year}"
        options.append((f"{year}-{month + 1:02d}", label))
    return options


@app.template_filter("who")
def format_user(user_id):
    return USERS.get(user_id, {}).get("name", user_id or "")


# ---------------------------------------------------------------- страницы


@app.route("/")
def index():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    if user["role"] == "brigade":
        return redirect(url_for("brigade_home"))
    return redirect(url_for("office_home"))


@app.route("/vhod", methods=["GET", "POST"])
def login():
    error = None
    selected = request.form.get("user_id", "")
    if request.method == "POST":
        pin = request.form.get("pin", "").strip()
        expected = os.environ.get(pin_env_name(selected), "") if selected in USERS else ""
        if not selected:
            error = "Выберите, кто вы."
        elif expected and hmac.compare_digest(pin, expected):
            session.clear()
            session.permanent = True
            session["user_id"] = selected
            return redirect(url_for("index"))
        else:
            error = "Неверный пароль. Попробуйте ещё раз."
    return render_template("login.html", selected=selected, error=error)


@app.route("/vyhod", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/ofis")
@login_required("boss", "office")
def office_home():
    rows = get_db().execute(
        """SELECT * FROM requests
           ORDER BY CASE status WHEN 'new' THEN 0 WHEN 'in_work' THEN 1 ELSE 2 END,
                    urgent DESC, id DESC"""
    ).fetchall()
    return render_template("office_home.html", requests=rows)


def build_agreed_date(day_text, month_text):
    """День («12») + месяц («2026-11») → («2026-11-12», None) или ("", текст ошибки)."""
    try:
        day = int(day_text)
        year, month = (int(x) for x in month_text.split("-"))
    except ValueError:
        return "", "Выберите день и месяц."
    if not 1 <= month <= 12:
        return "", "Выберите месяц."
    try:
        return date(year, month, day).isoformat(), None
    except ValueError:
        return "", f"В месяце «{MONTHS[month - 1].lower()}» нет {day}-го числа."


def read_request_form():
    """Достаёт поля заявки из формы и проверяет их. Возвращает (данные, ошибки)."""
    form = request.form
    data = {
        "client_name": form.get("client_name", "").strip(),
        "phone": form.get("phone", "").strip(),
        "address": form.get("address", "").strip(),
        "work_type": form.get("work_type", "").strip(),
        "agreed_day": form.get("agreed_day", "").strip(),
        "agreed_month": form.get("agreed_month", "").strip(),
        "agreed_date": "",
        "urgent": 1 if form.get("urgent") else 0,
        "description": form.get("description", "").strip(),
    }
    errors = {}
    if not data["client_name"]:
        errors["client_name"] = "Укажите имя клиента."
    if not data["phone"]:
        errors["phone"] = "Укажите телефон."
    elif sum(ch.isdigit() for ch in data["phone"]) < 5:
        errors["phone"] = "В телефоне слишком мало цифр."
    if not data["address"]:
        errors["address"] = "Укажите адрес."
    if not data["work_type"]:
        errors["work_type"] = "Напишите, что монтируем."
    if data["agreed_day"]:
        data["agreed_date"], date_error = build_agreed_date(data["agreed_day"], data["agreed_month"])
        if date_error:
            errors["agreed_date"] = date_error
    if not data["description"]:
        errors["description"] = "Опишите, что нужно сделать."
    return data, errors


@app.route("/ofis/novaya", methods=["GET", "POST"])
@login_required("boss", "office")
def request_new():
    data, errors = {}, {}
    if request.method == "POST":
        data, errors = read_request_form()
        if not errors:
            db = get_db()
            cur = db.execute(
                """INSERT INTO requests
                   (client_name, phone, address, work_type, agreed_date, urgent,
                    description, status, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
                (
                    data["client_name"],
                    data["phone"],
                    data["address"],
                    data["work_type"],
                    data["agreed_date"] or None,
                    data["urgent"],
                    data["description"],
                    current_user()["id"],
                    now_local().isoformat(),
                ),
            )
            db.commit()
            return redirect(url_for("request_view", request_id=cur.lastrowid, created=1))
    today = now_local().date()
    quick_dates = [
        (label, d.day, f"{d.year}-{d.month:02d}")
        for label, d in (("Сегодня", today), ("Завтра", today + timedelta(days=1)),
                         ("Послезавтра", today + timedelta(days=2)))
    ]
    return render_template(
        "request_form.html",
        data=data,
        errors=errors,
        months=month_options(),
        current_month=f"{today.year}-{today.month:02d}",
        quick_dates=quick_dates,
    )


@app.route("/zayavka/<int:request_id>")
@login_required("boss", "office")
def request_view(request_id):
    row = get_db().execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        abort(404)
    return render_template("request_view.html", r=row, created=request.args.get("created"))


@app.route("/brigada")
@login_required("brigade")
def brigade_home():
    return render_template("brigade_home.html")


@app.errorhandler(400)
def bad_request(_e):
    return render_template("error.html", message="Страница устарела. Вернитесь назад, обновите её и попробуйте ещё раз."), 400


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", message="Такой страницы или заявки нет."), 404


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
