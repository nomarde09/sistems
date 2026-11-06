"""Система заявок: монтаж камер и автоматических ворот."""

import hmac
import os
import secrets
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, redirect, render_template, request, session, url_for

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

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ["SECRET_KEY"],
    PERMANENT_SESSION_LIFETIME=timedelta(days=180),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


def current_user():
    user_id = session.get("user_id")
    if user_id in USERS:
        return {"id": user_id, **USERS[user_id]}
    return None


@app.context_processor
def inject_user():
    return {"user": current_user()}


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
    return render_template("login.html", users=USERS, selected=selected, error=error)


@app.route("/vyhod", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/ofis")
@login_required("boss", "office")
def office_home():
    return render_template("office_home.html")


@app.route("/brigada")
@login_required("brigade")
def brigade_home():
    return render_template("brigade_home.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
