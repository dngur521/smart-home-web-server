# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Flask-based smart home backend server designed to run on a **Raspberry Pi 5**. It consolidates what was previously a Node.js server and a separate Python hardware script into a single `app.py`. It serves a pre-built React app from `dist/` and exposes REST APIs for hardware control, sensor data, user auth, and system monitoring.

A **Wemos D1 (ESP8266)** module running `Sensor/Sensor.ino` connects to the local WiFi and serves PMS7003 dust sensor data at `http://<IP>/dust`. The Raspberry Pi backend polls this endpoint every 5 minutes.

## Running the Server

```bash
# pm2로 관리 (권장)
pm2 start venv/bin/python3 --name backend -- app.py
pm2 start /usr/local/bin/mjpg_streamer --name cctv -- \
  -i "/usr/local/lib/mjpg-streamer/input_uvc.so -d /dev/video0 -r 1280x960 -f 30" \
  -o "/usr/local/lib/mjpg-streamer/output_http.so -p 8080 -w /usr/local/share/mjpg-streamer/www"
pm2 start /usr/local/bin/ttyd --name ttyd -- --port 7681 --writable --base-path /console-ws bash

# 직접 실행 (포그라운드)
python3 app.py
```

The server listens on `http://0.0.0.0:5000`.

**Prerequisites before starting:**
- MariaDB running at `127.0.0.1:3306`, database `smart_home`, user `master`/`1234`
- Redis running at `localhost:6379`
- `SECRET_KEY` environment variable set (falls back to an insecure default)
- React frontend built to `dist/` (the server serves `dist/index.html` for all non-API routes)
- `DUST_SENSOR_URL` environment variable set to `http://<Wemos D1 IP>/dust`

On startup, `app.py` auto-creates the `history`, `sensor_data`, `users`, `dust_data`, `aircon_schedule` tables if they don't exist.

## Architecture

The entire backend is a **single file**: `app.py`. There are no modules or packages.

### Key Sections in app.py

| Lines | Purpose |
|-------|---------|
| 1–55 | Config constants and global `app`, `db_pool`, `redis_client`, `DUST_SENSOR_URL`, `CCTV_CONFIG_FILE`, `CCTV_SUPPORTED_OPTIONS` |
| 56–180 | Auth helpers: `get_db_connection()`, `login_required` decorator, token helpers, cookie helpers |
| 182–280 | Hardware: `send_command_to_arduino()`, `_schedule_next_5min()`, `read_and_save_dht_data_task()`, `read_and_save_dust_data_task()` |
| 280–600 | Arduino/sensor API endpoints |
| 600~ | React SPA catch-all, user auth/profile endpoints, system stats, CCTV config API |

### Background Tasks

두 백그라운드 스레드 태스크가 5분 정각(:00, :05, :10, …)마다 실행된다.

- `read_and_save_dht_data_task()` — Raspberry Pi GPIO 26의 DHT22 센서 읽어 `sensor_data` 저장
- `read_and_save_dust_data_task()` — Wemos D1의 `/dust` 엔드포인트 GET 후 `dust_data` 저장
- `_schedule_next_5min(fn)` — 다음 5분 정각까지 남은 시간을 계산해 `threading.Timer`로 예약하는 공통 헬퍼

APScheduler `BackgroundScheduler`가 **매분 정각**(cron `second=0`)에 실행된다.

- `_check_aircon_schedules()` — `aircon_schedule`에서 `status='pending' AND scheduled_at <= NOW()` 항목을 조회해 Arduino로 명령 전송 후 `status='done'` 업데이트

### Authentication Flow

- **Access token**: HS256 JWT, 30-minute expiry, HttpOnly cookie (`access_token_cookie`)
- **Refresh token**: UUID stored in Redis with key `refresh:<uuid>`, 7-day TTL; rotation on every use
- **`login_required` decorator**: reads token from `Authorization: Bearer` header or `access_token_cookie` cookie

### Hardware Dependencies (Raspberry Pi 5 specific)

- **DHT22 sensor**: GPIO pin 26 (물리 핀 37), `adafruit-circuitpython-dht` 라이브러리, `use_pulseio=False` 필수
  - lgpio는 pip 설치 불가 → `sudo apt install python3-lgpio` 후 venv에 심볼릭 링크
- **Arduino**: serial on `/dev/ttyUSB0` at 9600 baud
- **Wemos D1 (ESP8266)**: WiFi HTTP server, polls `/dust` for PMS7003 data (`DUST_SENSOR_URL`)
- **CCTV (Logitech C270)**: `/dev/video0`, mjpg_streamer MJPG 1280x960 30fps
- **CPU temp**: `vcgencmd measure_temp` subprocess call
- **NVMe temp**: `sudo smartctl -A /dev/nvme0` (requires passwordless sudo for `smartctl`, `/etc/sudoers.d/smartctl`)

### Database Schema

| Table | Columns |
|-------|---------|
| `users` | `id`, `username` (unique), `password_hash`, `is_active` (BOOL, default FALSE), `created_at` |
| `sensor_data` | `id`, `temperature`, `humidity`, `timestamp` |
| `history` | `id`, `command`, `response`, `timestamp` |
| `dust_data` | `id`, `pm1_0`, `pm2_5`, `pm10`, `timestamp` |
| `aircon_schedule` | `id`, `action` ENUM(on/off), `scheduled_at`, `temperature` INT, `mode` ENUM(cool/dry), `wind` ENUM(auto/low/mid/high), `status` ENUM(pending/done/cancelled), `created_at` |

New users are created with `is_active = FALSE`; an admin must activate accounts manually in the DB.

