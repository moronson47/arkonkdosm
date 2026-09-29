"""Веб-приложение для оценивания конкурса профессионального мастерства
инспекторов по досмотру.

Локальный запуск:
    python app.py            # отладочный сервер http://127.0.0.1:5000
    python app.py --init     # создать БД и пользователей
    python app.py --init --force   # пересоздать БД (с подтверждением)

Production:
    gunicorn --workers 3 --bind unix:/var/www/konkurs/konkurs.sock wsgi:app
"""
import io
import os
import secrets
import string
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from flask import (  # noqa: E402
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFError  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

from extensions import csrf  # noqa: E402
from models import close_db, get_db, init_db  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LENGTH = 8
MAX_COMMENT_LENGTH = 500

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


_flask_env = os.environ.get("FLASK_ENV", "development")

_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    _secret_key = secrets.token_hex(32)
    print(
        "[ВНИМАНИЕ] Переменная SECRET_KEY не задана. Сгенерирован временный ключ, "
        "сессии будут сбрасываться при каждом перезапуске. Задайте SECRET_KEY в .env.",
        file=sys.stderr,
    )

app.config.update(
    SECRET_KEY=_secret_key,
    DATABASE_PATH=os.environ.get("DATABASE_PATH")
    or os.path.join(BASE_DIR, "konkurs.db"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env_bool("SESSION_COOKIE_SECURE", _flask_env == "production"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    WTF_CSRF_TIME_LIMIT=None,
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    ADMIN_EMAIL=os.environ.get("ADMIN_EMAIL", ""),
)

csrf.init_app(app)
app.teardown_appcontext(close_db)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def is_locked():
    return get_setting("scoring_locked", "0") == "1"


def audit(action, details=""):
    try:
        db = get_db()
        db.execute(
            "INSERT INTO audit_log(user_id, action, details, ip, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session.get("user_id"), action, details, get_client_ip(), now_str()),
        )
        db.commit()
    except Exception:
        pass


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            session.clear()
            target = request.path
            return redirect(url_for("login", next=target))
        if g.user["must_change_password"] and request.endpoint not in (
            "change_password",
            "logout",
        ):
            return redirect(url_for("change_password"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def stage_criteria_count(db, stage_id):
    row = db.execute(
        "SELECT COUNT(*) AS c FROM criteria c "
        "JOIN blocks b ON c.block_id = b.id WHERE b.stage_id = ?",
        (stage_id,),
    ).fetchone()
    return row["c"]


def stage_max_score(db, stage_id):
    row = db.execute(
        "SELECT COALESCE(SUM(c.max_score), 0) AS m FROM criteria c "
        "JOIN blocks b ON c.block_id = b.id WHERE b.stage_id = ?",
        (stage_id,),
    ).fetchone()
    return row["m"]


def generate_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


def home_url_for(role):
    """Домашний раздел для роли пользователя."""
    if role == "admin":
        return url_for("admin_dashboard")
    if role == "observer":
        return url_for("observer_dashboard")
    return url_for("teams")


def observer_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("login", next=request.path))
        if g.user["role"] not in ("admin", "observer"):
            abort(403)
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Жизненный цикл запроса
# ---------------------------------------------------------------------------
@app.before_request
def load_logged_in_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None or not row["is_active"]:
            session.clear()
        else:
            g.user = row


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    return (
        render_template(
            "error.html",
            code=400,
            message="Форма устарела или отсутствует CSRF-токен. Обновите страницу и повторите действие.",
        ),
        400,
    )


@app.errorhandler(403)
def handle_403(error):
    return (
        render_template("error.html", code=403, message="Доступ запрещён."),
        403,
    )


@app.errorhandler(404)
def handle_404(error):
    return (
        render_template("error.html", code=404, message="Страница не найдена."),
        404,
    )


@app.errorhandler(500)
def handle_500(error):
    return (
        render_template(
            "error.html",
            code=500,
            message="Внутренняя ошибка сервера. Попробуйте ещё раз позже.",
        ),
        500,
    )


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------
def _attempts_count(db, ip):
    cutoff = (datetime.now() - timedelta(minutes=LOCKOUT_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    row = db.execute(
        "SELECT COUNT(*) AS c FROM login_attempts WHERE ip = ? AND attempted_at > ?",
        (ip, cutoff),
    ).fetchone()
    return row["c"]


def _record_attempt(db, ip):
    db.execute(
        "INSERT INTO login_attempts(ip, attempted_at) VALUES (?, ?)", (ip, now_str())
    )
    db.commit()


def _clear_attempts(db, ip):
    db.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
    db.commit()


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user") is not None:
        return redirect(url_for("index"))

    next_url = safe_next(request.args.get("next") or request.form.get("next"))

    if request.method == "POST":
        db = get_db()
        ip = get_client_ip()

        if _attempts_count(db, ip) >= MAX_LOGIN_ATTEMPTS:
            flash(
                f"Слишком много неудачных попыток входа. Попробуйте через {LOCKOUT_MINUTES} минут.",
                "error",
            )
            return render_template("login.html", next_url=next_url), 429

        login_value = (request.form.get("login") or "").strip()
        password = request.form.get("password") or ""

        row = db.execute(
            "SELECT * FROM users WHERE login = ?", (login_value,)
        ).fetchone()

        if row is None or not check_password_hash(row["password_hash"], password):
            _record_attempt(db, ip)
            flash("Неверный логин или пароль.", "error")
            return render_template("login.html", next_url=next_url), 401

        if not row["is_active"]:
            flash("Учётная запись деактивирована администратором.", "error")
            return render_template("login.html", next_url=next_url), 403

        _clear_attempts(db, ip)
        db.execute(
            "UPDATE users SET last_login = ? WHERE id = ?", (now_str(), row["id"])
        )
        db.commit()

        session.clear()
        session.permanent = True
        session["user_id"] = row["id"]

        audit("login", f"Вход пользователя {row['login']}")

        if row["must_change_password"]:
            return redirect(url_for("change_password"))

        if next_url:
            return redirect(next_url)
        return redirect(home_url_for(row["role"]))

    return render_template("login.html", next_url=next_url)


@app.route("/logout")
def logout():
    if g.get("user") is not None:
        audit("logout", f"Выход пользователя {g.user['login']}")
    session.clear()
    flash("Вы вышли из системы.", "info")
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old_password = request.form.get("old_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        error = None
        if not check_password_hash(g.user["password_hash"], old_password):
            error = "Неверный текущий пароль."
        elif len(new_password) < MIN_PASSWORD_LENGTH:
            error = f"Новый пароль должен содержать не менее {MIN_PASSWORD_LENGTH} символов."
        elif new_password != confirm_password:
            error = "Пароли не совпадают."

        if error:
            flash(error, "error")
            return render_template("change_password.html"), 400

        db = get_db()
        db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (generate_password_hash(new_password), g.user["id"]),
        )
        db.commit()
        audit("change_password", "Смена пароля пользователем")

        flash("Пароль успешно изменён.", "success")
        return redirect(home_url_for(g.user["role"]))

    return render_template("change_password.html")


# ---------------------------------------------------------------------------
# Эксперт
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("landing.html")


@app.route("/teams")
@login_required
def teams():
    if g.user["role"] != "expert":
        return redirect(home_url_for(g.user["role"]))
    db = get_db()
    team_rows = db.execute("SELECT * FROM teams ORDER BY order_num, id").fetchall()
    total_criteria = db.execute("SELECT COUNT(*) AS c FROM criteria").fetchone()["c"]

    items = []
    for team in team_rows:
        scored = db.execute(
            "SELECT COUNT(*) AS c FROM scores "
            "WHERE expert_id = ? AND team_id = ? AND value IS NOT NULL",
            (g.user["id"], team["id"]),
        ).fetchone()["c"]
        items.append({"team": team, "scored": scored, "total": total_criteria})

    return render_template("teams.html", items=items)


@app.route("/team/<int:team_id>")
@login_required
def team_stages(team_id):
    if g.user["role"] != "expert":
        return redirect(home_url_for(g.user["role"]))
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    stage_rows = db.execute("SELECT * FROM stages ORDER BY order_num, id").fetchall()

    items = []
    for stage in stage_rows:
        total = stage_criteria_count(db, stage["id"])
        stat = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(s.value), 0) AS total "
            "FROM scores s "
            "JOIN criteria c ON s.criterion_id = c.id "
            "JOIN blocks b ON c.block_id = b.id "
            "WHERE s.expert_id = ? AND s.team_id = ? AND b.stage_id = ? "
            "AND s.value IS NOT NULL",
            (g.user["id"], team_id, stage["id"]),
        ).fetchone()
        items.append(
            {
                "stage": stage,
                "scored": stat["c"],
                "total": total,
                "sum": stat["total"],
                "max": stage_max_score(db, stage["id"]),
            }
        )

    return render_template("stages.html", team=team, items=items)


@app.route("/team/<int:team_id>/stage/<int:stage_id>")
@login_required
def checklist(team_id, stage_id):
    if g.user["role"] != "expert":
        return redirect(home_url_for(g.user["role"]))
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    stage = db.execute("SELECT * FROM stages WHERE id = ?", (stage_id,)).fetchone()
    if team is None or stage is None:
        abort(404)

    block_rows = db.execute(
        "SELECT * FROM blocks WHERE stage_id = ? ORDER BY order_num, id", (stage_id,)
    ).fetchall()

    blocks = []
    total = 0
    scored = 0
    for block in block_rows:
        criterion_rows = db.execute(
            "SELECT * FROM criteria WHERE block_id = ? ORDER BY order_num, id",
            (block["id"],),
        ).fetchall()
        items = []
        for criterion in criterion_rows:
            saved = db.execute(
                "SELECT value, comment FROM scores "
                "WHERE expert_id = ? AND team_id = ? AND criterion_id = ?",
                (g.user["id"], team_id, criterion["id"]),
            ).fetchone()
            value = saved["value"] if saved else None
            comment = (saved["comment"] if saved else "") or ""
            if value is not None:
                scored += 1
            total += 1
            items.append({"criterion": criterion, "value": value, "comment": comment})
        blocks.append({"block": block, "items": items})

    return render_template(
        "checklist.html",
        team=team,
        stage=stage,
        blocks=blocks,
        total=total,
        scored=scored,
        locked=is_locked(),
    )


@app.route("/api/score", methods=["POST"])
@login_required
def api_score():
    if g.user["role"] != "expert":
        return jsonify(ok=False, error="Оценивать может только эксперт."), 403
    if is_locked():
        return jsonify(ok=False, error="Оценивание заблокировано администратором."), 403

    data = request.get_json(silent=True) or {}
    try:
        team_id = int(data.get("team_id"))
        criterion_id = int(data.get("criterion_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Некорректные данные запроса."), 400

    db = get_db()
    criterion = db.execute(
        "SELECT c.*, b.stage_id FROM criteria c "
        "JOIN blocks b ON c.block_id = b.id WHERE c.id = ?",
        (criterion_id,),
    ).fetchone()
    if criterion is None:
        return jsonify(ok=False, error="Критерий не найден."), 400

    team = db.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        return jsonify(ok=False, error="Команда не найдена."), 400

    raw_value = data.get("value", None)
    if raw_value is None or raw_value == "":
        value = None
    else:
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Балл должен быть целым числом."), 400
        value = max(0, min(value, criterion["max_score"]))

    comment = (data.get("comment") or "").strip()[:MAX_COMMENT_LENGTH]
    timestamp = now_str()

    if value is None and not comment:
        db.execute(
            "DELETE FROM scores WHERE expert_id = ? AND team_id = ? AND criterion_id = ?",
            (g.user["id"], team_id, criterion_id),
        )
    else:
        db.execute(
            "INSERT INTO scores(expert_id, team_id, criterion_id, value, comment, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(expert_id, team_id, criterion_id) DO UPDATE SET "
            "value = excluded.value, comment = excluded.comment, updated_at = excluded.updated_at",
            (g.user["id"], team_id, criterion_id, value, comment, timestamp),
        )
    db.commit()

    audit("score_change", f"team={team_id} criterion={criterion_id} value={value}")

    return jsonify(ok=True, value=value, comment=comment, updated_at=timestamp)


# ---------------------------------------------------------------------------
# Администратор
# ---------------------------------------------------------------------------
def _team_stage_stats(db, team_id, stage_id):
    rows = db.execute(
        "SELECT s.expert_id AS expert_id, COALESCE(SUM(s.value), 0) AS total, "
        "MAX(s.updated_at) AS last FROM scores s "
        "JOIN criteria c ON s.criterion_id = c.id "
        "JOIN blocks b ON c.block_id = b.id "
        "WHERE s.team_id = ? AND b.stage_id = ? AND s.value IS NOT NULL "
        "GROUP BY s.expert_id",
        (team_id, stage_id),
    ).fetchall()
    if not rows:
        return 0.0, 0, None
    average = sum(row["total"] for row in rows) / len(rows)
    lasts = [row["last"] for row in rows if row["last"]]
    return average, len(rows), (max(lasts) if lasts else None)


def build_results_grid(db):
    """Сводка команда × этап: средний балл по всем экспертам и число оценивших."""
    team_rows = db.execute("SELECT * FROM teams ORDER BY order_num, id").fetchall()
    stage_rows = db.execute("SELECT * FROM stages ORDER BY order_num, id").fetchall()

    grid = []
    for team in team_rows:
        cells = []
        total = 0.0
        for stage in stage_rows:
            average, count, last = _team_stage_stats(db, team["id"], stage["id"])
            cells.append(
                {
                    "stage": stage,
                    "avg": round(average, 2),
                    "count": count,
                    "last": last,
                }
            )
            total += average
        grid.append({"team": team, "cells": cells, "total": round(total, 2)})

    return team_rows, stage_rows, grid


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    team_rows, stage_rows, grid = build_results_grid(db)
    expert_rows = db.execute(
        "SELECT id, login, full_name FROM users WHERE role = 'expert' ORDER BY id"
    ).fetchall()
    expert_count = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE role = 'expert' AND is_active = 1"
    ).fetchone()["c"]

    return render_template(
        "admin_dashboard.html",
        grid=grid,
        stages=stage_rows,
        teams=team_rows,
        experts=expert_count,
        expert_list=expert_rows,
        locked=is_locked(),
    )


@app.route("/observer")
@observer_required
def observer_dashboard():
    return render_template("observer.html", locked=is_locked())


@app.route("/api/results")
@observer_required
def api_results():
    db = get_db()
    _team_rows, stage_rows, grid = build_results_grid(db)
    return jsonify(
        ok=True,
        generated_at=now_str(),
        stages=[{"id": stage["id"], "name": stage["name"]} for stage in stage_rows],
        teams=[
            {
                "id": row["team"]["id"],
                "name": row["team"]["name"],
                "cells": [
                    {"avg": cell["avg"], "count": cell["count"], "last": cell["last"]}
                    for cell in row["cells"]
                ],
                "total": row["total"],
            }
            for row in grid
        ],
    )


@app.route("/admin/users")
@admin_required
def admin_users():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM users ORDER BY role, id"
    ).fetchall()
    return render_template("admin_users.html", users=rows)


@app.route("/admin/users/add", methods=["POST"])
@admin_required
def admin_user_add():
    db = get_db()
    login_value = (request.form.get("login") or "").strip()
    full_name = (request.form.get("full_name") or "").strip()
    role = request.form.get("role") or "expert"

    if role not in ("admin", "expert", "observer"):
        role = "expert"

    if not login_value:
        flash("Укажите логин пользователя.", "error")
        return redirect(url_for("admin_users"))

    exists = db.execute(
        "SELECT id FROM users WHERE login = ?", (login_value,)
    ).fetchone()
    if exists:
        flash("Пользователь с таким логином уже существует.", "error")
        return redirect(url_for("admin_users"))

    password = generate_password()
    db.execute(
        "INSERT INTO users(login, password_hash, full_name, role, is_active, "
        "must_change_password, created_at) VALUES (?, ?, ?, ?, 1, 1, ?)",
        (
            login_value,
            generate_password_hash(password),
            full_name or login_value,
            role,
            now_str(),
        ),
    )
    db.commit()
    audit("user_add", f"Добавлен пользователь {login_value} ({role})")
    flash(f"Пользователь добавлен. Пароль: {password}", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/rename", methods=["POST"])
@admin_required
def admin_user_rename(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)

    full_name = (request.form.get("full_name") or "").strip()
    login_value = (request.form.get("login") or "").strip() or user["login"]

    other = db.execute(
        "SELECT id FROM users WHERE login = ? AND id != ?", (login_value, user_id)
    ).fetchone()
    if other:
        flash("Такой логин уже занят.", "error")
        return redirect(url_for("admin_users"))

    db.execute(
        "UPDATE users SET full_name = ?, login = ? WHERE id = ?",
        (full_name or user["full_name"], login_value, user_id),
    )
    db.commit()
    audit("user_rename", f"Изменён пользователь id={user_id}")
    flash("Данные пользователя обновлены.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reset", methods=["POST"])
@admin_required
def admin_user_reset(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)

    password = generate_password()
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
        (generate_password_hash(password), user_id),
    )
    db.commit()
    audit("user_reset_password", f"Сброшен пароль пользователя {user['login']}")
    flash(f"Новый пароль для {user['login']}: {password}", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_user_toggle(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)

    if user["id"] == g.user["id"]:
        flash("Нельзя деактивировать собственную учётную запись.", "error")
        return redirect(url_for("admin_users"))

    new_state = 0 if user["is_active"] else 1
    db.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_state, user_id))
    db.commit()
    audit("user_toggle", f"Пользователь {user['login']} is_active={new_state}")
    flash(
        "Пользователь активирован." if new_state else "Пользователь деактивирован.",
        "success",
    )
    return redirect(url_for("admin_users"))


@app.route("/admin/teams")
@admin_required
def admin_teams():
    db = get_db()
    rows = db.execute("SELECT * FROM teams ORDER BY order_num, id").fetchall()
    return render_template("admin_teams.html", teams=rows)


@app.route("/admin/teams/add", methods=["POST"])
@admin_required
def admin_team_add():
    db = get_db()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Укажите название команды.", "error")
        return redirect(url_for("admin_teams"))

    order_row = db.execute("SELECT COALESCE(MAX(order_num), 0) + 1 AS n FROM teams").fetchone()
    db.execute(
        "INSERT INTO teams(name, order_num) VALUES (?, ?)", (name, order_row["n"])
    )
    db.commit()
    audit("team_add", f"Добавлена команда {name}")
    flash("Команда добавлена.", "success")
    return redirect(url_for("admin_teams"))


@app.route("/admin/teams/<int:team_id>/rename", methods=["POST"])
@admin_required
def admin_team_rename(team_id):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    name = (request.form.get("name") or "").strip()
    if name:
        db.execute("UPDATE teams SET name = ? WHERE id = ?", (name, team_id))
        db.commit()
        audit("team_rename", f"Команда id={team_id} переименована в {name}")
        flash("Название команды обновлено.", "success")
    return redirect(url_for("admin_teams"))


@app.route("/admin/teams/<int:team_id>/delete", methods=["POST"])
@admin_required
def admin_team_delete(team_id):
    db = get_db()
    team = db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if team is None:
        abort(404)

    db.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    db.commit()
    audit("team_delete", f"Удалена команда {team['name']}")
    flash("Команда удалена вместе со всеми её оценками.", "success")
    return redirect(url_for("admin_teams"))


@app.route("/admin/results")
@admin_required
def admin_results():
    db = get_db()
    team_rows = db.execute("SELECT * FROM teams ORDER BY order_num, id").fetchall()
    stage_rows = db.execute("SELECT * FROM stages ORDER BY order_num, id").fetchall()
    experts = db.execute(
        "SELECT * FROM users WHERE role = 'expert' ORDER BY id"
    ).fetchall()

    report = []
    for team in team_rows:
        stages_data = []
        for stage in stage_rows:
            expert_rows = []
            for expert in experts:
                stat = db.execute(
                    "SELECT COUNT(*) AS c, COALESCE(SUM(s.value), 0) AS total, "
                    "MAX(s.updated_at) AS last FROM scores s "
                    "JOIN criteria c ON s.criterion_id = c.id "
                    "JOIN blocks b ON c.block_id = b.id "
                    "WHERE s.expert_id = ? AND s.team_id = ? AND b.stage_id = ? "
                    "AND s.value IS NOT NULL",
                    (expert["id"], team["id"], stage["id"]),
                ).fetchone()
                if stat["c"] > 0:
                    expert_rows.append(
                        {
                            "expert": expert,
                            "scored": stat["c"],
                            "total": stat["total"],
                            "last": stat["last"],
                        }
                    )
            if expert_rows:
                stages_data.append({"stage": stage, "experts": expert_rows})
        report.append({"team": team, "stages": stages_data})

    return render_template(
        "admin_results.html",
        report=report,
        stages=stage_rows,
        experts=experts,
    )


@app.route("/admin/lock", methods=["POST"])
@admin_required
def admin_lock():
    new_value = "0" if is_locked() else "1"
    set_setting("scoring_locked", new_value)
    audit("lock_toggle", f"scoring_locked={new_value}")
    flash(
        "Оценивание заблокировано." if new_value == "1" else "Оценивание разблокировано.",
        "success",
    )
    return redirect(safe_next(request.form.get("return_to")) or url_for("admin_dashboard"))


@app.route("/admin/reset", methods=["POST"])
@admin_required
def admin_reset():
    db = get_db()
    scope = request.form.get("scope") or "all"

    if scope == "expert":
        expert_id = request.form.get("expert_id", type=int)
        if not expert_id:
            flash("Выберите эксперта.", "error")
            return redirect(url_for("admin_dashboard"))
        db.execute("DELETE FROM scores WHERE expert_id = ?", (expert_id,))
        details = f"Сброс оценок эксперта id={expert_id}"
    elif scope == "team":
        team_id = request.form.get("team_id", type=int)
        if not team_id:
            flash("Выберите команду.", "error")
            return redirect(url_for("admin_dashboard"))
        db.execute("DELETE FROM scores WHERE team_id = ?", (team_id,))
        details = f"Сброс оценок команды id={team_id}"
    else:
        db.execute("DELETE FROM scores")
        details = "Полный сброс оценок"

    db.commit()
    audit("reset_scores", details)
    flash("Оценки сброшены.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/audit")
@admin_required
def admin_audit():
    db = get_db()
    user_id = request.args.get("user_id", type=int)
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()

    query = (
        "SELECT a.*, u.login AS user_login, u.full_name AS user_name "
        "FROM audit_log a LEFT JOIN users u ON a.user_id = u.id WHERE 1=1"
    )
    params = []
    if user_id:
        query += " AND a.user_id = ?"
        params.append(user_id)
    if date_from:
        query += " AND a.created_at >= ?"
        params.append(date_from + " 00:00:00")
    if date_to:
        query += " AND a.created_at <= ?"
        params.append(date_to + " 23:59:59")
    query += " ORDER BY a.id DESC LIMIT 500"

    rows = db.execute(query, params).fetchall()
    users = db.execute("SELECT id, login, full_name FROM users ORDER BY login").fetchall()

    return render_template(
        "admin_audit.html",
        records=rows,
        users=users,
        selected_user=user_id,
        date_from=date_from,
        date_to=date_to,
    )


# ---------------------------------------------------------------------------
# Экспорт Excel
# ---------------------------------------------------------------------------
HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
HEADER_FONT = Font(bold=True)
WRAP_ALIGN = Alignment(wrap_text=True, vertical="top")


def _autosize(ws, min_width=10, max_width=50):
    widths = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            for line in str(cell.value).split("\n"):
                widths[cell.column] = max(widths.get(cell.column, 0), len(line))
    for column, width in widths.items():
        ws.column_dimensions[get_column_letter(column)].width = min(
            max(width + 2, min_width), max_width
        )


def _style_header(ws):
    for cell in ws[1]:
        if cell.value is not None:
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = WRAP_ALIGN
    ws.freeze_panes = "A2"


def _round(value):
    return round(value, 2)


@app.route("/admin/export.xlsx")
@admin_required
def admin_export():
    db = get_db()
    team_rows = db.execute("SELECT * FROM teams ORDER BY order_num, id").fetchall()
    stage_rows = db.execute("SELECT * FROM stages ORDER BY order_num, id").fetchall()
    experts = db.execute(
        "SELECT * FROM users WHERE role = 'expert' ORDER BY id"
    ).fetchall()

    wb = Workbook()
    wb.remove(wb.active)

    # Лист "Итоги"
    ws = wb.create_sheet("Итоги")
    ws.append(["Команда"] + [stage["name"] for stage in stage_rows] + ["ИТОГО"])
    for team in team_rows:
        row = [team["name"]]
        total = 0.0
        for stage in stage_rows:
            average, _count, _last = _team_stage_stats(db, team["id"], stage["id"])
            row.append(_round(average))
            total += average
        row.append(_round(total))
        ws.append(row)
    _style_header(ws)
    _autosize(ws)

    # Листы по этапам
    for index, stage in enumerate(stage_rows, start=1):
        sheet = wb.create_sheet(f"Этап {index}")
        criterion_rows = db.execute(
            "SELECT c.id, c.name, c.max_score, b.name AS block_name, b.order_num AS b_order, "
            "c.order_num AS c_order FROM criteria c "
            "JOIN blocks b ON c.block_id = b.id WHERE b.stage_id = ? "
            "ORDER BY b.order_num, b.id, c.order_num, c.id",
            (stage["id"],),
        ).fetchall()

        sheet.append(["Блок", "Критерий", "Макс. балл"] + [t["name"] for t in team_rows])
        for criterion in criterion_rows:
            row = [criterion["block_name"], criterion["name"], criterion["max_score"]]
            for team in team_rows:
                stat = db.execute(
                    "SELECT AVG(s.value) AS avg FROM scores s "
                    "JOIN criteria c ON s.criterion_id = c.id "
                    "WHERE c.id = ? AND s.team_id = ? AND s.value IS NOT NULL",
                    (criterion["id"], team["id"]),
                ).fetchone()
                row.append(_round(stat["avg"]) if stat["avg"] is not None else None)
            sheet.append(row)
        _style_header(sheet)
        _autosize(sheet)

    # Лист "По экспертам"
    ws_exp = wb.create_sheet("По экспертам")
    ws_exp.append(
        ["Эксперт", "Команда", "Этап", "Сумма баллов", "Оценено критериев", "Последнее обновление"]
    )
    for expert in experts:
        for team in team_rows:
            for stage in stage_rows:
                stat = db.execute(
                    "SELECT COUNT(*) AS c, COALESCE(SUM(s.value), 0) AS total, "
                    "MAX(s.updated_at) AS last FROM scores s "
                    "JOIN criteria c ON s.criterion_id = c.id "
                    "JOIN blocks b ON c.block_id = b.id "
                    "WHERE s.expert_id = ? AND s.team_id = ? AND b.stage_id = ? "
                    "AND s.value IS NOT NULL",
                    (expert["id"], team["id"], stage["id"]),
                ).fetchone()
                if stat["c"] == 0:
                    continue
                ws_exp.append(
                    [
                        expert["full_name"] or expert["login"],
                        team["name"],
                        stage["name"],
                        stat["total"],
                        stat["c"],
                        stat["last"],
                    ]
                )
    _style_header(ws_exp)
    _autosize(ws_exp)

    # Лист "Детально"
    ws_detail = wb.create_sheet("Детально")
    ws_detail.append(
        [
            "Эксперт",
            "Команда",
            "Этап",
            "Блок",
            "Критерий",
            "Макс",
            "Балл",
            "Комментарий",
            "Дата",
        ]
    )
    detail_rows = db.execute(
        "SELECT u.full_name AS expert_name, u.login AS expert_login, t.name AS team_name, "
        "st.name AS stage_name, b.name AS block_name, c.name AS criterion_name, "
        "c.max_score AS max_score, s.value AS value, s.comment AS comment, "
        "s.updated_at AS updated_at, b.order_num AS b_order, c.order_num AS c_order "
        "FROM scores s "
        "JOIN users u ON s.expert_id = u.id "
        "JOIN teams t ON s.team_id = t.id "
        "JOIN criteria c ON s.criterion_id = c.id "
        "JOIN blocks b ON c.block_id = b.id "
        "JOIN stages st ON b.stage_id = st.id "
        "ORDER BY st.order_num, t.order_num, u.id, b.order_num, c.order_num",
    ).fetchall()
    for row in detail_rows:
        ws_detail.append(
            [
                row["expert_name"] or row["expert_login"],
                row["team_name"],
                row["stage_name"],
                row["block_name"],
                row["criterion_name"],
                row["max_score"],
                row["value"],
                row["comment"] or "",
                row["updated_at"] or "",
            ]
        )
    _style_header(ws_detail)
    _autosize(ws_detail, max_width=60)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = "konkurs_{}.xlsx".format(datetime.now().strftime("%Y%m%d_%H%M"))
    audit("export_excel", f"Экспорт результатов в {filename}")

    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/admin/backup")
@admin_required
def admin_backup():
    db = get_db()
    db.execute("PRAGMA wal_checkpoint(FULL)")
    db.commit()

    path = app.config["DATABASE_PATH"]
    if not os.path.exists(path):
        abort(404)

    audit("backup_download", "Скачивание резервной копии БД")

    filename = "konkurs_backup_{}.db".format(datetime.now().strftime("%Y%m%d_%H%M"))
    return send_file(
        path,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=filename,
    )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Конкурс: сервер оценивания")
    parser.add_argument("--init", action="store_true", help="инициализировать БД")
    parser.add_argument(
        "--force", action="store_true", help="пересоздать БД (удаляет все данные)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)

    if args.init:
        if args.force:
            print("ВНИМАНИЕ: база данных будет пересоздана, ВСЕ данные будут удалены!")
            answer = input("Введите 'yes' для продолжения: ").strip().lower()
            if answer not in ("yes", "y", "да"):
                print("Отменено.")
                return 1
        with app.app_context():
            init_db(force=args.force)
        print(f"База данных готова: {app.config['DATABASE_PATH']}")
        print("Пользователи: admin/admin, ekspert1..ekspert5 (пароль = логин).")
        return 0

    with app.app_context():
        init_db(force=False)
    app.run(host=args.host, port=args.port, debug=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
