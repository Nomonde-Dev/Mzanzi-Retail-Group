"""Mzanzi Retail Group server-backed sales analytics application.

Set FLASK_SECRET_KEY before running. Create the first administrator with:
    python app.py create-admin --email admin@example.com --name "Administrator"
Then run:
    python app.py run
"""

import argparse
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import pandas as pd
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from analysis import create_excel_report
from sales_data import REQUIRED_COLUMNS, validate_sales_data


ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "outputs" / "mzanzi_app.db"
DEFAULT_SOURCE = ROOT / "clean_sales.csv"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_RECORDS = 100_000
PASSWORD_MIN_LENGTH = 12
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_FAILURE_LIMIT = 10
REGISTRATION_WINDOW = timedelta(hours=1)
REGISTRATION_LIMIT = 5
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TRANSACTION_COLUMNS = REQUIRED_COLUMNS


def create_app(test_config=None):
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY"),
        DATABASE=os.environ.get("MZANZI_DATABASE", str(DEFAULT_DATABASE)),
        SOURCE_CSV=os.environ.get("MZANZI_SOURCE_CSV", str(DEFAULT_SOURCE)),
        SEED_DATA=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"]:
        raise RuntimeError(
            "Set FLASK_SECRET_KEY to a long, random secret before starting the app."
        )
    if app.config["SECRET_KEY"] == "dev" or len(str(app.config["SECRET_KEY"])) < 32:
        raise RuntimeError("FLASK_SECRET_KEY must contain at least 32 characters.")

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    initialize_database(app)

    @app.before_request
    def load_current_user():
        g.csp_nonce = secrets.token_urlsafe(18)
        g.user = None
        user_id = session.get("user_id")
        if user_id is not None:
            connection = connect_database(app)
            user = connection.execute(
                "SELECT id, name, email, role, active, session_version FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            connection.close()
            if (
                user is None
                or not user["active"]
                or user["session_version"] != session.get("session_version")
            ):
                session.clear()
            else:
                g.user = dict(user)

    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        nonce = getattr(g, "csp_nonce", "")
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
            f"style-src 'self' 'nonce-{nonce}'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self'; base-uri 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    @app.teardown_appcontext
    def close_database(_=None):
        connection = g.pop("database_connection", None)
        if connection is not None:
            connection.close()

    @app.get("/")
    def index():
        return redirect(url_for("dashboard") if g.user else url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("dashboard"))

        error = None
        email = ""
        if request.method == "POST":
            validate_csrf()
            email = request.form.get("email", "").strip().lower()[:254]
            password = request.form.get("password", "")
            if not EMAIL_PATTERN.fullmatch(email) or not password or len(password) > 1024:
                error = "Email or password is incorrect."
            elif login_is_limited(email):
                error = "Too many sign-in attempts. Try again in 15 minutes."
            else:
                connection = get_database()
                user = connection.execute(
                    "SELECT id, name, email, role, active, password_hash, session_version "
                    "FROM users WHERE email = ?",
                    (email,),
                ).fetchone()
                valid_password = check_password_hash(
                    user["password_hash"] if user else DUMMY_PASSWORD_HASH,
                    password,
                )
                if user and user["active"] and valid_password:
                    record_login_attempt(email, success=True)
                    session.clear()
                    session.permanent = request.form.get("remember") == "yes"
                    session["user_id"] = user["id"]
                    session["session_version"] = user["session_version"]
                    session["_csrf_token"] = secrets.token_urlsafe(32)
                    return redirect(url_for("dashboard"))
                record_login_attempt(email, success=False)
                error = "Email or password is incorrect."

        return render_template(
            "login.html",
            csrf_token=get_csrf_token(),
            csp_nonce=g.csp_nonce,
            error=error,
            email=email,
            notice=None,
        )

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if g.user:
            return redirect(url_for("dashboard"))

        error = None
        name = ""
        email = ""
        if request.method == "POST":
            validate_csrf()
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if registration_is_limited():
                error = "Registration is temporarily unavailable. Try again later."
            elif password != confirm_password:
                record_registration_attempt()
                error = "The passwords do not match."
            else:
                try:
                    validate_user_details(name, email, password, "analyst")
                except ValueError as validation_error:
                    record_registration_attempt()
                    error = str(validation_error)
                else:
                    connection = get_database()
                    try:
                        cursor = connection.execute(
                            """
                            INSERT INTO users
                                (name, email, password_hash, role, active, created_at)
                            VALUES (?, ?, ?, 'analyst', 1, ?)
                            """,
                            (name, email, hash_password(password), utc_timestamp()),
                        )
                        connection.commit()
                    except sqlite3.IntegrityError:
                        connection.rollback()
                        record_registration_attempt()
                        error = (
                            "Unable to register with those details. "
                            "Check the email address or sign in if you already have an account."
                        )
                    else:
                        record_registration_attempt()
                        session.clear()
                        session.permanent = True
                        session["user_id"] = cursor.lastrowid
                        session["session_version"] = 0
                        session["_csrf_token"] = secrets.token_urlsafe(32)
                        return redirect(url_for("dashboard"))

        return render_template(
            "register.html",
            csrf_token=get_csrf_token(),
            csp_nonce=g.csp_nonce,
            error=error,
            name=name,
            email=email,
        )

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        message = None
        if request.method == "POST":
            validate_csrf()
            message = (
                "For your privacy, this project does not confirm whether an account exists. "
                "Contact your administrator for a password reset."
            )
        return render_template(
            "forgot_password.html",
            csrf_token=get_csrf_token(),
            csp_nonce=g.csp_nonce,
            message=message or "Password resets are managed by an administrator.",
        )

    @app.post("/logout")
    def logout():
        validate_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        connection = get_database()
        dataset = get_active_dataset(connection)
        if dataset is None:
            abort(404, "No datasets are available.")
        rows = load_dataset_rows(connection, dataset["id"])
        rendered = render_dashboard(
            rows,
            dataset,
            g.user,
            get_csrf_token(),
            g.csp_nonce,
        )
        response = make_response(rendered)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response

    @app.get("/datasets")
    @login_required
    def datasets_page():
        connection = get_database()
        active = get_active_dataset(connection)
        datasets = connection.execute(
            """
            SELECT d.id, d.name, d.record_count, d.created_at, d.is_sample, u.name AS creator
            FROM datasets d LEFT JOIN users u ON u.id = d.created_by
            ORDER BY d.is_sample DESC, d.created_at DESC, d.id DESC
            """
        ).fetchall()
        return render_template(
            "datasets.html",
            datasets=datasets,
            active_id=active["id"] if active else None,
            current_user=g.user,
            csrf_token=get_csrf_token(),
            csp_nonce=g.csp_nonce,
            error=request.args.get("error"),
            notice=request.args.get("notice"),
        )

    @app.post("/datasets/<int:dataset_id>/select")
    @login_required
    def select_dataset(dataset_id):
        validate_csrf()
        connection = get_database()
        dataset = connection.execute(
            "SELECT id FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
        if dataset is None:
            abort(404)
        session["dataset_id"] = dataset_id
        return redirect(url_for("dashboard"))

    @app.post("/api/datasets/sample/select")
    @login_required
    def select_sample_dataset():
        validate_csrf()
        dataset = get_database().execute(
            "SELECT id FROM datasets WHERE is_sample = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if dataset is None:
            abort(404, "The supplied sample dataset is unavailable.")
        session["dataset_id"] = dataset["id"]
        return jsonify(dataset_id=dataset["id"])

    @app.post("/datasets/<int:dataset_id>/delete")
    @login_required
    @role_required("admin")
    def delete_dataset(dataset_id):
        validate_csrf()
        connection = get_database()
        dataset = connection.execute(
            "SELECT id, is_sample FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
        if dataset is None:
            abort(404)
        if dataset["is_sample"]:
            abort(400, "The supplied sample dataset cannot be deleted.")
        connection.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
        connection.commit()
        if session.get("dataset_id") == dataset_id:
            session.pop("dataset_id", None)
        return redirect(url_for("datasets_page", notice="Dataset and its transactions deleted."))

    @app.post("/api/datasets")
    @login_required
    def upload_dataset():
        validate_csrf()
        uploaded_file = request.files.get("file")
        if uploaded_file is None or not uploaded_file.filename:
            return jsonify(error="Choose a CSV file to upload."), 400
        if Path(uploaded_file.filename).suffix.lower() != ".csv":
            return jsonify(error="Only CSV files are supported."), 400
        payload = uploaded_file.read(MAX_UPLOAD_BYTES + 1)
        if len(payload) > MAX_UPLOAD_BYTES:
            return jsonify(error="The upload exceeds the 15 MB limit."), 413
        try:
            parsed = pd.read_csv(io.BytesIO(payload), encoding="utf-8-sig")
            data = validate_sales_data(parsed)
        except (UnicodeDecodeError, pd.errors.ParserError, ValueError) as error:
            return jsonify(error=str(error)), 400
        if data.empty:
            return jsonify(error="The CSV has no sales records."), 400
        if len(data) > MAX_RECORDS:
            return jsonify(error=f"The CSV exceeds the {MAX_RECORDS:,} record limit."), 400

        display_name = secure_filename(Path(uploaded_file.filename).name) or "sales.csv"
        if len(display_name) > 100:
            display_name = display_name[:100]
        connection = get_database()
        dataset_id = insert_dataset(
            connection,
            data,
            display_name,
            created_by=g.user["id"],
            is_sample=False,
        )
        connection.commit()
        session["dataset_id"] = dataset_id
        app.logger.info(
            "Dataset uploaded: dataset_id=%s user_id=%s records=%s",
            dataset_id,
            g.user["id"],
            len(data),
        )
        return jsonify(
            dataset_id=dataset_id,
            name=display_name,
            record_count=len(data),
        ), 201

    @app.get("/reports/sales.xlsx")
    @login_required
    def download_excel_report():
        connection = get_database()
        dataset = get_active_dataset(connection)
        if dataset is None:
            abort(404, "No datasets are available.")
        data = load_dataset_frame(connection, dataset["id"])
        output = io.BytesIO()
        create_excel_report(data, output)
        safe_name = secure_filename(dataset["name"].rsplit(".", 1)[0]) or "sales"
        return send_file(
            output,
            as_attachment=True,
            download_name=f"Mzanzi_{safe_name}_Report.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/reports/sales.csv")
    @login_required
    def download_csv_report():
        connection = get_database()
        dataset = get_active_dataset(connection)
        if dataset is None:
            abort(404, "No datasets are available.")
        data = load_dataset_frame(connection, dataset["id"])
        output = io.StringIO(newline="")
        data.to_csv(output, index=False)
        return send_file(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            as_attachment=True,
            download_name="Mzanzi_Sales_Transactions.csv",
            mimetype="text/csv; charset=utf-8",
        )

    @app.get("/admin/users")
    @login_required
    @role_required("admin")
    def admin_users():
        connection = get_database()
        users = connection.execute(
            "SELECT id, name, email, role, active FROM users ORDER BY email"
        ).fetchall()
        return render_template(
            "users.html",
            users=users,
            csrf_token=get_csrf_token(),
            csp_nonce=g.csp_nonce,
            error=request.args.get("error"),
            notice=request.args.get("notice"),
        )

    @app.post("/admin/users")
    @login_required
    @role_required("admin")
    def update_users():
        validate_csrf()
        action = request.form.get("action")
        connection = get_database()
        try:
            if action == "create":
                name = request.form.get("name", "").strip()
                email = request.form.get("email", "").strip().lower()
                password = request.form.get("password", "")
                role = request.form.get("role", "")
                validate_user_details(name, email, password, role)
                connection.execute(
                    "INSERT INTO users (name, email, password_hash, role, active, created_at) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (
                        name,
                        email,
                        hash_password(password),
                        role,
                        utc_timestamp(),
                    ),
                )
                notice = f"Created {email} as {role}."
            elif action == "reset_password":
                user_id = parse_user_id(request.form.get("user_id"))
                password = request.form.get("password", "")
                validate_password(password)
                cursor = connection.execute(
                    "UPDATE users SET password_hash = ?, session_version = session_version + 1 "
                    "WHERE id = ?",
                    (                    hash_password(password), user_id),
                )
                if cursor.rowcount == 0:
                    abort(404)
                notice = "Password reset. The user must sign in again."
            elif action == "update":
                user_id = parse_user_id(request.form.get("user_id"))
                name = request.form.get("name", "").strip()
                role = request.form.get("role", "")
                active_value = request.form.get("active")
                if not name or len(name) > 100 or role not in {"admin", "analyst"}:
                    raise ValueError("Provide a valid name and role.")
                if active_value not in {"0", "1"}:
                    raise ValueError("Choose a valid account status.")
                active = int(active_value)
                existing = connection.execute(
                    "SELECT id, role, active FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                if existing is None:
                    abort(404)
                if user_id == g.user["id"] and (not active or role != "admin"):
                    raise ValueError("You cannot disable or demote your own active administrator account.")
                if existing["role"] == "admin" and existing["active"] and (
                    not active or role != "admin"
                ):
                    if active_admin_count(connection, excluding_user=user_id) == 0:
                        raise ValueError("At least one active administrator account must remain.")
                connection.execute(
                    "UPDATE users SET name = ?, role = ?, active = ? WHERE id = ?",
                    (name, role, active, user_id),
                )
                notice = "User account updated."
            else:
                raise ValueError("Unknown user-management action.")
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            return redirect(
                url_for("admin_users", error="An account with that email address already exists.")
            )
        except ValueError as error:
            connection.rollback()
            return redirect(url_for("admin_users", error=str(error)))

        return redirect(url_for("admin_users", notice=notice))

    @app.errorhandler(413)
    def request_too_large(_):
        if request.path.startswith("/api/"):
            return jsonify(error="The upload exceeds the 15 MB limit."), 413
        return "Request too large.", 413

    return app


def connect_database(app):
    connection = sqlite3.connect(app.config["DATABASE"], timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def get_database():
    if "database_connection" not in g:
        from flask import current_app

        g.database_connection = connect_database(current_app)
    return g.database_connection


def html_escape(value):
    from html import escape

    return escape(str(value), quote=True)


def initialize_database(app):
    connection = connect_database(app)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'analyst')),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
            session_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS datasets (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            record_count INTEGER NOT NULL CHECK(record_count >= 0),
            created_at TEXT NOT NULL,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            is_sample INTEGER NOT NULL DEFAULT 0 CHECK(is_sample IN (0, 1))
        );
        CREATE TABLE IF NOT EXISTS sales_transactions (
            dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
            order_id TEXT NOT NULL,
            order_date TEXT NOT NULL,
            month TEXT NOT NULL,
            year INTEGER NOT NULL,
            region TEXT NOT NULL,
            category TEXT NOT NULL,
            product TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            discount REAL NOT NULL,
            unit_cost REAL NOT NULL,
            revenue REAL NOT NULL,
            cost REAL NOT NULL,
            profit REAL NOT NULL,
            PRIMARY KEY (dataset_id, order_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sales_dataset_month
            ON sales_transactions(dataset_id, month);
        CREATE INDEX IF NOT EXISTS idx_sales_dataset_region
            ON sales_transactions(dataset_id, region);
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY,
            address_hash TEXT NOT NULL,
            email TEXT NOT NULL,
            succeeded INTEGER NOT NULL CHECK(succeeded IN (0, 1)),
            attempted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_login_attempts_recent
            ON login_attempts(address_hash, email, attempted_at);
        CREATE TABLE IF NOT EXISTS registration_attempts (
            id INTEGER PRIMARY KEY,
            address_hash TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_registration_attempts_recent
            ON registration_attempts(address_hash, attempted_at);
        """
    )
    user_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()
    }
    if "session_version" not in user_columns:
        connection.execute(
            "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0"
        )
    if app.config["SEED_DATA"]:
        existing = connection.execute("SELECT id FROM datasets LIMIT 1").fetchone()
        if existing is None:
            source_path = Path(app.config["SOURCE_CSV"])
            if not source_path.is_file():
                connection.close()
                raise FileNotFoundError(f"Required source data was not found: {source_path}")
            data = validate_sales_data(pd.read_csv(source_path))
            insert_dataset(
                connection,
                data,
                "Supplied sample data",
                created_by=None,
                is_sample=True,
            )
    connection.commit()
    connection.close()


def insert_dataset(connection, data, name, created_by, is_sample):
    cursor = connection.execute(
        "INSERT INTO datasets (name, record_count, created_at, created_by, is_sample) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, len(data), utc_timestamp(), created_by, int(is_sample)),
    )
    dataset_id = cursor.lastrowid
    rows = []
    for row in data.itertuples(index=False):
        rows.append(
            (
                dataset_id,
                str(row.order_id),
                str(row.order_date),
                str(row.month),
                int(row.year),
                str(row.region),
                str(row.category),
                str(row.product),
                int(row.quantity),
                float(row.unit_price),
                float(row.discount),
                float(row.unit_cost),
                float(row.revenue),
                float(row.cost),
                float(row.profit),
            )
        )
    connection.executemany(
        """
        INSERT INTO sales_transactions (
            dataset_id, order_id, order_date, month, year, region, category,
            product, quantity, unit_price, discount, unit_cost, revenue, cost, profit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return dataset_id


def load_dataset_rows(connection, dataset_id):
    rows = connection.execute(
        """
        SELECT order_date, month, year, region, category, product,
               quantity, unit_price, discount, unit_cost, revenue, cost, profit, order_id
        FROM sales_transactions
        WHERE dataset_id = ?
        ORDER BY order_date, order_id
        """,
        (dataset_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_dataset_frame(connection, dataset_id):
    rows = connection.execute(
        """
        SELECT order_id, order_date, month, year, region, category, product,
               quantity, unit_price, discount, unit_cost, revenue, cost, profit
        FROM sales_transactions
        WHERE dataset_id = ?
        ORDER BY order_date, order_id
        """,
        (dataset_id,),
    ).fetchall()
    return pd.DataFrame([dict(row) for row in rows], columns=TRANSACTION_COLUMNS)


def get_active_dataset(connection):
    dataset_id = session.get("dataset_id")
    if dataset_id is not None:
        dataset = connection.execute(
            "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
        if dataset is not None:
            return dataset
    dataset = connection.execute(
        "SELECT * FROM datasets ORDER BY is_sample DESC, created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if dataset is not None:
        session["dataset_id"] = dataset["id"]
    return dataset


def render_dashboard(rows, dataset, user, csrf_token, csp_nonce):
    path = ROOT / "dashboard_template.html"
    if not path.is_file():
        raise FileNotFoundError(f"Dashboard template was not found: {path}")
    document = path.read_text(encoding="utf-8")
    sales_json = (
        pd.DataFrame(rows)
        .to_json(orient="records", force_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    replacements = {
        "__SALES_DATA__": sales_json,
        "__REFRESHED_AT__": dataset["created_at"],
        "__SERVER_MODE__": "true",
        "__CSRF_TOKEN__": csrf_token,
        "__CSP_NONCE__": csp_nonce,
        "__USER_DISPLAY_NAME__": html_escape(user["name"]),
        "__USER_ROLE__": html_escape(user["role"].title()),
        "__ADMIN_NAV__": (
            '<a href="/admin/users">Manage Users</a>'
            if user["role"] == "admin"
            else ""
        ),
    }
    for marker, value in replacements.items():
        document = document.replace(marker, value)
    return document


def get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf():
    expected = session.get("_csrf_token", "")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, "Invalid or missing CSRF token.")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required."), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def role_required(role):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                abort(401)
            if g.user["role"] != role:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorate


def login_is_limited(email):
    cutoff = (datetime.now(timezone.utc) - LOGIN_WINDOW).isoformat()
    address_hash = hashed_client_address()
    count = get_database().execute(
        """
        SELECT COUNT(*) FROM login_attempts
        WHERE address_hash = ? AND email = ? AND succeeded = 0 AND attempted_at >= ?
        """,
        (address_hash, email, cutoff),
    ).fetchone()[0]
    return count >= LOGIN_FAILURE_LIMIT


def registration_is_limited():
    cutoff = (datetime.now(timezone.utc) - REGISTRATION_WINDOW).isoformat()
    count = get_database().execute(
        """
        SELECT COUNT(*) FROM registration_attempts
        WHERE address_hash = ? AND attempted_at >= ?
        """,
        (hashed_client_address(), cutoff),
    ).fetchone()[0]
    return count >= REGISTRATION_LIMIT


def record_registration_attempt():
    connection = get_database()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    connection.execute(
        "DELETE FROM registration_attempts WHERE attempted_at < ?", (cutoff,)
    )
    connection.execute(
        "INSERT INTO registration_attempts (address_hash, attempted_at) VALUES (?, ?)",
        (hashed_client_address(), utc_timestamp()),
    )
    connection.commit()


def record_login_attempt(email, success):
    connection = get_database()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    connection.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
    connection.execute(
        "INSERT INTO login_attempts (address_hash, email, succeeded, attempted_at) "
        "VALUES (?, ?, ?, ?)",
        (hashed_client_address(), email, int(success), utc_timestamp()),
    )
    connection.commit()


def hashed_client_address():
    from flask import current_app

    address = request.remote_addr or "unknown"
    return hmac.new(
        str(current_app.config["SECRET_KEY"]).encode("utf-8"),
        address.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def validate_password(password):
    if not isinstance(password, str) or not PASSWORD_MIN_LENGTH <= len(password) <= 1024:
        raise ValueError(f"Passwords must be {PASSWORD_MIN_LENGTH} to 1024 characters.")
    if password.strip() != password:
        raise ValueError("Passwords cannot begin or end with whitespace.")


def validate_user_details(name, email, password, role):
    if not name or len(name) > 100:
        raise ValueError("Name must contain 1 to 100 characters.")
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Provide a valid email address.")
    if role not in {"admin", "analyst"}:
        raise ValueError("Choose a valid account role.")
    validate_password(password)


def parse_user_id(value):
    try:
        user_id = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid user account.") from error
    if user_id <= 0:
        raise ValueError("Invalid user account.")
    return user_id


def active_admin_count(connection, excluding_user=None):
    if excluding_user is None:
        query = "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
        parameters = ()
    else:
        query = (
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id != ?"
        )
        parameters = (excluding_user,)
    return connection.execute(query, parameters).fetchone()[0]


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password):
    return generate_password_hash(password, method="pbkdf2:sha256:600000")


DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def bootstrap_admin(email, name):
    email = email.strip().lower()
    name = name.strip()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Provide a valid administrator email address.")
    if not name or len(name) > 100:
        raise ValueError("Name must contain 1 to 100 characters.")
    password = ""
    confirmation = ""
    import getpass

    while True:
        password = getpass.getpass("Administrator password (12+ characters): ")
        confirmation = getpass.getpass("Confirm administrator password: ")
        if password != confirmation:
            print("Passwords do not match. Try again.")
            continue
        try:
            validate_password(password)
        except ValueError as error:
            print(error)
            continue
        break

    app = create_app()
    connection = connect_database(app)
    try:
        connection.execute(
            "INSERT INTO users (name, email, password_hash, role, active, created_at) "
            "VALUES (?, ?, ?, 'admin', 1, ?)",
            (name, email, hash_password(password), utc_timestamp()),
        )
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise ValueError(f"An account with {email} already exists.") from error
    finally:
        connection.close()
    print(f"Administrator account created for {email}.")


def main():
    parser = argparse.ArgumentParser(description="Mzanzi Retail Group BI application")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_admin_parser = subparsers.add_parser(
        "create-admin",
        help="Create the first administrator account",
    )
    create_admin_parser.add_argument("--email", required=True)
    create_admin_parser.add_argument("--name", required=True)
    subparsers.add_parser("run", help="Run the local development web server")
    args = parser.parse_args()

    if args.command == "create-admin":
        try:
            bootstrap_admin(args.email, args.name)
        except ValueError as error:
            parser.error(str(error))
        return

    app = create_app()
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )


if __name__ == "__main__":
    main()
