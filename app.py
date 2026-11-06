"""Система заявок: монтаж камер и автоматических ворот."""

import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote

import stats

from flask import (
    Flask,
    abort,
    flash,
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

PAYMENTS = {
    "kaspi_transfer": "Перевод на Kaspi",
    "kaspi_pay": "Kaspi Pay",
    "cash": "Наличными",
}

# Тип заявки и оборудование. Ключи хранятся в базе (для отчётов), подписи — для людей.
# Ключи не меняйте: по ним считается статистика. Подписи и порядок менять можно.
REQUEST_TYPES = {
    "montazh": "Монтаж (новый объект)",
    "remont": "Ремонт / неисправность",
    "to": "Плановое ТО",
    "diagnostika": "Диагностика / выезд на осмотр",
    "doosnashenie": "Дооснащение / доработка",
    "demontazh": "Демонтаж",
    "drugoe": "Другое",
}
EQUIPMENT = {
    "vorota": "Автоматика ворот / шлагбаум",
    "domofon": "Домофон",
    "kamery": "Камеры / видеонаблюдение",
    "skud": "СКУД / замки",
    "drugoe": "Другое",
}
# Время, на которое договорились: каждые 30 минут с 07:00 до 22:00.
AGREED_TIMES = [f"{h:02d}:{m:02d}" for h in range(7, 23) for m in (0, 30) if (h, m) != (22, 30)]

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
    lines.append("# Технический вход суперадмина: адрес /tech/vhod. Пароль не короче 12 символов.")
    lines.append("# Уберите эти две строки, чтобы полностью выключить технический раздел.")
    lines.append("SUPERADMIN_LOGIN=admin")
    lines.append(f"SUPERADMIN_PASSWORD={secrets.token_urlsafe(15)}")
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
    # На хостинге с https включите COOKIE_SECURE=1 в .env: cookie входа не уйдёт по http.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "") == "1",
    # Формы у нас маленькие; огромные отправки отбрасываем, чтобы ими нельзя было забить сайт.
    MAX_CONTENT_LENGTH=256 * 1024,
)


# ---------------------------------------------------------------- лог приложения
# Пишем в data/logs/app.log: кто, когда, какой адрес открыл и чем кончилось.
# Пароли, тексты заявок и строки поиска в лог не попадают.

LOG_FILE = DATABASE.parent / "logs" / "app.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("zayavki")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    _handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    _formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    # Время в логе — в поясе из .env, как и на сайте (на хостинге часы сервера обычно в UTC).
    _formatter.converter = lambda secs: datetime.fromtimestamp(secs, LOCAL_TZ).timetuple()
    _handler.setFormatter(_formatter)
    log.addHandler(_handler)


def who_for_log():
    if session.get("tech_admin"):
        return "суперадмин"
    return session.get("user_id") or "-"


def safe_for_log(text, limit=300):
    """Убирает из текста переводы строк и прочие служебные символы, чтобы нельзя было
    подделать строку лога через адрес страницы; слишком длинное обрезает."""
    text = "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in str(text))
    return text[:limit] + ("…" if len(text) > limit else "")


@app.after_request
def log_request(response):
    if not request.path.startswith("/static/"):
        log.info("%s %s %s %s %s", request.remote_addr, who_for_log(), request.method,
                 safe_for_log(request.path), response.status_code)
    return response


# Защитные заголовки для браузера:
# - нельзя встроить сайт в чужую страницу (защита от подмены кликов);
# - браузер не угадывает тип файлов;
# - при переходе на карты и другие сайты адрес нашей страницы (с телефоном в поиске) не передаётся;
# - скрипты, стили и картинки — только наши; формы отправляются только к нам;
# - геолокация — только для нашего сайта, камера и микрофон не нужны.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
    ),
    "Permissions-Policy": "geolocation=(self), camera=(), microphone=(), payment=()",
}


@app.after_request
def security_headers(response):
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


# ---------------------------------------------------------------- база данных

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_name    TEXT NOT NULL,
    phone          TEXT NOT NULL,
    address        TEXT NOT NULL,
    work_type      TEXT NOT NULL,   -- УСТАРЕЛО: «что монтируем» у старых заявок; у новых пусто
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

-- Журнал: кто что делал с заявкой и когда.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  INTEGER NOT NULL REFERENCES requests(id),
    action      TEXT NOT NULL,      -- см. EVENT_LABELS
    user_id     TEXT NOT NULL,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_request ON events(request_id);
