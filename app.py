import os
import re
import secrets
import string
import logging
import shlex
import subprocess
from datetime import datetime, timedelta

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from flask import Flask, redirect, render_template, request, url_for


app = Flask(__name__)

ALPHANUMERIC = string.ascii_letters + string.digits
DEFAULT_TABLE_NAME = "mn8_brana_access"
DEFAULT_PRIMARY_KEY_COLUMN = "id"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
USER_COLUMN = "mn8_user"
FROM_COLUMN = "mn8_from"
TO_COLUMN = "mn8_to"
DESCRIPTION_COLUMN = "mn8_desc"
WELCOME_TEXT_COLUMN = "welcome_text"
DEFAULT_LOG_FILE = "mn8_manage.log"
DEFAULT_BRANA_COMMAND = "mosquitto_pub -h linksys -t rb/ctrl/dev/Brana -m 1"
DEFAULT_PAVLAC_COMMAND = "mosquitto_pub -h linksys -t rb/ctrl/dev/DverePavlac -m 1"
DEFAULT_SIMULATION_MARKER = "simulation"

ACCESS_ACTIONS = {
    "brana": {
        "label": "FRONT DOOR",
        "description": "Front door onto pavement. Press button to open.",
        "command_env": "MN8_CMD_BRANA",
        "default_command": DEFAULT_BRANA_COMMAND,
    },
    "pavlac": {
        "label": "BALCONY ACCESS",
        "description": "Balcony door. Press the button. Pull door slightly then push to open.",
        "command_env": "MN8_CMD_PAVLAC",
        "default_command": DEFAULT_PAVLAC_COMMAND,
    },
}


def create_audit_logger():
    logger = logging.getLogger("mn8_manage_audit")
    if logger.handlers:
        return logger

    log_file_path = os.environ.get("MN8_LOG_FILE", DEFAULT_LOG_FILE)
    log_directory = os.path.dirname(log_file_path)
    if log_directory:
        os.makedirs(log_directory, exist_ok=True)

    handler = logging.FileHandler(log_file_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


AUDIT_LOGGER = create_audit_logger()


def validate_identifier(identifier):
    if not IDENTIFIER_RE.fullmatch(identifier):
        raise RuntimeError(f"Invalid SQL identifier: {identifier}")
    return identifier


def build_identifier(path):
    parts = path.split(".")
    for part in parts:
        validate_identifier(part)
    return sql.SQL(".").join(sql.Identifier(part) for part in parts)


def get_table_identifier():
    table_name = os.environ.get("MN8_ACCESS_TABLE", DEFAULT_TABLE_NAME)
    return build_identifier(table_name)


def get_primary_key_identifier():
    primary_key = os.environ.get("MN8_ACCESS_PK_COLUMN", DEFAULT_PRIMARY_KEY_COLUMN)
    validate_identifier(primary_key)
    return sql.Identifier(primary_key)


def get_database_url():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return database_url


def get_current_user():
    user_name = request.headers.get("X-Authenticated-User")
    if not user_name:
        user_name = request.environ.get("REMOTE_USER")
    if not user_name:
        user_name = os.environ.get("MN8_DEV_AUTH_USER")
    if not user_name:
        return None
    return user_name.strip() or None


def require_current_user():
    user_name = get_current_user()
    if not user_name:
        return None, render_error(
            401,
            "Authentication information is missing.",
            "The application expected nginx to forward the authenticated username."
        )
    return user_name, None


def connect_db():
    return psycopg.connect(get_database_url(), row_factory=dict_row)


def run_query(query, params=None, *, fetchone=False, fetchall=False, commit=False):
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, params or ())
            result = None
            if fetchone:
                result = cursor.fetchone()
            elif fetchall:
                result = cursor.fetchall()
            if commit:
                connection.commit()
            return result


def log_crud_action(action, user_name, **details):
    safe_details = {
        key: value for key, value in details.items() if value is not None
    }
    details_text = " ".join(
        f"{key}={safe_details[key]}" for key in sorted(safe_details)
    )
    message = f"action={action} user={user_name or '-'}"
    if details_text:
        message = f"{message} {details_text}"
    AUDIT_LOGGER.info(message)


