from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, time
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "database.db"
STANDARD_START_TIME = time(9, 0)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["DATABASE"] = str(DATABASE_PATH)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        connection = sqlite3.connect(app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.db = connection
    return g.db


@app.teardown_appcontext
def close_db(exception: Exception | None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db() -> None:
    db = get_db()
    schema = (BASE_DIR / "schema.sql").read_text(encoding="utf-8")
    db.executescript(schema)
    db.commit()


def query_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return get_db().execute(query, params).fetchone()


def query_all(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return get_db().execute(query, params).fetchall()


def execute(query: str, params: tuple[Any, ...] = ()) -> None:
    db = get_db()
    db.execute(query, params)
    db.commit()


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            flash("Сначала войдите в систему.", "warning")
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped_view(**kwargs):
        if g.user["role"] != "admin":
            flash("Доступ разрешён только администратору.", "danger")
            return redirect(url_for("employee_dashboard"))
        return view(**kwargs)

    return wrapped_view


def employee_required(view):
    @wraps(view)
    @login_required
    def wrapped_view(**kwargs):
        if g.user["role"] != "employee":
            flash("Эта страница доступна сотрудникам.", "danger")
            return redirect(url_for("admin_dashboard"))
        return view(**kwargs)

    return wrapped_view


@app.before_request
def load_logged_in_user() -> None:
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = query_one("SELECT * FROM users WHERE id = ?", (user_id,))


@app.context_processor
def inject_globals() -> dict[str, Any]:
    return {
        "current_user": g.user,
        "standard_start_time": STANDARD_START_TIME.strftime("%H:%M"),
    }


def first_user_is_registering() -> bool:
    row = query_one("SELECT COUNT(*) AS total FROM users")
    return row["total"] == 0


def get_employee_by_user_id(user_id: int) -> sqlite3.Row | None:
    return query_one(
        """
        SELECT e.*, u.username, u.role
        FROM employees e
        JOIN users u ON u.id = e.user_id
        WHERE e.user_id = ?
        """,
        (user_id,),
    )


def get_employee(employee_id: int) -> sqlite3.Row | None:
    return query_one(
        """
        SELECT e.*, u.username, u.role
        FROM employees e
        JOIN users u ON u.id = e.user_id
        WHERE e.id = ?
        """,
        (employee_id,),
    )


def parse_date_input(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def current_month_bounds() -> tuple[date, date]:
    today = date.today()
    first_day = today.replace(day=1)
    return first_day, today


def compute_hours(check_in: datetime, check_out: datetime) -> float:
    seconds = max((check_out - check_in).total_seconds(), 0)
    hours = seconds / 3600
    return round(hours, 2)


def late_minutes_for(check_in: datetime) -> int:
    standard_start = datetime.combine(check_in.date(), STANDARD_START_TIME)
    delay = int((check_in - standard_start).total_seconds() // 60)
    return max(delay, 0)


def employee_summary(employee_id: int, start_date: date, end_date: date) -> dict[str, Any]:
    summary = query_one(
        """
        SELECT
            e.id,
            e.full_name,
            e.position,
            e.hourly_rate,
            COALESCE(SUM(CASE WHEN wl.status = 'present' THEN wl.worked_hours ELSE 0 END), 0) AS total_hours,
            COALESCE(SUM(CASE WHEN wl.status = 'present' THEN wl.worked_hours * e.hourly_rate ELSE 0 END), 0) AS total_earnings,
            COALESCE(SUM(CASE WHEN wl.status = 'present' AND wl.late_minutes > 0 THEN 1 ELSE 0 END), 0) AS late_days,
            COALESCE(SUM(CASE WHEN wl.status = 'absent' THEN 1 ELSE 0 END), 0) AS absent_days,
            COALESCE(SUM(CASE WHEN wl.status = 'present' AND wl.check_in IS NOT NULL THEN 1 ELSE 0 END), 0) AS worked_days
        FROM employees e
        LEFT JOIN work_logs wl
            ON wl.employee_id = e.id
            AND wl.work_date BETWEEN ? AND ?
        WHERE e.id = ?
        GROUP BY e.id
        """,
        (start_date.isoformat(), end_date.isoformat(), employee_id),
    )
    return dict(summary) if summary else {}


def employee_chart_data(employee_id: int, start_date: date, end_date: date) -> dict[str, list[Any]]:
    rows = query_all(
        """
        SELECT
            wl.work_date,
            wl.worked_hours,
            wl.late_minutes,
            ROUND(wl.worked_hours * e.hourly_rate, 2) AS earnings
        FROM work_logs wl
        JOIN employees e ON e.id = wl.employee_id
        WHERE wl.employee_id = ?
          AND wl.work_date BETWEEN ? AND ?
          AND wl.status = 'present'
        ORDER BY wl.work_date
        """,
        (employee_id, start_date.isoformat(), end_date.isoformat()),
    )
    return {
        "labels": [row["work_date"] for row in rows],
        "hours": [row["worked_hours"] for row in rows],
        "late": [1 if row["late_minutes"] > 0 else 0 for row in rows],
        "earnings": [row["earnings"] for row in rows],
    }


def dashboard_chart_data(employee_id: int | None, start_date: date, end_date: date) -> dict[str, list[Any]]:
    params: tuple[Any, ...]
    employee_sql = ""
    if employee_id is not None:
        employee_sql = "AND wl.employee_id = ?"
        params = (start_date.isoformat(), end_date.isoformat(), employee_id)
    else:
        params = (start_date.isoformat(), end_date.isoformat())

    rows = query_all(
        f"""
        SELECT
            wl.work_date,
            ROUND(SUM(CASE WHEN wl.status = 'present' THEN wl.worked_hours ELSE 0 END), 2) AS hours,
            SUM(CASE WHEN wl.status = 'present' AND wl.late_minutes > 0 THEN 1 ELSE 0 END) AS late_count,
            ROUND(SUM(CASE WHEN wl.status = 'present' THEN wl.worked_hours * e.hourly_rate ELSE 0 END), 2) AS earnings
        FROM work_logs wl
        JOIN employees e ON e.id = wl.employee_id
        WHERE wl.work_date BETWEEN ? AND ?
          {employee_sql}
        GROUP BY wl.work_date
        ORDER BY wl.work_date
        """,
        params,
    )
    return {
        "labels": [row["work_date"] for row in rows],
        "hours": [row["hours"] for row in rows],
        "late": [row["late_count"] for row in rows],
        "earnings": [row["earnings"] for row in rows],
    }


def employee_scope_overview(employee_id: int | None, start_date: date, end_date: date) -> dict[str, Any]:
    employee_sql = ""
    params: tuple[Any, ...]
    if employee_id is not None:
        employee_sql = "AND e.id = ?"
        params = (start_date.isoformat(), end_date.isoformat(), employee_id)
    else:
        params = (start_date.isoformat(), end_date.isoformat())

    row = query_one(
        f"""
        SELECT
            COUNT(DISTINCT e.id) AS employee_count,
            COALESCE(SUM(CASE WHEN wl.status = 'present' THEN wl.worked_hours ELSE 0 END), 0) AS total_hours,
            COALESCE(SUM(CASE WHEN wl.status = 'present' THEN wl.worked_hours * e.hourly_rate ELSE 0 END), 0) AS total_earnings,
            COALESCE(SUM(CASE WHEN wl.status = 'present' AND wl.late_minutes > 0 THEN 1 ELSE 0 END), 0) AS total_late_days,
            COALESCE(SUM(CASE WHEN wl.status = 'absent' THEN 1 ELSE 0 END), 0) AS total_absences
        FROM employees e
        LEFT JOIN work_logs wl
            ON wl.employee_id = e.id
            AND wl.work_date BETWEEN ? AND ?
        WHERE 1 = 1
          {employee_sql}
        """,
        params,
    )
    return dict(row) if row else {}


def recent_activity(start_date: date, end_date: date, employee_id: int | None = None) -> list[sqlite3.Row]:
    employee_sql = ""
    params: tuple[Any, ...]
    if employee_id is not None:
        employee_sql = "AND e.id = ?"
        params = (start_date.isoformat(), end_date.isoformat(), employee_id)
    else:
        params = (start_date.isoformat(), end_date.isoformat())
    return query_all(
        f"""
        SELECT
            wl.work_date,
            wl.check_in,
            wl.check_out,
            wl.worked_hours,
            wl.late_minutes,
            wl.status,
            e.full_name,
            e.hourly_rate,
            ROUND(wl.worked_hours * e.hourly_rate, 2) AS day_earnings
        FROM work_logs wl
        JOIN employees e ON e.id = wl.employee_id
        WHERE wl.work_date BETWEEN ? AND ?
          {employee_sql}
        ORDER BY wl.work_date DESC, e.full_name ASC
        LIMIT 20
        """,
        params,
    )


@app.route("/")
def index():
    if g.user is None:
        return redirect(url_for("login"))
    if g.user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("employee_dashboard"))


@app.route("/register", methods=("GET", "POST"))
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        full_name = request.form.get("full_name", "").strip()
        position = request.form.get("position", "").strip() or "Сотрудник"

        error = None
        if not username:
            error = "Укажите логин."
        elif not password or len(password) < 4:
            error = "Пароль должен содержать минимум 4 символа."
        elif query_one("SELECT id FROM users WHERE username = ?", (username,)):
            error = "Пользователь с таким логином уже существует."

        if error is None:
            db = get_db()
            role = "admin" if first_user_is_registering() else "employee"
            cursor = db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), role),
            )
            user_id = cursor.lastrowid

            if role == "employee":
                db.execute(
                    """
                    INSERT INTO employees (user_id, full_name, position, hourly_rate)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, full_name or username, position, 0),
                )

            db.commit()
            flash(
                "Регистрация прошла успешно. Войдите в систему."
                if role == "employee"
                else "Первый пользователь создан как администратор. Теперь можно войти.",
                "success",
            )
            return redirect(url_for("login"))

        flash(error, "danger")

    return render_template("register.html", is_first_user=first_user_is_registering())


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user = query_one("SELECT * FROM users WHERE username = ?", (username,))

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Неверный логин или пароль.", "danger")
        else:
            session.clear()
            session["user_id"] = user["id"]
            flash("Вход выполнен.", "success")
            return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Вы вышли из системы.", "info")
    return redirect(url_for("login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    default_start, default_end = current_month_bounds()
    selected_employee_raw = request.args.get("employee_id", "").strip()
    start_date = parse_date_input(request.args.get("start_date"), default_start)
    end_date = parse_date_input(request.args.get("end_date"), default_end)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    employees = query_all(
        """
        SELECT e.id, e.full_name, e.position, e.hourly_rate, u.username
        FROM employees e
        JOIN users u ON u.id = e.user_id
        ORDER BY e.full_name
        """
    )

    selected_employee_id = int(selected_employee_raw) if selected_employee_raw.isdigit() else None
    card_filter = ""
    card_params: tuple[Any, ...] = (start_date.isoformat(), end_date.isoformat())
    if selected_employee_id is not None:
        card_filter = "WHERE e.id = ?"
        card_params = (start_date.isoformat(), end_date.isoformat(), selected_employee_id)

    cards = query_all(
        f"""
        SELECT
            e.id,
            e.full_name,
            e.position,
            e.hourly_rate,
            u.username,
            COALESCE(SUM(CASE WHEN wl.status = 'present' THEN wl.worked_hours ELSE 0 END), 0) AS total_hours,
            COALESCE(SUM(CASE WHEN wl.status = 'present' THEN wl.worked_hours * e.hourly_rate ELSE 0 END), 0) AS total_earnings,
            COALESCE(SUM(CASE WHEN wl.status = 'present' AND wl.late_minutes > 0 THEN 1 ELSE 0 END), 0) AS late_days,
            COALESCE(SUM(CASE WHEN wl.status = 'absent' THEN 1 ELSE 0 END), 0) AS absent_days
        FROM employees e
        JOIN users u ON u.id = e.user_id
        LEFT JOIN work_logs wl
            ON wl.employee_id = e.id
            AND wl.work_date BETWEEN ? AND ?
        {card_filter}
        GROUP BY e.id
        ORDER BY e.full_name
        """,
        card_params,
    )

    selected_employee = get_employee(selected_employee_id) if selected_employee_id else None
    chart_data = dashboard_chart_data(selected_employee_id, start_date, end_date)
    overview = employee_scope_overview(selected_employee_id, start_date, end_date)
    activity = recent_activity(start_date, end_date, selected_employee_id)

    return render_template(
        "admin/dashboard.html",
        employees=employees,
        employee_cards=cards,
        selected_employee=selected_employee,
        selected_employee_id=selected_employee_id,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        overview=overview,
        activity=activity,
        chart_data=chart_data,
    )


@app.route("/admin/employees/new", methods=("GET", "POST"))
@admin_required
def create_employee():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        position = request.form.get("position", "").strip()
        hourly_rate_raw = request.form.get("hourly_rate", "0").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        error = None
        try:
            hourly_rate = round(float(hourly_rate_raw), 2)
            if hourly_rate < 0:
                raise ValueError
        except ValueError:
            error = "Ставка должна быть положительным числом."
            hourly_rate = 0.0

        if not full_name:
            error = "Укажите имя сотрудника."
        elif not position:
            error = "Укажите должность."
        elif not username:
            error = "Укажите логин."
        elif not password or len(password) < 4:
            error = "Пароль должен содержать минимум 4 символа."
        elif query_one("SELECT id FROM users WHERE username = ?", (username,)):
            error = "Такой логин уже занят."

        if error is None:
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'employee')",
                (username, generate_password_hash(password)),
            )
            user_id = cursor.lastrowid
            db.execute(
                """
                INSERT INTO employees (user_id, full_name, position, hourly_rate)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, full_name, position, hourly_rate),
            )
            db.commit()
            flash("Сотрудник добавлен.", "success")
            return redirect(url_for("admin_dashboard"))

        flash(error, "danger")

    return render_template("admin/employee_form.html", employee=None)