"""

EVENT_LABELS = {
    "created": "Создал(а) заявку",
    "taken": "Взял(а) в работу",
    "returned": "Вернул(а) в свободные",
    "done": "Выполнил(а)",
    "edited": "Исправил(а) заявку",
    "cancelled": "Отменил(а) заявку",
    "warranty": "Изменил(а) гарантию",
}


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


def ensure_column(db, table, column, declaration):
    """Добавляет колонку в уже существующую таблицу, если её там ещё нет."""
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init_db():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE)
    try:
        db.executescript(SCHEMA)
        ensure_column(db, "requests", "cancel_reason", "TEXT")
        ensure_column(db, "requests", "report_payment", "TEXT")  # ключ из PAYMENTS
        ensure_column(db, "requests", "location", "TEXT")  # ссылка на карту или «широта, долгота»
        ensure_column(db, "requests", "request_type", "TEXT")  # ключ из REQUEST_TYPES
        ensure_column(db, "requests", "equipment", "TEXT")  # ключ из EQUIPMENT
        ensure_column(db, "requests", "warranty", "INTEGER NOT NULL DEFAULT 0")  # 1 — гарантийная
        ensure_column(db, "requests", "agreed_time", "TEXT")  # «ЧЧ:ММ», на какое время договорились
        ensure_column(db, "events", "details", "TEXT")  # что исправили / причина отмены
        for key, label in OLD_WORK_TYPES.items():
            db.execute("UPDATE requests SET work_type = ? WHERE work_type = ?", (label, key))
        # Заявкам, созданным до появления журнала, добавляем запись «создал».
        db.execute(
            """INSERT INTO events (request_id, action, user_id, at)
               SELECT id, 'created', created_by, created_at FROM requests
               WHERE id NOT IN (SELECT request_id FROM events)"""
        )
        db.commit()
    finally:
        db.close()


init_db()


def now_local():
    return datetime.now(LOCAL_TZ).replace(microsecond=0, tzinfo=None)


def log_event(db, request_id, action, user_id, at, details=None):
    db.execute(
        "INSERT INTO events (request_id, action, user_id, at, details) VALUES (?, ?, ?, ?, ?)",
        (request_id, action, user_id, at, details),
    )


def get_request_or_404(request_id):
    row = get_db().execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def get_events(request_id):
    return get_db().execute(
        "SELECT * FROM events WHERE request_id = ? ORDER BY at, id", (request_id,)
    ).fetchall()


# ---------------------------------------------------------------- вход


def person_name(user_id):
    """Имя человека для шапки: NAME_<ID> из .env, а если не задано — должность."""
    return os.environ.get(f"NAME_{user_id.upper()}", "").strip() or USERS[user_id]["name"]


def current_user():
    user_id = session.get("user_id")
    if user_id in USERS:
        return {"id": user_id, **USERS[user_id], "person": person_name(user_id)}
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
        expected = session.get("csrf", "")
        # Сравниваем байты: строки с не-латинскими буквами compare_digest не принимает (падал с 500).
        if not sent or not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
            abort(400)


@app.context_processor
def inject_globals():
    user = current_user()
    counts = None
    if user and user["role"] == "brigade":
        db = get_db()
        counts = {
            "free": db.execute("SELECT COUNT(*) FROM requests WHERE status = 'new'").fetchone()[0],
            "mine": db.execute(
                "SELECT COUNT(*) FROM requests WHERE status = 'in_work' AND taken_by = ?",
                (user["id"],),
            ).fetchone()[0],
        }
    return {
        "user": user,
        "counts": counts,
        "csrf_token": csrf_token,
        "USERS": USERS,
        "STATUSES": STATUSES,
        "PAYMENTS": PAYMENTS,
        "REQUEST_TYPES": REQUEST_TYPES,
        "EQUIPMENT": EQUIPMENT,
        "AGREED_TIMES": AGREED_TIMES,
        "EVENT_LABELS": EVENT_LABELS,
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


@app.template_filter("money")
def format_money(value):
    """15000 → «15 000» (с неразрывным пробелом)."""
    if value is None:
        return ""
    return f"{value:,}".replace(",", " ")


@app.template_filter("month_name")
def format_month(value):
    """«2026-11» → «Ноябрь 2026»."""
    year, month = value.split("-")
    return f"{MONTHS[int(month) - 1]} {year}"


@app.template_global()
def map_links(location, address):
    """Кнопки карты для заявки: [(подпись, ссылка), ...]."""
    coords = parse_coords(location)
    if coords:
        lat, lon = coords
        return [
            ("Открыть в 2ГИС", f"https://2gis.kz/geo/{lon},{lat}"),
            ("Яндекс Карты", f"https://yandex.kz/maps/?pt={lon},{lat}&z=17&l=map"),
            ("Google Карты", f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"),
        ]
    if location and location.lower().startswith(("http://", "https://")):
        return [("Открыть на карте", location)]
    if address:
        return [("Найти адрес в 2ГИС", "https://2gis.kz/search/" + quote(address))]
    return []


@app.template_filter("tel")
def format_tel_link(phone):
    """«8 (701) 555-12-34» → «87015551234» для ссылки звонка; плюс в начале сохраняем."""
    digits = "".join(ch for ch in phone or "" if ch.isdigit())
    return ("+" if (phone or "").strip().startswith("+") else "") + digits


@app.template_filter("who")
def format_user(user_id):
    """Как подписывать человека везде на сайте: имя из .env (NAME_<ID>), без него — должность."""
    if user_id in USERS:
        return person_name(user_id)
    return user_id or ""


# ---------------------------------------------------------------- страницы


@app.route("/")
def index():
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    if user["role"] == "brigade":
        return redirect(url_for("brigade_home"))
    return redirect(url_for("office_home"))


def login_env_name(user_id):
    return f"LOGIN_{user_id.upper()}"


def login_of(user_id):
    """Логин для входа: из .env (LOGIN_<ID>), а если не задан — внутреннее имя."""
    return (os.environ.get(login_env_name(user_id), "").strip() or user_id).casefold()


def find_user_by_login(text):
    wanted = text.strip().casefold()
    return next((uid for uid in USERS if wanted and login_of(uid) == wanted), None)


# Защита от подбора: 5 неверных паролей к одному логину с одного адреса — пауза 15 минут.
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 15 * 60
_login_fails = {}


def _fails(key):
    now = time.monotonic()
    if len(_login_fails) > 2000:  # чистим старые записи, чтобы перебор логинов не съел память
        for old_key in [k for k, ts in _login_fails.items() if not ts or now - ts[-1] >= LOGIN_LOCK_SECONDS]:
            del _login_fails[old_key]
        if len(_login_fails) > 5000:  # всё ещё много — оставляем самые свежие
            keep = sorted(_login_fails.items(), key=lambda kv: kv[1][-1], reverse=True)[:2000]
            _login_fails.clear()
            _login_fails.update(keep)
    recent = [t for t in _login_fails.get(key, []) if now - t < LOGIN_LOCK_SECONDS]
    if recent:
        _login_fails[key] = recent
    else:
        _login_fails.pop(key, None)
    return recent


@app.route("/vhod", methods=["GET", "POST"])
def login():
    error = None
    typed_login = request.form.get("login", "").strip()
    if request.method == "POST":
        password = request.form.get("password", "")
        key = (request.remote_addr, typed_login.casefold())
        user_id = find_user_by_login(typed_login)
        expected = os.environ.get(pin_env_name(user_id), "") if user_id else ""
        if not typed_login or not password:
            error = "Введите логин и пароль."
        elif len(_fails(key)) >= LOGIN_MAX_FAILS:
            error = "Слишком много неверных попыток. Подождите 15 минут."
            log.warning("ВХОД заблокирован (много попыток): %s с %s", user_id or "?", request.remote_addr)
        elif expected and hmac.compare_digest(password.encode(), expected.encode()):
            _login_fails.pop(key, None)
            session.clear()
            session.permanent = True
            session["user_id"] = user_id
            log.info("ВХОД успешно: %s с %s", user_id, request.remote_addr)
            return redirect(url_for("index"))
        else:
            _login_fails.setdefault(key, []).append(time.monotonic())
            error = "Неверный логин или пароль."
            log.warning("ВХОД неудачно: %s с %s", user_id or "?", request.remote_addr)
    return render_template("login.html", typed_login=typed_login, error=error)


@app.route("/vyhod", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/ofis")
@login_required("boss", "office")
def office_home():
    rows = get_db().execute(
        # Срочные незакрытые — в самом верху; дальше новые, в работе, потом закрытые (свежие выше).
        """SELECT * FROM requests
           ORDER BY CASE WHEN urgent = 1 AND status IN ('new', 'in_work') THEN 0 ELSE 1 END,
                    CASE status WHEN 'new' THEN 0 WHEN 'in_work' THEN 1 ELSE 2 END,
                    COALESCE(done_at, cancelled_at, created_at) DESC, id DESC"""
    ).fetchall()
    filters = {
        "q": request.args.get("q", "").strip(),
        "status": "",
        "brigada": request.args.get("brigada", ""),
        "avtor": request.args.get("avtor", ""),
        "period": request.args.get("period", ""),
        "oplata": request.args.get("oplata", ""),
        "tip": request.args.get("tip", ""),
        "oborud": request.args.get("oborud", ""),
        "garantiya": request.args.get("garantiya", ""),
    }
    filtering = any(filters.values())

    # Вкладка по статусу. Если её не выбрали: при поиске — «Все», при выборе
    # периода или оплаты — «Выполнены» (они бывают только у выполненных), иначе — «Новые».
    tab = request.args.get("status", "")
    if tab not in STATUSES and tab != "all":
        if filters["q"]:
            tab = "all"
        elif filters["period"] or filters["oplata"]:
            tab = "done"
        else:
            tab = "new"

    base = [r for r in rows if matches(r, filters)]  # всё, кроме вкладки
    counts = {key: sum(r["status"] == key for r in base) for key in STATUSES}
    counts["all"] = len(base)
    found = base if tab == "all" else [r for r in base if r["status"] == tab]
    done = [r for r in found if r["status"] == "done"]
    by_payment = {}
    for r in done:
        if r["report_price"]:
            key = r["report_payment"] if r["report_payment"] in PAYMENTS else "unknown"
            by_payment[key] = by_payment.get(key, 0) + r["report_price"]
    summary = {
        "found": len(found),
        "done": len(done),
        "total": sum(r["report_price"] or 0 for r in done),
        # Порядок как в PAYMENTS; «не указана» — для старых отчётов без способа оплаты.
        "by_payment": [(PAYMENTS.get(k, "не указана"), by_payment[k])
                       for k in [*PAYMENTS, "unknown"] if k in by_payment],
    }
    return render_template(
        "office_home.html",
        requests=found,
        filters=filters,
        filtering=filtering,
        tab=tab,
        tabs=OFFICE_TABS,
        counts=counts,
        # Ссылки вкладок сохраняют поиск и фильтры.
        tab_links={key: url_for("office_home", status=key,
                                **{k: v for k, v in filters.items() if v and k != "status"})
                   for key in OFFICE_TABS},
        summary=summary,
        periods=PERIODS,
        brigades={k: v for k, v in USERS.items() if v["role"] == "brigade"},
        authors={k: v for k, v in USERS.items() if v["role"] in ("boss", "office")},
    )


OFFICE_TABS = {
    "new": "Новые",
    "in_work": "В работе",
    "done": "Выполнены",
    "cancelled": "Отменены",
    "all": "Все",
}

PERIODS = {
    "today": "сегодня",
    "yesterday": "вчера",
    "week": "за 7 дней",
    "month": "в этом месяце",
    "prev_month": "в прошлом месяце",
}


def period_range(period):
    """Период → (с какой даты, по какую дату включительно)."""
    today = now_local().date()
    if period == "today":
        return today, today
    if period == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    if period == "week":
        return today - timedelta(days=6), today
    first = today.replace(day=1)
    if period == "month":
        return first, today
    if period == "prev_month":
        last_prev = first - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    return None


def matches(r, f):
    """Подходит ли заявка под фильтры офиса."""
    if f["status"] and r["status"] != f["status"]:
        return False
    if f["brigada"] and r["taken_by"] != f["brigada"]:
        return False
    if f["avtor"] and r["created_by"] != f["avtor"]:
        return False
    if f["oplata"] and r["report_payment"] != f["oplata"]:
        return False
    if f["tip"] and r["request_type"] != f["tip"]:
        return False
    if f["oborud"] and r["equipment"] != f["oborud"]:
        return False
    if f["garantiya"] and r["warranty"] != (1 if f["garantiya"] == "da" else 0):
        return False
    if f["period"]:
        rng = period_range(f["period"])
        if rng:
            if r["status"] != "done":
                return False
            done_day = datetime.fromisoformat(r["done_at"]).date()
            if not rng[0] <= done_day <= rng[1]:
                return False
    if f["q"]:
        needle = f["q"].casefold()
        haystack = " ".join(
            str(r[k] or "") for k in
            ("client_name", "address", "phone", "work_type", "description", "report_work", "report_comment")
        ).casefold() + " " + " ".join((REQUEST_TYPES.get(r["request_type"], ""),
                                       EQUIPMENT.get(r["equipment"], ""))).casefold()
        by_number = f["q"].lstrip("№ ") == str(r["id"])
        if needle not in haystack and not by_number and not phone_matches(f["q"], r["phone"]):
            return False
    return True


def phone_matches(query, phone):
    """Ищем телефон по цифрам: «8 701 555» и «701-555» найдут «+7 701 555-12-34»."""
    digits = "".join(ch for ch in query if ch.isdigit())
    if len(digits) < 4:
        return False
    phone_digits = "".join(ch for ch in phone if ch.isdigit())
    variants = {digits}
    if len(digits) >= 7 and digits[0] in "78":
        variants.add(digits[1:])  # 8 или 7 в начале — код страны, его могли записать по-разному
    return any(v in phone_digits for v in variants)


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


COORDS_RE = re.compile(r"^\s*(-?\d{1,2}(?:[.,]\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:[.,]\d+)?)\s*$")
URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)


def parse_coords(text):
    """«43.2389, 76.8897» → (43.2389, 76.8897) или None."""
    found = COORDS_RE.match(text or "")
    if not found:
        return None
    lat, lon = (float(x.replace(",", ".")) for x in found.groups())
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def normalize_location(text):
    """Геолокация из формы → (что сохранить, ошибка).

    Принимаем координаты («43.2389, 76.8897») или ссылку на карту. Ссылку можно вставить
    вместе с текстом (как её присылают в мессенджере) — берём из текста саму ссылку.
    Сохраняем только http/https, чтобы в ссылке не оказалось ничего опасного.
    """
    coords = parse_coords(text)
    if coords:
        return f"{coords[0]:.6f}, {coords[1]:.6f}", None
    found = URL_RE.search(text)
    if found and len(found.group(1)) <= 1000:
        return found.group(1).rstrip(".,;)»\""), None
    return text, "Вставьте ссылку на карту (https://…) или координаты, например 43.2389, 76.8897."


def read_request_form(with_warranty=False):
    """Достаёт поля заявки из формы и проверяет их. Возвращает (данные, ошибки).

    «Гарантийная» при создании не задаётся (всегда «нет»); with_warranty=True — для исправления.
    """
    form = request.form
    data = {
        "client_name": form.get("client_name", "").strip(),
        "phone": form.get("phone", "").strip(),
        "address": form.get("address", "").strip(),
        "request_type": form.get("request_type", ""),
        "equipment": form.get("equipment", ""),
        "agreed_day": form.get("agreed_day", "").strip(),
        "agreed_month": form.get("agreed_month", "").strip(),
        "agreed_date": "",
        "agreed_time": form.get("agreed_time", "").strip(),
        "urgent": 1 if form.get("urgent") else 0,
        "warranty": (1 if form.get("warranty") else 0) if with_warranty else 0,
        "description": form.get("description", "").strip(),
        "location": form.get("location", "").strip(),
    }
    errors = {}
    if data["location"]:
        data["location"], location_error = normalize_location(data["location"])
        if location_error:
            errors["location"] = location_error
    if not data["client_name"]:
        errors["client_name"] = "Укажите имя клиента."
    if not data["phone"]:
        errors["phone"] = "Укажите телефон."
    elif sum(ch.isdigit() for ch in data["phone"]) < 5:
        errors["phone"] = "В телефоне слишком мало цифр."
    if not data["address"]:
        errors["address"] = "Укажите адрес."
    if data["request_type"] not in REQUEST_TYPES:
        errors["request_type"] = "Выберите тип заявки."
    if data["equipment"] not in EQUIPMENT:
        errors["equipment"] = "Выберите оборудование."
    if data["agreed_day"]:
        data["agreed_date"], date_error = build_agreed_date(data["agreed_day"], data["agreed_month"])
        if date_error:
            errors["agreed_date"] = date_error
    if data["agreed_time"]:
        if data["agreed_time"] not in AGREED_TIMES:
            errors["agreed_date"] = "Выберите время из списка."
        elif not data["agreed_day"]:
            errors["agreed_date"] = "Время указано, а день — нет. Выберите и день."
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
            user_id, at = current_user()["id"], now_local().isoformat()
            cur = db.execute(
                """INSERT INTO requests
                   (client_name, phone, address, work_type, request_type, equipment, warranty,
                    agreed_date, agreed_time, urgent,
                    description, location, status, created_by, created_at)
                   VALUES (?, ?, ?, '', ?, ?, 0, ?, ?, ?, ?, ?, 'new', ?, ?)""",
                (
                    data["client_name"],
                    data["phone"],
                    data["address"],
                    data["request_type"],
                    data["equipment"],
                    data["agreed_date"] or None,
                    data["agreed_time"] or None,
                    data["urgent"],
                    data["description"],
                    data["location"] or None,
                    user_id,
                    at,
                ),
            )
            log_event(db, cur.lastrowid, "created", user_id, at)
            db.commit()
            return redirect(url_for("request_view", request_id=cur.lastrowid, created=1))
    return render_request_form(data, errors)


def render_request_form(data, errors, edit=None):
    """Форма заявки: новая (edit=None) или исправление существующей (edit=строка заявки)."""
    today = now_local().date()
    quick_dates = [
        (label, d.day, f"{d.year}-{d.month:02d}")
        for label, d in (("Сегодня", today), ("Завтра", today + timedelta(days=1)),
                         ("Послезавтра", today + timedelta(days=2)))
    ]
    months = month_options()
    # У старой заявки день может быть в прошедшем месяце — добавим его в список.
    chosen = data.get("agreed_month")
    if chosen and chosen not in {value for value, _ in months}:
        year, month = (int(x) for x in chosen.split("-"))
        months.insert(0, (chosen, f"{MONTHS[month - 1]} {year}"))
    return render_template(
        "request_form.html",
        data=data,
        errors=errors,
        edit=edit,
        months=months,
        current_month=f"{today.year}-{today.month:02d}",
        quick_dates=quick_dates,
    )


@app.route("/ofis/statistika")
@login_required("boss")
def boss_stats():
    """Сквозная статистика для руководителя — та же, что в техническом разделе."""
    return render_template("boss_stats.html", **stats.page_context(
        get_db(), USERS, PAYMENTS, person_name, now_local().date(), request.args, "boss_stats",
        REQUEST_TYPES, EQUIPMENT))


@app.route("/zayavka/<int:request_id>")
@login_required("boss", "office")
def request_view(request_id):
    return render_template(
        "request_view.html",
        r=get_request_or_404(request_id),
        events=get_events(request_id),
        created=request.args.get("created"),
    )


# Что можно исправлять и как это называть в журнале.
EDITABLE_FIELDS = {
    "client_name": "имя клиента",
    "phone": "телефон",
    "address": "адрес",
    "request_type": "тип заявки",
    "equipment": "оборудование",
    "agreed_date": "день",
    "agreed_time": "время",
    "urgent": "срочность",
    "warranty": "гарантия",
    "description": "описание",
    "location": "геолокация",
}
REPORT_FIELDS = {
    "report_work": "что сделано",
    "report_price": "цена",
    "report_payment": "оплата",
    "report_comment": "комментарий",
}


@app.route("/zayavka/<int:request_id>/ispravit", methods=["GET", "POST"])
@login_required("boss")
def request_edit(request_id):
    row = get_request_or_404(request_id)
    if row["status"] == "cancelled":
        flash("Отменённую заявку исправить нельзя.", "error")
        return redirect(url_for("request_view", request_id=request_id))
    with_report = row["status"] == "done"

    if request.method == "GET":
        data = dict(row)
        if row["agreed_date"]:
            d = date.fromisoformat(row["agreed_date"])
            data["agreed_day"], data["agreed_month"] = str(d.day), f"{d.year}-{d.month:02d}"
        data["report_price"] = "" if row["report_price"] is None else str(row["report_price"])
        data["report_comment"] = row["report_comment"] or ""
        data["report_payment"] = row["report_payment"] or ""
        return render_request_form(data, {}, edit=row)

    data, errors = read_request_form(with_warranty=True)
    new_values = {name: (data[name] if data[name] != "" else None) for name in EDITABLE_FIELDS}
    new_values["urgent"] = data["urgent"]
    new_values["warranty"] = data["warranty"]
    fields = dict(EDITABLE_FIELDS)
    if with_report:
        fields.update(REPORT_FIELDS)
        report_data, report_values, report_errors = read_report_form()
        data.update(report_data)
        errors.update(report_errors)
        new_values.update(report_values)
    if errors:
        return render_request_form(data, errors, edit=row)

    changed = [label for name, label in fields.items() if new_values[name] != row[name]]
    if not changed:
        flash("Ничего не изменилось.", "ok")
        return redirect(url_for("request_view", request_id=request_id))

    db = get_db()
    at = now_local().isoformat()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    db.execute(
        f"UPDATE requests SET {assignments}, updated_at = ? WHERE id = ?",
        (*(new_values[name] for name in fields), at, request_id),
    )
    log_event(db, request_id, "edited", current_user()["id"], at, "исправлено: " + ", ".join(changed))
    db.commit()
    flash("Заявка исправлена.", "ok")
    return redirect(url_for("request_view", request_id=request_id))


@app.route("/zayavka/<int:request_id>/garantiya", methods=["POST"])
@login_required("boss", "office")
def request_warranty(request_id):
    """Переключатель «Гарантийная»: включают после создания заявки (по умолчанию — платная)."""
    row = get_request_or_404(request_id)
    if row["status"] == "cancelled":
        flash("У отменённой заявки гарантию не меняют.", "error")
        return redirect(url_for("request_view", request_id=request_id))
    value = 1 if request.form.get("warranty") == "1" else 0
    if value != row["warranty"]:
        db = get_db()
        user_id, at = current_user()["id"], now_local().isoformat()
        db.execute("UPDATE requests SET warranty = ?, updated_at = ? WHERE id = ?", (value, at, request_id))
        log_event(db, request_id, "warranty", user_id, at, "гарантийная" if value else "платная")
        db.commit()
        flash("Заявка отмечена как гарантийная." if value else "Заявка отмечена как платная.", "ok")
    return redirect(url_for("request_view", request_id=request_id))


@app.route("/zayavka/<int:request_id>/otmenit", methods=["POST"])
@login_required("boss")
def request_cancel(request_id):
    get_request_or_404(request_id)
    reason = request.form.get("reason", "").strip()
    db = get_db()
    user_id, at = current_user()["id"], now_local().isoformat()
    cur = db.execute(
        """UPDATE requests SET status = 'cancelled', cancelled_by = ?, cancelled_at = ?,
                  cancel_reason = ?, updated_at = ?
           WHERE id = ? AND status IN ('new', 'in_work')""",
        (user_id, at, reason or None, at, request_id),
    )
    if cur.rowcount == 0:
        db.rollback()
        flash("Эту заявку уже нельзя отменить.", "error")
    else:
        log_event(db, request_id, "cancelled", user_id, at, reason or None)
        db.commit()
        flash(f"Заявка №{request_id} отменена.", "ok")
    return redirect(url_for("request_view", request_id=request_id))


# ---------------------------------------------------------------- бригады

# Свободные: срочные первыми, потом по ближайшему дню (без дня — в конце), потом старые раньше.
FREE_ORDER = "urgent DESC, agreed_date IS NULL, agreed_date, id"


@app.route("/brigada")
@login_required("brigade")
def brigade_home():
    rows = get_db().execute(
        f"SELECT * FROM requests WHERE status = 'new' ORDER BY {FREE_ORDER}"
    ).fetchall()
    return render_template("brigade_home.html", requests=rows)


@app.route("/brigada/moi")
@login_required("brigade")
def brigade_mine():
    db, user_id = get_db(), current_user()["id"]
    rows = db.execute(
        f"SELECT * FROM requests WHERE status = 'in_work' AND taken_by = ? ORDER BY {FREE_ORDER}",
        (user_id,),
    ).fetchall()
    done = db.execute(
        """SELECT * FROM requests WHERE status = 'done' AND taken_by = ?
           ORDER BY done_at DESC LIMIT 20""",
        (user_id,),
    ).fetchall()
    return render_template("brigade_mine.html", requests=rows, done=done)


@app.route("/brigada/zayavka/<int:request_id>")
@login_required("brigade")
def brigade_request(request_id):
    row = get_request_or_404(request_id)
    mine = row["taken_by"] == current_user()["id"]
    if row["status"] != "new" and not mine:
        flash("Эту заявку уже взяла другая бригада или её отменили.", "error")
        return redirect(url_for("brigade_home"))
    return render_template("brigade_request.html", r=row, mine=mine)


@app.route("/brigada/zayavka/<int:request_id>/vzyat", methods=["POST"])
@login_required("brigade")
def brigade_take(request_id):
    get_request_or_404(request_id)
    db = get_db()
    user_id, at = current_user()["id"], now_local().isoformat()
    # Берём только если заявка ещё свободна. Проверка и запись — одной командой,
    # поэтому две бригады не могут взять одну заявку, даже нажав одновременно.
    cur = db.execute(
        """UPDATE requests SET status = 'in_work', taken_by = ?, taken_at = ?, updated_at = ?
           WHERE id = ? AND status = 'new'""",
        (user_id, at, at, request_id),
    )
    if cur.rowcount == 0:
        db.rollback()
        flash("Не получилось: эту заявку только что взяла другая бригада.", "error")
        return redirect(url_for("brigade_home"))
    log_event(db, request_id, "taken", user_id, at)
    db.commit()
    flash(f"Заявка №{request_id} ваша. Она теперь в «Мои заявки».", "ok")
    return redirect(url_for("brigade_request", request_id=request_id))


@app.route("/brigada/zayavka/<int:request_id>/vernut", methods=["POST"])
@login_required("brigade")
def brigade_return(request_id):
    get_request_or_404(request_id)
    db = get_db()
    user_id, at = current_user()["id"], now_local().isoformat()
    cur = db.execute(
        """UPDATE requests SET status = 'new', taken_by = NULL, taken_at = NULL, updated_at = ?
           WHERE id = ? AND status = 'in_work' AND taken_by = ?""",
        (at, request_id, user_id),
    )
    if cur.rowcount == 0:
        db.rollback()
        flash("Эту заявку нельзя вернуть: она не у вас в работе.", "error")
        return redirect(url_for("brigade_mine"))
    log_event(db, request_id, "returned", user_id, at)
    db.commit()
    flash(f"Заявка №{request_id} возвращена в свободные.", "ok")
    return redirect(url_for("brigade_mine"))


def parse_price(text):
    """«15 000», «15000 тг», «15.000» → 15000. Пусто или без цифр → None."""
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def read_report_form():
    """Поля отчёта из формы. Возвращает (данные для показа, значения для базы, ошибки)."""
    form = request.form
    data = {
        "report_work": form.get("report_work", "").strip(),
        "report_price": form.get("report_price", "").strip(),
        "report_payment": form.get("report_payment", ""),
        "report_comment": form.get("report_comment", "").strip(),
    }
    errors = {}
    price = parse_price(data["report_price"])
    if not data["report_work"]:
        errors["report_work"] = "Напишите, что сделали."
    if price is None:
        errors["report_price"] = "Укажите цену цифрами."
    elif price > 100_000_000:
        errors["report_price"] = "Слишком большая сумма, проверьте."
    payment = data["report_payment"] if data["report_payment"] in PAYMENTS else None
    if payment is None and price:  # при цене 0 (гарантия) оплаты нет
        errors["report_payment"] = "Выберите, как заплатил клиент."
    values = {
        "report_work": data["report_work"],
        "report_price": price,
        "report_payment": payment,
        "report_comment": data["report_comment"] or None,
    }
    return data, values, errors


@app.route("/brigada/zayavka/<int:request_id>/otchet", methods=["GET", "POST"])
@login_required("brigade")
def brigade_report(request_id):
    row = get_request_or_404(request_id)
    user_id = current_user()["id"]
    if row["status"] != "in_work" or row["taken_by"] != user_id:
        flash("Отчёт можно написать только по своей заявке в работе.", "error")
        return redirect(url_for("brigade_mine"))

    data, errors = {}, {}
    if request.method == "POST":
        data, values, errors = read_report_form()
        if not errors:
            db = get_db()
            at = now_local().isoformat()
            cur = db.execute(
                """UPDATE requests SET status = 'done', done_at = ?, report_work = ?,
                          report_price = ?, report_payment = ?, report_comment = ?, updated_at = ?
                   WHERE id = ? AND status = 'in_work' AND taken_by = ?""",
                (at, values["report_work"], values["report_price"], values["report_payment"],
                 values["report_comment"], at, request_id, user_id),
            )
            if cur.rowcount == 0:
                db.rollback()
                flash("Не получилось сохранить: заявка уже не у вас в работе.", "error")
                return redirect(url_for("brigade_mine"))
            log_event(db, request_id, "done", user_id, at)
            db.commit()
            flash(f"Отчёт сохранён. Заявка №{request_id} выполнена.", "ok")
            return redirect(url_for("brigade_mine"))
    return render_template("brigade_report.html", r=row, data=data, errors=errors)


# ---------------------------------------------------------------- технический раздел
# Суперадминистратор (разработчик, системный аудит): см. tech.py.

import tech  # noqa: E402

tech.register(app, {
    "get_db": get_db,
    "log": log,
    "USERS": USERS,
    "STATUSES": STATUSES,
    "PAYMENTS": PAYMENTS,
    "EVENT_LABELS": EVENT_LABELS,
    "now_local": now_local,
    "person_name": person_name,
    "REQUEST_TYPES": REQUEST_TYPES,
    "EQUIPMENT": EQUIPMENT,
    "LOG_FILE": LOG_FILE,
    "DATABASE": DATABASE,
})


@app.errorhandler(400)
def bad_request(_e):
    return render_template("error.html", message="Страница устарела. Вернитесь назад, обновите её и попробуйте ещё раз."), 400


@app.route("/favicon.ico")
def favicon():
    """Браузеры сами спрашивают /favicon.ico — отдаём наш значок, чтобы не было лишних 404."""
    return app.send_static_file("favicon-32.png")


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", message="Слишком много текста в форме. Сократите и попробуйте ещё раз."), 413


@app.errorhandler(500)
def server_error(_e):
    log.error("ОШИБКА 500 на %s %s", request.method, safe_for_log(request.path))
    return render_template("error.html", message="Что-то пошло не так. Обновите страницу и попробуйте ещё раз."), 500


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", message="Такой страницы или заявки нет."), 404


if __name__ == "__main__":
    import sys

    if "--telefon" in sys.argv:
        # Показ с телефона в той же Wi-Fi сети: принимаем подключения из сети,
        # режим разработчика выключен (его нельзя открывать другим устройствам).
        app.run(host="0.0.0.0", port=5000, debug=False)
    else:
        app.run(host="127.0.0.1", port=5000, debug=True)
