# MN8 Flask App

Server-rendered Flask application for managing user-specific records in PostgreSQL behind nginx Basic Auth.

## Routes

- `/mn8/manage/` shows the authenticated user's records.
- `/mn8/manage/create` opens the create form.
- `/mn8/manage/<record_id>/edit` opens the edit form.
- `/mn8/manage/<record_id>/delete` shows the delete confirmation page.
- `/mn8/manage/show_info` displays request metadata and forwarded headers for diagnostics.

## Environment variables

- `DATABASE_URL` is required for list, create, edit, and delete operations.
- `MN8_ACCESS_TABLE` defaults to `mn8_brana_access`.
- `MN8_ACCESS_PK_COLUMN` defaults to `id`. The application aliases this column internally, so the physical column name does not need to be `id`.
- `MN8_DEV_AUTH_USER` is optional and only intended for local development when nginx is not forwarding `X-Authenticated-User`.
- `MN8_LOG_FILE` defaults to `mn8_manage.log` and controls where CRUD audit entries are appended.
- `FLASK_RUN_HOST` defaults to `0.0.0.0`.
- `FLASK_RUN_PORT` defaults to `5000`.
- `FLASK_DEBUG` defaults to `1`.

## Run locally

Create a virtual environment, install dependencies, export environment variables, and start the app:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DBNAME'
export MN8_DEV_AUTH_USER='testuser'
export MN8_LOG_FILE='mn8_manage.log'
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

## Notes

- The existing PostgreSQL table is assumed to already exist.
- The application filters rows by the authenticated user forwarded from nginx.
- Physical database columns are expected to be `mn8_user`, `mn8_from`, `mn8_to`, and `mn8_desc`.
- CRUD actions are appended to the log file configured by `MN8_LOG_FILE`.