def list_records_for_user(user_name):
    query = sql.SQL(
        """
        SELECT {pk} AS record_id,
               {user_column} AS user_name,
               {from_column} AS start_at,
               {to_column} AS end_at,
               link,
               {description_column} AS description,
               {welcome_text_column} AS welcome_text
        FROM {table}
        WHERE {user_column} = %s
        ORDER BY {from_column} DESC, {pk} DESC
        """
    ).format(
        pk=get_primary_key_identifier(),
        user_column=sql.Identifier(USER_COLUMN),
        from_column=sql.Identifier(FROM_COLUMN),
        to_column=sql.Identifier(TO_COLUMN),
        description_column=sql.Identifier(DESCRIPTION_COLUMN),
        welcome_text_column=sql.Identifier(WELCOME_TEXT_COLUMN),
        table=get_table_identifier(),
    )
    return run_query(query, (user_name,), fetchall=True)


def get_row_state(start_at, end_at, reference_time=None):
    reference_time = reference_time or now_local()
    if end_at <= start_at:
        return "invalid"
    if start_at <= reference_time < end_at:
        return "active"
    if reference_time < start_at:
        return "future"
    return "past"


def annotate_row_states(rows):
    reference_time = now_local()
    for row in rows:
        row["row_state"] = get_row_state(row["start_at"], row["end_at"], reference_time)
    return rows


def get_record_for_user(record_id, user_name):
    query = sql.SQL(
        """
        SELECT {pk} AS record_id,
               {user_column} AS user_name,
               {from_column} AS start_at,
               {to_column} AS end_at,
               link,
               {description_column} AS description,
               {welcome_text_column} AS welcome_text
        FROM {table}
        WHERE {pk} = %s AND {user_column} = %s
        """
    ).format(
        pk=get_primary_key_identifier(),
        user_column=sql.Identifier(USER_COLUMN),
        from_column=sql.Identifier(FROM_COLUMN),
        to_column=sql.Identifier(TO_COLUMN),
        description_column=sql.Identifier(DESCRIPTION_COLUMN),
        welcome_text_column=sql.Identifier(WELCOME_TEXT_COLUMN),
        table=get_table_identifier(),
    )
    return run_query(query, (record_id, user_name), fetchone=True)


def get_record_by_link(link_value):
    query = sql.SQL(
        """
        SELECT {pk} AS record_id,
               {user_column} AS user_name,
               {from_column} AS start_at,
               {to_column} AS end_at,
               link,
               {description_column} AS description
        FROM {table}
        WHERE link = %s
        """
    ).format(
        pk=get_primary_key_identifier(),
        user_column=sql.Identifier(USER_COLUMN),
        from_column=sql.Identifier(FROM_COLUMN),
        to_column=sql.Identifier(TO_COLUMN),
        description_column=sql.Identifier(DESCRIPTION_COLUMN),
        table=get_table_identifier(),
    )
    return run_query(query, (link_value,), fetchone=True)


def link_exists(link_value):
    query = sql.SQL(
        "SELECT 1 FROM {table} WHERE link = %s LIMIT 1"
    ).format(table=get_table_identifier())
    return run_query(query, (link_value,), fetchone=True) is not None


def generate_unique_link():
    for _ in range(20):
        link_value = "".join(secrets.choice(ALPHANUMERIC) for _ in range(10))
        if not link_exists(link_value):
            return link_value
    raise RuntimeError("Failed to generate a unique link after multiple attempts.")


def insert_record(user_name, start_at, end_at, description, welcome_text):
    query = sql.SQL(
        """
        INSERT INTO {table} ({user_column}, {from_column}, {to_column}, link, {description_column}, {welcome_text_column})
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING {pk} AS record_id, link
        """
    ).format(
        table=get_table_identifier(),
        pk=get_primary_key_identifier(),
        user_column=sql.Identifier(USER_COLUMN),
        from_column=sql.Identifier(FROM_COLUMN),
        to_column=sql.Identifier(TO_COLUMN),
        description_column=sql.Identifier(DESCRIPTION_COLUMN),
        welcome_text_column=sql.Identifier(WELCOME_TEXT_COLUMN),
    )
    return run_query(
        query,
        (user_name, start_at, end_at, generate_unique_link(), description, welcome_text),
        fetchone=True,
        commit=True,
    )