모든 timestamp는 DB에 로컬 시간(KST)으로 저장되며, API 응답 시 `+09:00` suffix를 붙여 반환한다 (`format_rows_datetime()`).

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/auth/register` | No | Register user |
| POST | `/api/auth/login` | No | Login (HttpOnly cookie 발급) |
| POST | `/api/auth/refresh` | No | Rotate refresh token |
| POST | `/api/auth/logout` | No | Invalidate refresh token in Redis |
| GET | `/api/user/profile` | Yes | Get current user info |
| PUT | `/api/user/update-password` | Yes | Change password |
| DELETE | `/api/user/delete` | Yes | Delete own account |
| POST | `/api/arduino/send-command` | Yes | Send command to Arduino, logs to `history` |
| GET | `/api/arduino/dht-sensor` | Yes | Live DHT22 reading |
| GET | `/api/arduino/dht-history` | Yes | Paginated `sensor_data` (`?page=&limit=`) |
| GET | `/api/arduino/dht-history/today` | Yes | 오늘 온습도 전체 (ASC) |
| GET | `/api/arduino/dht-history/seek` | Yes | timestamp 기준 페이지 번호 반환 |
| GET | `/api/arduino/aircon-history` | Yes | Paginated `history` (`?page=&limit=`) |
| GET | `/api/arduino/aircon-history/seek` | Yes | timestamp 기준 페이지 번호 반환 |
| GET | `/api/arduino/dust-sensor` | Yes | 최신 미세먼지 1건 조회 |
| GET | `/api/arduino/dust-history` | Yes | Paginated `dust_data` (`?page=&limit=`) / 범위 조회 (`?from=&to=`, ISO 8601 UTC) |
| GET | `/api/arduino/dust-history/today` | Yes | 오늘 미세먼지 전체 (ASC) |
| GET | `/api/arduino/environment-history` | Yes | 온습도+미세먼지 5분 버킷 JOIN (`?page=&limit=`) |
| GET | `/api/system/stats` | Yes | CPU/RAM/disk/network stats |
| GET | `/api/system/cctv/config` | Yes | 현재 CCTV 해상도/FPS 및 지원 옵션 조회 |
| POST | `/api/system/cctv/config` | Yes | CCTV 해상도/FPS 변경 (`{"resolution":"1280x960","fps":30}`) — mjpg_streamer pm2 재시작, `cctv_config.json` 저장 |
| POST | `/api/schedule/aircon` | Yes | 에어컨 예약 등록 (`action`, `scheduled_at`, `temperature`, `mode`, `wind`) |
| GET | `/api/schedule/aircon` | Yes | 에어컨 예약 목록 전체 조회 (scheduled_at ASC) |
| DELETE | `/api/schedule/aircon/:id` | Yes | 특정 예약 취소 (pending → cancelled) |
| DELETE | `/api/schedule/aircon/bulk` | Yes | 예약 일괄 삭제 (`status`, `older_than_days` 필터, pending 제외) |

## Sensor.ino (Wemos D1 / ESP8266)

위치: `Sensor/Sensor.ino`

- PMS7003 미세먼지 센서: SoftwareSerial TX→D7(GPIO13), RX→D6(GPIO12)
- `/dust` 엔드포인트로 `pm1_0`, `pm2_5`, `pm10` 반환
- 라즈베리파이 백엔드가 5분마다 이 엔드포인트를 폴링해서 DB에 저장

## Python Dependencies

```
flask flask-cors pyserial adafruit-circuitpython-dht mysql-connector-python bcrypt PyJWT redis psutil requests apscheduler
```

### chatbot.py (AI 챗봇 서버, 포트 5001)

ollama `qwen2.5:1.5b` 기반 스마트홈 AI 어시스턴트. **Python-first 아키텍처** — 데이터 조회는 Python이 직접 처리하고, LLM은 추론/대화만 담당.

```
pip install ollama
```

**Fast path 구조** (LLM 미호출):

| Path | 트리거 | 동작 |
|------|--------|------|
| A | 시간 표현 + 센서 키워드 | DB 직접 조회 후 템플릿 응답 |
| B | 현재 온도/습도/미세먼지 | 최신 레코드 조회 |
| C | 에어컨 제어 키워드 | Python 파서 → 즉시 시리얼 전송 |
| D/D2 | 환기/창문 / 에어컨 켜야 할까 | PM2.5/온도 규칙 기반 답변 |
| E | 인사/감사 대화 | 고정 응답 |
| F | 시스템 상태 키워드 | system stats 직접 조회 |
| G | 에어컨 몇 번 켰어? | COUNT(*) 쿼리 |
| H | 에어컨 예약 (N시간 후/N시에) | `aircon_schedule` DB 직접 조작 |
| I | 에어컨 켜져 있어? | `_is_aircon_on()` 기반 상태 응답 |

**시간 파싱** (`_detect_time_context`, `_parse_schedule_datetime`):
- 지원: `YYYY년MM월DD일`, `YY년도MM월`, `N시간 전/후`, `N분 전/후`, `한/두/세 시간 전/후`, `방금`, `아까`, `어제`, `오늘`, `최근N시간`, `밤/저녁/오전 N시`, `내일 N시`

`lgpio`는 pip으로 설치 불가 (Pi 5). `sudo apt install python3-lgpio` 후 venv에 심볼릭 링크:
```bash
ln -s /usr/lib/python3/dist-packages/lgpio.py venv/lib/python3.13/site-packages/
ln -s /usr/lib/python3/dist-packages/_lgpio.cpython-313-aarch64-linux-gnu.so venv/lib/python3.13/site-packages/
```

## monitor.sh

Standalone shell script (not part of the Flask server) that prints a formatted system stats summary to the terminal using `vcgencmd`, `smartctl`, `free`, `iostat`, and `sar`. Requires `sysstat`, `bc`, and `smartmontools` packages.