@app.route("/admin/employees/<int:employee_id>/edit", methods=("GET", "POST"))
@admin_required
def edit_employee(employee_id: int):
    employee = get_employee(employee_id)
    if employee is None:
        flash("Сотрудник не найден.", "danger")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        position = request.form.get("position", "").strip()
        hourly_rate_raw = request.form.get("hourly_rate", "0").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        error = None
        try:
            hourly_rate = round(float(hourly_rate_raw), 2)
            if hourly_rate < 0:
                raise ValueError
        except ValueError:
            error = "Ставка должна быть положительным числом."
            hourly_rate = employee["hourly_rate"]

        if not full_name:
            error = "Укажите имя сотрудника."
        elif not position:
            error = "Укажите должность."
        elif not username:
            error = "Укажите логин."
        elif query_one(
            "SELECT id FROM users WHERE username = ? AND id != ?",
            (username, employee["user_id"]),
        ):
            error = "Такой логин уже занят."

        if error is None:
            db = get_db()
            if password:
                db.execute(
                    """
                    UPDATE users
                    SET username = ?, password_hash = ?
                    WHERE id = ?
                    """,
                    (username, generate_password_hash(password), employee["user_id"]),
                )
            else:
                db.execute(
                    "UPDATE users SET username = ? WHERE id = ?",
                    (username, employee["user_id"]),
                )

            db.execute(
                """
                UPDATE employees
                SET full_name = ?, position = ?, hourly_rate = ?
                WHERE id = ?
                """,
                (full_name, position, hourly_rate, employee_id),
            )
            db.commit()
            flash("Данные сотрудника обновлены.", "success")
            return redirect(url_for("admin_dashboard"))

        flash(error, "danger")

    return render_template("admin/employee_form.html", employee=employee)