def update_record(record_id, user_name, start_at, end_at, description, welcome_text):
    query = sql.SQL(
        """
        UPDATE {table}
        SET {from_column} = %s,
            {to_column} = %s,
            {description_column} = %s,
            {welcome_text_column} = %s
        WHERE {pk} = %s AND {user_column} = %s
        """
    ).format(
        pk=get_primary_key_identifier(),
        table=get_table_identifier(),
        user_column=sql.Identifier(USER_COLUMN),
        from_column=sql.Identifier(FROM_COLUMN),
        to_column=sql.Identifier(TO_COLUMN),
        description_column=sql.Identifier(DESCRIPTION_COLUMN),
        welcome_text_column=sql.Identifier(WELCOME_TEXT_COLUMN),
    )
    run_query(query, (start_at, end_at, description, welcome_text, record_id, user_name), commit=True)


def delete_record(record_id, user_name):
    query = sql.SQL(
        "DELETE FROM {table} WHERE {pk} = %s AND {user_column} = %s"
    ).format(
        pk=get_primary_key_identifier(),
        table=get_table_identifier(),
        user_column=sql.Identifier(USER_COLUMN),
    )
    run_query(query, (record_id, user_name), commit=True)


def simulation_enabled():
    marker_path = os.environ.get("MN8_SIMULATION_MARKER", DEFAULT_SIMULATION_MARKER)
    return os.path.exists(marker_path)


def get_access_command(action_name):
    action_config = ACCESS_ACTIONS.get(action_name)
    if action_config is None:
        raise RuntimeError(f"Unknown access action: {action_name}")
    return os.environ.get(action_config["command_env"], action_config["default_command"])


def execute_access_action(action_name):
    command = get_access_command(action_name)
    if simulation_enabled():
        return {"simulation": True, "command": command}

    subprocess.run(shlex.split(command), check=True)
    return {"simulation": False, "command": command}


def now_local():
    return datetime.now().replace(microsecond=0)


def default_end_datetime(start_at):
    return (start_at + timedelta(days=5)).replace(hour=23, minute=59, second=59, microsecond=0)


def format_datetime_for_input(value):
    if not value:
        return ""
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def format_datetime_for_display(value):
    if not value:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def parse_datetime_value(raw_value, label):
    if not raw_value:
        raise ValueError(f"Field {label} is required.")
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError as exc:
        raise ValueError(f"Field {label} must contain a valid date and time.") from exc


def build_form_data(record=None, submitted=None):
    submitted = submitted or {}
    if record is None:
        start_at = now_local()
        end_at = default_end_datetime(start_at)
        return {
            "od": submitted.get("od", format_datetime_for_input(start_at)),
            "do": submitted.get("do", format_datetime_for_input(end_at)),
            "popis": submitted.get("popis", ""),
            "welcome_text": submitted.get("welcome_text", ""),
        }
    return {
        "od": submitted.get("od", format_datetime_for_input(record["start_at"])),
        "do": submitted.get("do", format_datetime_for_input(record["end_at"])),
        "popis": submitted.get("popis", record["description"] or ""),
        "welcome_text": submitted.get("welcome_text", record["welcome_text"] or ""),
    }


def validate_form(submitted):
    errors = []
    warnings = []
    start_at = None
    end_at = None

    try:
        start_at = parse_datetime_value(submitted.get("od", ""), "Od")
    except ValueError as exc:
        errors.append(str(exc))

    try:
        end_at = parse_datetime_value(submitted.get("do", ""), "Do")
    except ValueError as exc:
        errors.append(str(exc))

    if start_at and end_at and end_at <= start_at:
        warnings.append("The end timestamp should be later than the start timestamp.")

    description = submitted.get("popis", "").strip()
    welcome_text = submitted.get("welcome_text", "").strip()

    return errors, warnings, start_at, end_at, description, welcome_text


def get_form_state(form_data, start_at=None, end_at=None):
    if start_at is not None and end_at is not None:
        return get_row_state(start_at, end_at)

    raw_start = form_data.get("od")
    raw_end = form_data.get("do")
    if not raw_start or not raw_end:
        return None

    try:
        parsed_start = datetime.fromisoformat(raw_start)
        parsed_end = datetime.fromisoformat(raw_end)
    except ValueError:
        return None

    return get_row_state(parsed_start, parsed_end)


