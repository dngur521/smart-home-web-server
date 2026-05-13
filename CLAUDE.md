# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Flask-based smart home backend server designed to run on a **Raspberry Pi**. It consolidates what was previously a Node.js server and a separate Python hardware script into a single `app.py`. It serves a pre-built React app from `dist/` and exposes REST APIs for hardware control, sensor data, user auth, and system monitoring.

## Running the Server

```bash
# Start in background with timestamped log file under ./log/
bash start_server.sh

# Or run directly (foreground)
python3 app.py
```

The server listens on `http://0.0.0.0:5000`.

**Prerequisites before starting:**
- MySQL running at `127.0.0.1:3306`, database `smart_home`, user `master`/`1234`
- Redis running at `localhost:6379`
- `SECRET_KEY` environment variable set (falls back to an insecure default)
- React frontend built to `dist/` (the server serves `dist/index.html` for all non-API routes)

On startup, `app.py` auto-creates the `history`, `sensor_data`, and `users` tables if they don't exist.

## Architecture

The entire backend is a **single file**: `app.py`. There are no modules or packages.

### Key Sections in app.py

| Lines | Purpose |
|-------|---------|
| 1–53 | Config constants and global `app`, `db_pool`, `redis_client` |
| 56–113 | Auth helpers: `get_db_connection()`, `login_required` decorator, `create_access_token()`, `create_refresh_token()` |
| 116–163 | Hardware: `send_command_to_arduino()` (serial), `read_and_save_dht_data_task()` (background timer, runs every 5 min) |
| 168–310 | Arduino/sensor API endpoints |
| 313–329 | React SPA catch-all static file serving from `dist/` |
| 333–547 | User auth/profile API endpoints |
| 549–646 | System stats: `get_ssd_temp()` + `/api/system/stats` endpoint |
| 649–716 | `__main__` startup: DB pool init, table creation, Redis connect, sensor thread, Flask run |

### Authentication Flow

- **Access token**: HS256 JWT, 30-minute expiry, stored by client
- **Refresh token**: UUID stored in Redis with key `refresh:<uuid>`, 7-day TTY; rotation on every use (old token deleted, new one issued)
- **`login_required` decorator**: reads token from `Authorization: Bearer` header or `access_token_cookie` cookie

### Hardware Dependencies (Raspberry Pi specific)

- **DHT22 sensor**: GPIO pin 26, read via `Adafruit_DHT` library
- **Arduino**: serial on `/dev/ttyUSB0` at 9600 baud
- **CPU temp**: `vcgencmd measure_temp` subprocess call
- **SSD temp**: `sudo smartctl -A /dev/sda` subprocess call (requires passwordless sudo for `smartctl`)

### Database Schema

| Table | Columns |
|-------|---------|
| `users` | `id`, `username` (unique), `password_hash`, `is_active` (BOOL, default FALSE), `created_at` |
| `sensor_data` | `id`, `temperature`, `humidity`, `timestamp` |
| `history` | `id`, `command`, `response`, `timestamp` |

New users are created with `is_active = FALSE`; an admin must activate accounts manually in the DB.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/auth/register` | No | Register user |
| POST | `/api/auth/login` | No | Login, returns access+refresh tokens |
| POST | `/api/auth/refresh` | No | Rotate refresh token |
| POST | `/api/auth/logout` | No | Invalidate refresh token in Redis |
| GET | `/api/user/profile` | Yes | Get current user info |
| PUT | `/api/user/update-password` | Yes | Change password |
| DELETE | `/api/user/delete` | Yes | Delete own account |
| POST | `/api/arduino/send-command` | Yes | Send command to Arduino, logs to `history` |
| GET | `/api/arduino/dht-sensor` | Yes | Live DHT22 reading |
| GET | `/api/arduino/dht-history` | Yes | Paginated `sensor_data` (`?page=&limit=`) |
| GET | `/api/arduino/aircon-history` | Yes | Paginated `history` (`?page=&limit=`) |
| GET | `/api/system/stats` | Yes | CPU/RAM/disk/network stats |

## Python Dependencies

```
flask flask-cors pyserial Adafruit_DHT mysql-connector-python bcrypt PyJWT redis psutil
```

## monitor.sh

Standalone shell script (not part of the Flask server) that prints a formatted system stats summary to the terminal using `vcgencmd`, `smartctl`, `free`, `iostat`, and `sar`. Requires `sysstat`, `bc`, and `smartmontools` packages.