@app.route("/admin/employees/<int:employee_id>/delete", methods=("POST",))
@admin_required
def delete_employee(employee_id: int):
    employee = get_employee(employee_id)
    if employee is None:
        flash("Сотрудник не найден.", "danger")
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (employee["user_id"],))
    db.commit()
    flash("Сотрудник удалён.", "info")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/absences/mark", methods=("POST",))
@admin_required
def mark_absence():
    employee_id_raw = request.form.get("employee_id", "").strip()
    work_date_raw = request.form.get("work_date", "").strip()

    if not employee_id_raw.isdigit():
        flash("Выберите сотрудника.", "danger")
        return redirect(url_for("admin_dashboard"))

    employee = get_employee(int(employee_id_raw))
    if employee is None:
        flash("Сотрудник не найден.", "danger")
        return redirect(url_for("admin_dashboard"))

    try:
        work_date = datetime.strptime(work_date_raw, "%Y-%m-%d").date()
    except ValueError:
        flash("Укажите корректную дату пропуска.", "danger")
        return redirect(url_for("admin_dashboard"))

    existing = query_one(
        "SELECT * FROM work_logs WHERE employee_id = ? AND work_date = ?",
        (employee["id"], work_date.isoformat()),
    )
    db = get_db()

    if existing and existing["status"] == "present":
        flash("За эту дату уже есть рабочий день. Пропуск не отмечен.", "warning")
        return redirect(url_for("admin_dashboard"))

    if existing:
        db.execute(
            """
            UPDATE work_logs
            SET status = 'absent',
                check_in = NULL,
                check_out = NULL,
                worked_hours = 0,
                late_minutes = 0,
                notes = ?
            WHERE id = ?
            """,
            ("Пропуск отмечен администратором", existing["id"]),
        )
    else:
        db.execute(
            """
            INSERT INTO work_logs (
                employee_id, work_date, check_in, check_out, worked_hours, late_minutes, status, notes
            )
            VALUES (?, ?, NULL, NULL, 0, 0, 'absent', ?)
            """,
            (employee["id"], work_date.isoformat(), "Пропуск отмечен администратором"),
        )
    db.commit()
    flash("Пропуск сохранён.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/employee/dashboard")