def build_request_details():
    selected_headers = {
        "Host": request.headers.get("Host"),
        "X-Real-IP": request.headers.get("X-Real-IP"),
        "X-Forwarded-For": request.headers.get("X-Forwarded-For"),
        "X-Forwarded-Proto": request.headers.get("X-Forwarded-Proto"),
        "X-Forwarded-Prefix": request.headers.get("X-Forwarded-Prefix"),
        "X-Original-URI": request.headers.get("X-Original-URI"),
        "X-Authenticated-User": request.headers.get("X-Authenticated-User"),
        "User-Agent": request.headers.get("User-Agent"),
    }

    request_info = {
        "method": request.method,
        "scheme": request.scheme,
        "host": request.host,
        "path": request.path,
        "full_path": request.full_path,
        "url": request.url,
        "base_url": request.base_url,
        "url_root": request.url_root,
        "remote_addr": request.remote_addr,
        "query_string": request.query_string.decode("utf-8", errors="replace"),
        "authenticated_user": get_current_user(),
        "remote_user": request.environ.get("REMOTE_USER"),
    }

    return {
        "request_info": request_info,
        "selected_headers": selected_headers,
        "all_headers": sorted(request.headers.items()),
        "query_args": list(request.args.lists()),
        "environ_info": {
            "REMOTE_USER": request.environ.get("REMOTE_USER"),
            "SCRIPT_NAME": request.environ.get("SCRIPT_NAME"),
            "PATH_INFO": request.environ.get("PATH_INFO"),
            "SERVER_NAME": request.environ.get("SERVER_NAME"),
            "SERVER_PORT": request.environ.get("SERVER_PORT"),
        },
    }


def get_access_record_state(record):
    return get_row_state(record["start_at"], record["end_at"])


def build_access_inactive_detail(record, record_state):
    if record_state == "past":
        return f"This access link expired at {format_datetime_for_display(record['end_at'])}."
    if record_state == "future":
        return f"This access link will become active at {format_datetime_for_display(record['start_at'])}."
    return "This access link is invalid."


def render_access_error(status_code, title, detail, page_state="invalid"):
    return render_template(
        "access_error.html",
        title=title,
        detail=detail,
        status_code=status_code,
        page_state=page_state,
    ), status_code


def render_error(status_code, title, detail):
    return render_template("error.html", title=title, detail=detail, status_code=status_code), status_code


@app.template_filter("datetime_display")
def datetime_display_filter(value):
    return format_datetime_for_display(value)


@app.context_processor
def inject_navigation_context():
    return {
        "current_user_name": get_current_user(),
    }


@app.errorhandler(404)
def not_found(_error):
    return render_error(404, "Page Not Found", "The requested page does not exist.")


@app.errorhandler(RuntimeError)
def runtime_error(error):
    return render_error(500, "Configuration Error", str(error))


@app.route("/")
def root():
    return redirect(url_for("manage_index"))


@app.route("/mn8/manage/")
def manage_index():
    user_name, error_response = require_current_user()
    if error_response:
        return error_response

    rows = annotate_row_states(list_records_for_user(user_name))
    log_crud_action(
        "read",
        user_name,
        count=len(rows),
        path=request.path,
        remote_addr=request.remote_addr,
    )
    return render_template(
        "manage_list.html",
        rows=rows,
        message=request.args.get("message")
    )


@app.route("/mn8/manage/show_info")
def show_info():
    return render_template("show_info.html", **build_request_details())


@app.route("/mn8/access/<token>", methods=["GET", "POST"])
def public_access(token):
    record = get_record_by_link(token)
    if record is None:
        log_crud_action("access-denied", None, reason="token-not-found", token=token, remote_addr=request.remote_addr)
        return render_access_error(404, "error", "Access link was not found.", "invalid")

    record_state = get_access_record_state(record)
    if record_state != "active":
        log_crud_action(
            "access-denied",
            record["user_name"],
            reason=f"inactive-{record_state}",
            token=token,
            record_id=record["record_id"],
            remote_addr=request.remote_addr,
        )
        return render_access_error(
            403,
            "error",
            build_access_inactive_detail(record, record_state),
            record_state,
        )

    action_message = None
    action_kind = None

    if request.method == "POST":
        action_name = request.form.get("open_door", "").strip().lower()
        action_config = ACCESS_ACTIONS.get(action_name)
        if action_config is None:
            action_message = "Unknown action."
            action_kind = "error"
        else:
            try:
                action_result = execute_access_action(action_name)
                if action_result["simulation"]:
                    action_message = f"Simulation: {action_config['label']} was requested."
                else:
                    action_message = f"{action_config['label']} command sent."
                action_kind = "notice"
                log_crud_action(
                    "access-open",
                    record["user_name"],
                    token=token,
                    record_id=record["record_id"],
                    door=action_name,
                    remote_addr=request.remote_addr,
                    simulation=action_result["simulation"],
                )
            except (RuntimeError, subprocess.CalledProcessError) as error:
                action_message = f"Action failed: {error}"
                action_kind = "error"
                log_crud_action(
                    "access-open-failed",
                    record["user_name"],
                    token=token,
                    record_id=record["record_id"],
                    door=action_name,
                    remote_addr=request.remote_addr,
                )
    else:
        log_crud_action(
            "access-view",
            record["user_name"],
            token=token,
            record_id=record["record_id"],
            remote_addr=request.remote_addr,
        )

    return render_template(
        "access_link.html",
        record=record,
        actions=ACCESS_ACTIONS,
        action_message=action_message,
        action_kind=action_kind,
        page_state="active",
    )


