# MN8 Flask App

Server-rendered Flask application for managing user-specific records in PostgreSQL behind nginx Basic Auth.

## Routes

- `/mn8/manage/` shows the authenticated user's records.
- `/mn8/manage/create` opens the create form.
- `/mn8/manage/<record_id>/edit` opens the edit form.
- `/mn8/manage/<record_id>/delete` shows the delete confirmation page.
- `/mn8/manage/show_info` displays request metadata and forwarded headers for diagnostics.
- `/mn8/access/<token>` shows the public access page when the token exists and is currently active.

## Environment variables

- `DATABASE_URL` is required for list, create, edit, and delete operations.
- `MN8_ACCESS_TABLE` defaults to `mn8_brana_access`.
- `MN8_ACCESS_PK_COLUMN` defaults to `id`. The application aliases this column internally, so the physical column name does not need to be `id`.
- `MN8_DEV_AUTH_USER` is optional and only intended for local development when nginx is not forwarding `X-Authenticated-User`.
- `MN8_LOG_FILE` defaults to `mn8_manage.log` and controls where CRUD audit entries are appended.
- `MN8_CMD_BRANA` optionally overrides the command used for opening the front gate.
- `MN8_CMD_PAVLAC` optionally overrides the command used for opening the balcony/pavlač door.
- `MN8_SIMULATION_MARKER` defaults to `simulation`; if that path exists, access actions are logged but not executed.
- `FLASK_RUN_HOST` defaults to `0.0.0.0`.
- `FLASK_RUN_PORT` defaults to `5000`.
- `FLASK_DEBUG` defaults to `1`.

## Run locally

Create a virtual environment, install dependencies, export environment variables, and start the app:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DBNAME'
export MN8_DEV_AUTH_USER='testuser'
export MN8_LOG_FILE='mn8_manage.log'
export MN8_CMD_BRANA="mosquitto_pub -h linksys -t rb/ctrl/dev/Brana -m 1"
export MN8_CMD_PAVLAC="mosquitto_pub -h linksys -t rb/ctrl/dev/DverePavlac -m 1"
python app.py
```

Then open:

```text
http://127.0.0.1:5000/mn8/manage/
```

For header diagnostics use:

```text
http://127.0.0.1:5000/mn8/manage/show_info
```

For the public token page use:

```text
http://127.0.0.1:5000/mn8/access/<token>
```

## Run with Gunicorn

For deployment, run Gunicorn through the same Python interpreter as the virtual environment:

```sh
cd /opt/webapp/mn8_website
. .venv/bin/activate
export DATABASE_URL='postgresql://postgres@linksys:5432/rbdb'
python -m gunicorn -w 1 -b 127.0.0.1:7780 app:app
```

This avoids accidentally using a system `gunicorn` executable with a different Python environment.

## Notes

- The existing PostgreSQL table is assumed to already exist.
- The application filters rows by the authenticated user forwarded from nginx.
- Physical database columns are expected to be `mn8_user`, `mn8_from`, `mn8_to`, `mn8_desc`, and `welcome_text`.
- CRUD actions are appended to the log file configured by `MN8_LOG_FILE`.
- Public access actions are allowed only when the token exists and the current time falls within its active period.
- On platforms such as OpenWrt, prefer plain `psycopg` instead of `psycopg[binary]`, because binary wheels may be unavailable.