@employee_required
def employee_dashboard():
    employee = get_employee_by_user_id(g.user["id"])
    if employee is None:
        flash("Профиль сотрудника не найден.", "danger")
        session.clear()
        return redirect(url_for("login"))

    default_start, default_end = current_month_bounds()
    today_str = date.today().isoformat()
    today_log = query_one(
        """
        SELECT *
        FROM work_logs
        WHERE employee_id = ? AND work_date = ?
        """,
        (employee["id"], today_str),
    )

    summary = employee_summary(employee["id"], default_start, default_end)
    chart_data = employee_chart_data(employee["id"], default_start, default_end)
    history = query_all(
        """
        SELECT
            wl.*,
            ROUND(wl.worked_hours * ?, 2) AS day_earnings
        FROM work_logs wl
        WHERE wl.employee_id = ?
        ORDER BY wl.work_date DESC
        LIMIT 12
        """,
        (employee["hourly_rate"], employee["id"]),
    )

    return render_template(
        "employee/dashboard.html",
        employee=employee,
        summary=summary,
        today_log=today_log,
        history=history,
        chart_data=chart_data,
    )


@app.route("/employee/start-day", methods=("POST",))
@employee_required
def start_day():
    employee = get_employee_by_user_id(g.user["id"])
    if employee is None:
        flash("Профиль сотрудника не найден.", "danger")
        return redirect(url_for("employee_dashboard"))

    now = datetime.now()
    today_str = now.date().isoformat()
    existing = query_one(
        "SELECT * FROM work_logs WHERE employee_id = ? AND work_date = ?",
        (employee["id"], today_str),
    )

    if existing and existing["check_in"]:
        flash("Рабочий день уже начат.", "warning")
        return redirect(url_for("employee_dashboard"))

    late_minutes = late_minutes_for(now)
    db = get_db()
    if existing:
        db.execute(
            """
            UPDATE work_logs
            SET check_in = ?, check_out = NULL, worked_hours = 0, late_minutes = ?, status = 'present', notes = NULL
            WHERE id = ?
            """,
            (now.isoformat(timespec="minutes"), late_minutes, existing["id"]),
        )
    else:
        db.execute(
            """
            INSERT INTO work_logs (
                employee_id, work_date, check_in, check_out, worked_hours, late_minutes, status, notes
            )
            VALUES (?, ?, ?, NULL, 0, ?, 'present', NULL)
            """,
            (employee["id"], today_str, now.isoformat(timespec="minutes"), late_minutes),
        )
    db.commit()

    if late_minutes > 0:
        flash(f"Рабочий день начат. Зафиксировано опоздание: {late_minutes} мин.", "warning")
    else:
        flash("Рабочий день начат вовремя.", "success")
    return redirect(url_for("employee_dashboard"))


