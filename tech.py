"""Технический раздел суперадминистратора (разработчик, системный аудит).

Изоляция от обычных входов:
- свой адрес /tech и своя форма входа /tech/vhod, не связанные с /vhod сотрудников;
- логин и пароль только из окружения (.env): SUPERADMIN_LOGIN, SUPERADMIN_PASSWORD;
  без них (или с паролем короче 12 символов) раздел выключен и отвечает 404;
- своя отметка в сессии (tech_admin), без user_id: с ней нельзя открыть страницы
  сотрудников, а сессия сотрудника не открывает /tech;
- сессия суперадмина живёт 8 часов и не «запоминается» браузером;
- после 5 неудачных попыток с одного адреса вход блокируется на 15 минут;
- права только на чтение; каждое действие суперадмина пишется в лог приложения.

В интерфейсе сотрудников эта учётная запись нигде не показывается: её нет в USERS.
"""

import hmac
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

import stats as stats_module

MIN_PASSWORD_LENGTH = 12
SESSION_HOURS = 8
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60

# Неудачные попытки входа: адрес → время попыток. Хранится в памяти процесса.
_failed_attempts = defaultdict(list)


def credentials():
    """(логин, пароль) из окружения или None, если раздел не настроен."""
    login = os.environ.get("SUPERADMIN_LOGIN", "").strip()
    password = os.environ.get("SUPERADMIN_PASSWORD", "")
    if not login or len(password) < MIN_PASSWORD_LENGTH:
        return None
    return login, password


def _recent_fails(ip):
    now = time.monotonic()
    _failed_attempts[ip] = [t for t in _failed_attempts[ip] if now - t < LOCK_SECONDS]
    return _failed_attempts[ip]


def register(app, deps):
    """Подключает раздел к приложению. deps — нужные функции и настройки из app.py."""
    bp = Blueprint("tech", __name__, url_prefix="/tech")
    get_db, log = deps["get_db"], deps["log"]
    USERS, STATUSES, PAYMENTS = deps["USERS"], deps["STATUSES"], deps["PAYMENTS"]
    EVENT_LABELS, now_local = deps["EVENT_LABELS"], deps["now_local"]
    LOG_FILE, DATABASE = deps["LOG_FILE"], deps["DATABASE"]
    started_at = now_local()

    def is_tech_session():
        if not session.get("tech_admin"):
            return False
        since = session.get("tech_since")
        try:
            return datetime.fromisoformat(since) + timedelta(hours=SESSION_HOURS) > now_local()
        except (TypeError, ValueError):
            return False

    @bp.before_request
    def guard():
        """Охрана всего раздела: выключен — 404; не вошёл — на форму входа."""
        if credentials() is None:
            abort(404)
        if request.endpoint == "tech.login":
            return None
        if not is_tech_session():
            session.pop("tech_admin", None)
            session.pop("tech_since", None)
            return redirect(url_for("tech.login"))
        return None

    @bp.after_request
    def no_cache(response):
        # Страницы раздела не кэшируются и не индексируются.
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    # ------------------------------------------------------------ вход и выход

    @bp.route("/vhod", methods=["GET", "POST"])
    def login():
        error = None
        ip = request.remote_addr or "?"
        if request.method == "POST":
            if len(_recent_fails(ip)) >= MAX_FAILS:
                log.warning("ТЕХ-ВХОД заблокирован (много попыток) с %s", ip)
                error = "Слишком много попыток. Подождите 15 минут."
            else:
                login_value, password = credentials()
                sent_login = request.form.get("login", "")
                sent_password = request.form.get("password", "")
                ok_login = hmac.compare_digest(sent_login.encode(), login_value.encode())
                ok_password = hmac.compare_digest(sent_password.encode(), password.encode())
                if ok_login and ok_password:
                    _failed_attempts.pop(ip, None)
                    session.clear()  # обычная сессия сотрудника, если была, закрывается
                    session.permanent = False
                    session["tech_admin"] = True
                    session["tech_since"] = now_local().isoformat()
                    log.warning("ТЕХ-ВХОД успешно с %s", ip)
                    return redirect(url_for("tech.stats"))
                _failed_attempts[ip].append(time.monotonic())
                log.warning("ТЕХ-ВХОД неудачно с %s", ip)
                error = "Неверный логин или пароль."
        return render_template("tech/login.html", error=error)

    @bp.route("/vyhod", methods=["POST"])
    def logout():
        log.warning("ТЕХ-ВЫХОД с %s", request.remote_addr)
        session.clear()
        return redirect(url_for("tech.login"))

    # ------------------------------------------------------------ статистика

    @bp.route("/")
    def stats():
        ctx = stats_module.page_context(get_db(), USERS, PAYMENTS, deps["person_name"],
                                        now_local().date(), request.args, "tech.stats",
                                        deps["REQUEST_TYPES"], deps["EQUIPMENT"])
        system = {
            "started_at": started_at,
            "python": sys.version.split()[0],
            "db_size": DATABASE.stat().st_size if DATABASE.exists() else 0,
            "log_size": sum(p.stat().st_size for p in LOG_FILE.parent.glob("app.log*")),
            "requests": ctx["s"]["total_requests"],
            "events": ctx["s"]["events_count"],
        }
        return render_template("tech/stats.html", system=system, **ctx)

    # ------------------------------------------------------------ журнал действий

    @bp.route("/zhurnal")
    def journal():
        f = {
            "user": request.args.get("user", ""),
            "action": request.args.get("action", ""),
            "request_id": request.args.get("request_id", "").strip(),
        }
        sql = """SELECT e.*, r.client_name, r.address FROM events e
                 LEFT JOIN requests r ON r.id = e.request_id WHERE 1 = 1"""
        params = []
        if f["user"] in USERS:
            sql += " AND e.user_id = ?"
            params.append(f["user"])
        if f["action"] in EVENT_LABELS:
            sql += " AND e.action = ?"
            params.append(f["action"])
        if f["request_id"].isdigit():
            sql += " AND e.request_id = ?"
            params.append(int(f["request_id"]))
        sql += " ORDER BY e.at DESC, e.id DESC LIMIT 500"
        return render_template("tech/journal.html", events=get_db().execute(sql, params).fetchall(),
                               f=f, actions=EVENT_LABELS)

    @bp.route("/zayavka/<int:request_id>")
    def request_card(request_id):
        db = get_db()
        r = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if r is None:
            abort(404)
        events = db.execute("SELECT * FROM events WHERE request_id = ? ORDER BY at, id",
                            (request_id,)).fetchall()
        return render_template("tech/request.html", r=r, events=events)

    # ------------------------------------------------------------ лог приложения

    @bp.route("/log")
    def app_log():
        try:
            limit = max(50, min(int(request.args.get("n", 300)), 5000))
        except ValueError:
            limit = 300
        only = request.args.get("only", "")
        needle = request.args.get("q", "").strip()
        lines = []
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        if only == "warnings":
            lines = [ln for ln in lines if " WARNING " in ln or " ERROR " in ln]
        elif only == "logins":
            lines = [ln for ln in lines if "ВХОД" in ln or "ВЫХОД" in ln]
        if needle:
            lines = [ln for ln in lines if needle.casefold() in ln.casefold()]
        return render_template("tech/log.html", lines=list(reversed(lines[-limit:])), limit=limit,
                               only=only, q=needle, log_name=LOG_FILE.name)

    app.register_blueprint(bp)