@app.route("/mn8/manage/create", methods=["GET", "POST"])
def create_record_view():
    user_name, error_response = require_current_user()
    if error_response:
        return error_response

    form_data = build_form_data(submitted=request.form)
    errors = []
    warnings = []
    form_state = get_form_state(form_data)

    if request.method == "POST":
        errors, warnings, start_at, end_at, description, welcome_text = validate_form(request.form)
        form_state = get_form_state(form_data, start_at, end_at)
        if not errors:
            created_record = insert_record(user_name, start_at, end_at, description, welcome_text)
            log_crud_action(
                "create",
                user_name,
                record_id=created_record["record_id"],
                link=created_record["link"],
                start_at=start_at.isoformat(sep=" "),
                end_at=end_at.isoformat(sep=" "),
            )
            return redirect(url_for("manage_index", message="Access entry created."))

    return render_template(
        "manage_form.html",
        page_title="Create New Access",
        submit_label="Create",
        form_action=url_for("create_record_view"),
        form_data=form_data,
        form_state=form_state,
        errors=errors,
        warnings=warnings,
        link_value=None,
        cancel_url=url_for("manage_index")
    )


@app.route("/mn8/manage/<int:record_id>/edit", methods=["GET", "POST"])
def edit_record_view(record_id):
    user_name, error_response = require_current_user()
    if error_response:
        return error_response

    record = get_record_for_user(record_id, user_name)
    if record is None:
        return render_error(404, "Access Record Not Found", "The selected record does not exist or does not belong to the current user.")

    form_data = build_form_data(record=record, submitted=request.form)
    errors = []
    warnings = []
    form_state = get_form_state(form_data)

    if request.method == "POST":
        errors, warnings, start_at, end_at, description, welcome_text = validate_form(request.form)
        form_state = get_form_state(form_data, start_at, end_at)
        if not errors:
            update_record(record_id, user_name, start_at, end_at, description, welcome_text)
            log_crud_action(
                "update",
                user_name,
                record_id=record_id,
                link=record["link"],
                start_at=start_at.isoformat(sep=" "),
                end_at=end_at.isoformat(sep=" "),
            )
            return redirect(url_for("manage_index", message="Access entry updated."))

    return render_template(
        "manage_form.html",
        page_title="Edit Access",
        submit_label="Save Changes",
        form_action=url_for("edit_record_view", record_id=record_id),
        form_data=form_data,
        form_state=form_state,
        errors=errors,
        warnings=warnings,
        link_value=record["link"],
        cancel_url=url_for("manage_index")
    )


@app.route("/mn8/manage/<int:record_id>/delete", methods=["GET", "POST"])
def delete_record_view(record_id):
    user_name, error_response = require_current_user()
    if error_response:
        return error_response

    record = get_record_for_user(record_id, user_name)
    if record is None:
        return render_error(404, "Access Record Not Found", "The selected record does not exist or does not belong to the current user.")

    if request.method == "POST":
        delete_record(record_id, user_name)
        log_crud_action(
            "delete",
            user_name,
            record_id=record_id,
            link=record["link"],
            start_at=record["start_at"].isoformat(sep=" "),
            end_at=record["end_at"].isoformat(sep=" "),
        )
        return redirect(url_for("manage_index", message="Access entry deleted."))

    return render_template(
        "delete_confirm.html",
        record=record,
        confirm_url=url_for("delete_record_view", record_id=record_id),
        cancel_url=url_for("manage_index")
    )


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_RUN_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_RUN_PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
    )