@app.route("/employee/end-day", methods=("POST",))
@employee_required
def end_day():
    employee = get_employee_by_user_id(g.user["id"])
    if employee is None:
        flash("Профиль сотрудника не найден.", "danger")
        return redirect(url_for("employee_dashboard"))

    now = datetime.now()
    today_str = now.date().isoformat()
    existing = query_one(
        """
        SELECT *
        FROM work_logs
        WHERE employee_id = ? AND work_date = ?
        """,
        (employee["id"], today_str),
    )

    if existing is None or not existing["check_in"]:
        flash("Сначала начните рабочий день.", "danger")
        return redirect(url_for("employee_dashboard"))

    if existing["check_out"]:
        flash("Рабочий день уже завершён.", "warning")
        return redirect(url_for("employee_dashboard"))

    check_in = datetime.fromisoformat(existing["check_in"])
    worked_hours = compute_hours(check_in, now)

    execute(
        """
        UPDATE work_logs
        SET check_out = ?, worked_hours = ?
        WHERE id = ?
        """,
        (now.isoformat(timespec="minutes"), worked_hours, existing["id"]),
    )
    flash(f"Рабочий день завершён. Отработано {worked_hours} ч.", "success")
    return redirect(url_for("employee_dashboard"))


def ensure_database() -> None:
    with app.app_context():
        if not DATABASE_PATH.exists():
            init_db()
            return

        db = get_db()
        table = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if table is None:
            init_db()


ensure_database()


if __name__ == "__main__":
    app.run(debug=True)
