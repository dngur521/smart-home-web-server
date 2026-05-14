# -*- coding: utf-8 -*-
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import Adafruit_DHT
import bcrypt  # 비밀번호 해싱/검증
import jwt  # JWT (JSON Web Token) 처리
import mysql.connector
import psutil
import redis  # Redis 라이브러리
import requests
import serial
from flask import Flask, jsonify, make_response, request, send_from_directory
from flask_cors import CORS

# --- 설정: JWT 및 보안 설정 ---
SECRET_KEY = os.environ.get(
    "SECRET_KEY", "your_super_secret_key_change_me"
)  # 실제 환경에서는 환경 변수를 사용해야 합니다.
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = (
    7  # 여기서는 refresh token을 구현하지 않지만, 개념적으로 추가
)

# --- 설정: Redis 설정 ---
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 0

# --- 설정: Node.js (dbPool.js) 기준 DB 설정 ---
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": "3306",
    "user": "master",
    "password": "1234",
    "database": "smart_home",
}

# --- 설정: Python (server.py) 기준 하드웨어 설정 ---
SENSOR_TYPE = Adafruit_DHT.DHT22
SENSOR_PIN = 26
SERIAL_PORT = "/dev/ttyUSB0"
BAUD_RATE = 9600
SERVER_PORT = 5000  # Node.js 서버 포트 기준
DUST_SENSOR_URL = os.environ.get(
    "DUST_SENSOR_URL", "http://192.168.0.38/dust"
)  # Wemos D1 IP로 변경

# --- 설정: 쿠키 및 CORS ---
# 개발 시 React 개발 서버 주소로 설정 (예: http://localhost:5173)
# 프로덕션에서는 Flask가 프론트를 직접 서빙하므로 CORS 불필요하나 일관성을 위해 유지
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173")
# HTTPS 환경이면 환경변수 COOKIE_SECURE=true 로 설정
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

# --- 전역 변수 ---
app = Flask(__name__, static_folder="dist")
CORS(
    app,
    resources={r"/api/*": {"origins": FRONTEND_ORIGIN, "supports_credentials": True}},
)
db_pool = None
redis_client = None


# --- 6. 인증/인가 헬퍼 함수 및 데코레이터 ---
def format_rows_datetime(rows):
    """DB에서 가져온 row의 datetime 필드에 KST(+09:00) 정보를 붙여 반환"""
    result = []
    for row in rows:
        formatted = {}
        for k, v in row.items():
            if isinstance(v, datetime):
                formatted[k] = v.strftime("%Y-%m-%dT%H:%M:%S+09:00")
            else:
                formatted[k] = v
        result.append(formatted)
    return result


def get_db_connection():
    """DB 풀에서 커넥션을 가져오고, 연결 실패 시 500 에러를 발생시킵니다."""
    try:
        return db_pool.get_connection()
    except mysql.connector.Error as e:
        print(f"DB connection error: {e}", file=sys.stderr)
        return None


def login_required(f):
    """JWT 토큰을 검증하고 사용자 ID를 요청 객체에 저장하는 데코레이터"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = None
        # Authorization: Bearer <token> 헤더에서 토큰 추출
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

        # 헤더에 없으면 쿠키에서 확인 (Nginx auth_request 및 <img> 태그용)
        if not token:
            token = request.cookies.get("access_token_cookie")

        if not token:
            return jsonify({"message": "Token is missing from header or cookie."}), 401

        try:
            # 토큰 디코딩 및 검증
            payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
            request.user_id = payload.get("user_id")  # 사용자 ID를 요청 객체에 저장
        except jwt.ExpiredSignatureError:
            return jsonify({"message": "Token has expired."}), 401
        except jwt.InvalidTokenError:
            return jsonify({"message": "Invalid token."}), 401

        return f(*args, **kwargs)

    return decorated_function


# JWT 생성 함수
def create_access_token(user_id: int):
    """Access Token 생성 (30분 만료)"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {"user_id": user_id, "exp": expire}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: int):
    """Refresh Token 생성 및 Redis에 저장 (7일 만료)"""
    # UUID를 사용하여 고유한 Refresh Token 값 생성
    import uuid

    token_value = str(uuid.uuid4())

    # Refresh Token 만료 시간
    expires_in_seconds = int(timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS).total_seconds())

    # Redis에 토큰 값(Key)과 사용자 ID(Value)를 저장하고 만료 시간 설정
    redis_key = f"refresh:{token_value}"
    redis_client.set(redis_key, user_id, ex=expires_in_seconds)

    return token_value


def set_token_cookies(response, access_token, refresh_token):
    """응답 객체에 access/refresh 토큰을 HttpOnly 쿠키로 설정"""
    response.set_cookie(
        "access_token_cookie",
        access_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="Strict",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        "refresh_token_cookie",
        refresh_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="Strict",
        max_age=int(timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS).total_seconds()),
    )
    return response


def clear_token_cookies(response):
    """응답 객체에서 토큰 쿠키 삭제"""
    response.delete_cookie("access_token_cookie")
    response.delete_cookie("refresh_token_cookie")
    return response


# --- 1. 하드웨어 제어 함수 ---
def send_command_to_arduino(command):
    """아두이노로 명령을 전송하고 응답을 받습니다."""
    ser = None
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        time.sleep(2)
        ser.write(f"{command}\n".encode("utf-8"))
        response = ser.readline().decode("utf-8").strip()
        print(f"Arduino command: {command}, response: {response}")
        return {
            "status": "success",
            "message": "Command sent to Arduino.",
            "arduinoResponse": response,
        }
    except serial.SerialException as e:
        print(f"Serial Error: {e}", file=sys.stderr)
        return {"status": "error", "message": f"Error communicating with Arduino: {e}"}
    finally:
        if ser and ser.is_open:
            ser.close()


# --- 2. 백그라운드 센서 데이터 저장 ---
def read_and_save_dht_data_task():
    """5분마다 온습도 센서 데이터를 읽고 원격 DB에 저장하는 백그라운드 작업"""
    global db_pool
    try:
        humidity, temperature = Adafruit_DHT.read_retry(SENSOR_TYPE, SENSOR_PIN)
        if humidity is not None and temperature is not None:
            temperature = round(temperature, 1)
            humidity = round(humidity, 1)

            # DB 풀에서 커넥션 가져오기
            conn = db_pool.get_connection()
            cursor = conn.cursor()
            query = "INSERT INTO sensor_data (temperature, humidity, timestamp) VALUES (%s, %s, NOW())"
            cursor.execute(query, (temperature, humidity))
            conn.commit()
            print(
                f"Background sensor data saved: Temp={temperature}°C, Humidity={humidity}%"
            )
        else:
            print("Background sensor read failed. Retrying...")
    except mysql.connector.Error as e:
        print(f"Background DB error: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Background sensor read error: {e}", file=sys.stderr)
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()  # 커넥션을 풀에 반환

    _schedule_next_5min(read_and_save_dht_data_task)


def _schedule_next_5min(task_fn):
    """다음 5분 정각(:00, :05, :10, ...)까지 남은 시간을 계산해 task_fn을 예약"""
    now = datetime.now()
    next_minute = (now.minute // 5 + 1) * 5
    if next_minute >= 60:
        next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        next_run = now.replace(minute=next_minute, second=0, microsecond=0)
    delay = (next_run - datetime.now()).total_seconds()
    t = threading.Timer(delay, task_fn)
    t.daemon = True
    t.start()


def read_and_save_dust_data_task():
    """5분 정각마다 Wemos D1의 /dust 엔드포인트를 GET해서 dust_data 테이블에 저장"""
    global db_pool
    try:
        resp = requests.get(DUST_SENSOR_URL, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "success":
            print(f"Dust sensor returned error: {data}", file=sys.stderr)
        else:
            conn = db_pool.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO dust_data (pm1_0, pm2_5, pm10, timestamp) VALUES (%s, %s, %s, NOW())",
                (data["pm1_0"], data["pm2_5"], data["pm10"]),
            )
            conn.commit()
            print(
                f"Dust data saved: PM1.0={data['pm1_0']} PM2.5={data['pm2_5']} PM10={data['pm10']}"
            )
    except requests.RequestException as e:
        print(f"Dust sensor fetch error: {e}", file=sys.stderr)
    except mysql.connector.Error as e:
        print(f"DB error saving dust data: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Unexpected error in dust task: {e}", file=sys.stderr)
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()

    _schedule_next_5min(read_and_save_dust_data_task)


# --- 3. API 엔드포인트 정의 ---
@app.route("/api/arduino/send-command", methods=["POST"])
@login_required
def handle_send_command():
    """React 앱에서 명령을 받아 아두이노로 전송하고 DB에 기록"""
    global db_pool
    data = request.get_json()
    command_to_send = data.get("command")

    if not command_to_send:
        return jsonify(
            {"status": "error", "message": "The 'command' field is required."}
        ), 400

    print(f"Received command from Client: {command_to_send}")

    # 1. 아두이노로 명령 전송
    result = send_command_to_arduino(command_to_send)

    # 2. 원격 DB에 히스토리 삽입
    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        query = (
            "INSERT INTO history (command, response, timestamp) VALUES (%s, %s, NOW())"
        )
        cursor.execute(
            query, (command_to_send, result.get("arduinoResponse", "No response"))
        )
        conn.commit()
    except mysql.connector.Error as e:
        print(f"Database error on /send-command: {e}", file=sys.stderr)
        # 아두이노 제어는 성공했을 수 있으므로, DB 에러를 포함하여 응답
        result["db_status"] = "error"
        result["db_message"] = str(e)
        return jsonify(result), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    # 3. 결과를 JSON 형식으로 반환
    return jsonify(result)


@app.route("/api/arduino/dht-sensor", methods=["GET"])
@login_required
def handle_get_sensor_data():
    """실시간 온습도 데이터를 센서에서 직접 읽어 반환"""
    try:
        humidity, temperature = Adafruit_DHT.read_retry(SENSOR_TYPE, SENSOR_PIN)
        if humidity is not None and temperature is not None:
            temperature = round(temperature, 1)
            humidity = round(humidity, 1)
            return jsonify(
                {"temperature": temperature, "humidity": humidity, "status": "success"}
            )
        else:
            return jsonify(
                {"status": "error", "message": "Failed to retrieve data from sensor."}
            ), 500
    except Exception as e:
        print(f"Sensor read error on /dht-sensor: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": f"Sensor read error: {e}"}), 500


@app.route("/api/arduino/dht-history", methods=["GET"])
@login_required
def handle_get_dht_history():
    """원격 DB에서 온습도 히스토리 조회 (페이지네이션)"""
    global db_pool
    try:
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 10))
        offset = (page - 1) * limit

        conn = db_pool.get_connection()
        cursor = conn.cursor(dictionary=True)  # 결과를 dict 형태로 받음

        # 데이터 조회
        query = "SELECT * FROM sensor_data ORDER BY id DESC LIMIT %s OFFSET %s"
        cursor.execute(query, (limit, offset))
        rows = format_rows_datetime(cursor.fetchall())

        # 전체 카운트 조회
        total_query = "SELECT COUNT(*) AS count FROM sensor_data"
        cursor.execute(total_query)
        total_count = cursor.fetchone()["count"]

        return jsonify(
            {
                "data": rows,
                "total": total_count,
                "page": page,
                "limit": limit,
                "status": "success",
            }
        ), 200
    except mysql.connector.Error as e:
        print(f"Database error on /dht-history: {e}", file=sys.stderr)
        return jsonify(
            {"status": "error", "message": "Failed to fetch sensor data."}
        ), 500
    except Exception as e:
        print(f"Error on /dht-history: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": "Internal server error."}), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


@app.route("/api/arduino/dht-history/today", methods=["GET"])
@login_required
def handle_get_dht_history_today():
    """오늘 날짜(로컬 시간 기준) 온습도 기록 전체 조회 — timestamp ASC"""
    global db_pool
    try:
        today = datetime.now().date()
        conn = db_pool.get_connection()
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT * FROM sensor_data
            WHERE DATE(timestamp) = %s
            ORDER BY timestamp ASC
        """
        cursor.execute(query, (today,))
        rows = format_rows_datetime(cursor.fetchall())
        return jsonify({"status": "success", "data": rows}), 200
    except mysql.connector.Error as e:
        print(f"Database error on /dht-history/today: {e}", file=sys.stderr)
        return jsonify(
            {"status": "error", "message": "Failed to fetch today's sensor data."}
        ), 500
    except Exception as e:
        print(f"Error on /dht-history/today: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": "Internal server error."}), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


@app.route("/api/arduino/aircon-history", methods=["GET"])
@login_required
def handle_get_aircon_history():
    """원격 DB에서 에어컨 제어 히스토리 조회 (페이지네이션)"""
    global db_pool
    try:
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 10))
        offset = (page - 1) * limit

        conn = db_pool.get_connection()
        cursor = conn.cursor(dictionary=True)

        # 데이터 조회
        query = "SELECT * FROM history ORDER BY id DESC LIMIT %s OFFSET %s"
        cursor.execute(query, (limit, offset))
        rows = format_rows_datetime(cursor.fetchall())

        # 전체 카운트 조회
        total_query = "SELECT COUNT(*) AS count FROM history"
        cursor.execute(total_query)
        total_count = cursor.fetchone()["count"]

        return jsonify(
            {
                "data": rows,
                "total": total_count,
                "page": page,
                "limit": limit,
                "status": "success",
            }
        ), 200
    except mysql.connector.Error as e:
        print(f"Database error on /aircon-history: {e}", file=sys.stderr)
        return jsonify(
            {"status": "error", "message": "Failed to fetch aircon history."}
        ), 500
    except Exception as e:
        print(f"Error on /aircon-history: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": "Internal server error."}), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


def _seek_page(table, timestamp_str, limit):
    """주어진 timestamp에 가장 가까운 레코드가 속한 페이지 번호를 반환하는 내부 헬퍼"""
    try:
        # ISO 8601 파싱 (Z → +00:00 변환 후 KST로 변환)
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        ts_kst = ts.astimezone(timezone(timedelta(hours=9))).replace(tzinfo=None)
    except ValueError:
        return None, "Invalid timestamp format. Use ISO 8601."

    try:
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        # DESC 정렬 기준: target_ts보다 최신인 레코드 수 = offset
        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE timestamp > %s", (ts_kst,))
        offset = cursor.fetchone()[0]
        page = (offset // limit) + 1
        return page, None
    except mysql.connector.Error as e:
        return None, str(e)
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


@app.route("/api/arduino/dht-history/seek", methods=["GET"])
@login_required
def handle_dht_history_seek():
    """주어진 timestamp가 속한 온습도 이력 페이지 번호 반환"""
    timestamp_str = request.args.get("timestamp")
    limit = int(request.args.get("limit", 10))

    if not timestamp_str:
        return jsonify({"status": "error", "message": "timestamp is required."}), 400

    page, err = _seek_page("sensor_data", timestamp_str, limit)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify({"status": "success", "page": page}), 200


@app.route("/api/arduino/aircon-history/seek", methods=["GET"])
@login_required
def handle_aircon_history_seek():
    """주어진 timestamp가 속한 에어컨 제어 이력 페이지 번호 반환"""
    timestamp_str = request.args.get("timestamp")
    limit = int(request.args.get("limit", 10))

    if not timestamp_str:
        return jsonify({"status": "error", "message": "timestamp is required."}), 400

    page, err = _seek_page("history", timestamp_str, limit)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify({"status": "success", "page": page}), 200


@app.route("/api/arduino/dust-history", methods=["GET"])
@login_required
def handle_get_dust_history():
    """미세먼지 이력 조회 (페이지네이션)"""
    global db_pool
    try:
        page  = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 10))
        offset = (page - 1) * limit

        conn   = db_pool.get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT * FROM dust_data ORDER BY id DESC LIMIT %s OFFSET %s", (limit, offset))
        rows = format_rows_datetime(cursor.fetchall())

        cursor.execute("SELECT COUNT(*) AS count FROM dust_data")
        total = cursor.fetchone()["count"]

        return jsonify({"status": "success", "data": rows, "total": total, "page": page, "limit": limit}), 200
    except mysql.connector.Error as e:
        print(f"DB error on /dust-history: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": "Failed to fetch dust history."}), 500
    except Exception as e:
        print(f"Error on /dust-history: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": "Internal server error."}), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


@app.route("/api/sensor/dust", methods=["POST"])
def receive_dust_data():
    """ESP8266이 5분마다 미세먼지 데이터를 전송하는 수신 엔드포인트 (인증 없음)"""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    pm1_0 = data.get("pm1_0")
    pm2_5 = data.get("pm2_5")
    pm10 = data.get("pm10")

    if pm1_0 is None or pm2_5 is None or pm10 is None:
        return jsonify(
            {"status": "error", "message": "pm1_0, pm2_5, pm10 are required"}
        ), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify(
            {"status": "error", "message": "Database connection failed."}
        ), 500

    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO dust_data (pm1_0, pm2_5, pm10, timestamp) VALUES (%s, %s, %s, NOW())",
            (int(pm1_0), int(pm2_5), int(pm10)),
        )
        conn.commit()
        print(f"Dust data saved: PM1.0={pm1_0} PM2.5={pm2_5} PM10={pm10}")
        return jsonify({"status": "success"}), 201
    except mysql.connector.Error as e:
        print(f"DB error on /sensor/dust: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": "Database error."}), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if conn:
            conn.close()


@app.route("/api/arduino/dust-sensor", methods=["GET"])
@login_required
def handle_get_dust_sensor():
    """DB에서 최신 미세먼지 데이터 조회"""
    conn = get_db_connection()
    if conn is None:
        return jsonify(
            {"status": "error", "message": "Database connection failed."}
        ), 500
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM dust_data ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            return jsonify({"status": "error", "message": "No dust data yet."}), 503
        return jsonify(
            {"status": "success", "data": format_rows_datetime([row])[0]}
        ), 200
    except mysql.connector.Error as e:
        print(f"DB error on /dust-sensor: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": "Database error."}), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if conn:
            conn.close()


@app.route("/api/arduino/environment-history", methods=["GET"])
@login_required
def handle_environment_history():
    """온습도(sensor_data) + 미세먼지(dust_data)를 5분 버킷 기준 JOIN하여 반환 (페이지네이션)"""
    global db_pool
    try:
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 10))
        offset = (page - 1) * limit

        conn = db_pool.get_connection()
        cursor = conn.cursor(dictionary=True)

        data_query = """
            SELECT
              FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(timestamp) / 300) * 300) AS timestamp,
              MAX(temperature) AS temperature,
              MAX(humidity)    AS humidity,
              MAX(pm1_0)       AS pm1_0,
              MAX(pm2_5)       AS pm2_5,
              MAX(pm10)        AS pm10
            FROM (
              SELECT timestamp, temperature, humidity,
                     NULL AS pm1_0, NULL AS pm2_5, NULL AS pm10
              FROM sensor_data
              UNION ALL
              SELECT timestamp, NULL AS temperature, NULL AS humidity,
                     pm1_0, pm2_5, pm10
              FROM dust_data
            ) combined
            GROUP BY FLOOR(UNIX_TIMESTAMP(timestamp) / 300)
            ORDER BY timestamp DESC
            LIMIT %s OFFSET %s
        """
        cursor.execute(data_query, (limit, offset))
        rows = format_rows_datetime(cursor.fetchall())

        count_query = """
            SELECT COUNT(*) AS count FROM (
              SELECT DISTINCT FLOOR(UNIX_TIMESTAMP(timestamp) / 300) AS bucket
              FROM (
                SELECT timestamp FROM sensor_data
                UNION ALL
                SELECT timestamp FROM dust_data
              ) all_ts
            ) cnt
        """
        cursor.execute(count_query)
        total = cursor.fetchone()["count"]

        return jsonify(
            {
                "status": "success",
                "data": rows,
                "total": total,
                "page": page,
                "limit": limit,
            }
        ), 200
    except mysql.connector.Error as e:
        print(f"DB error on /environment-history: {e}", file=sys.stderr)
        return jsonify(
            {"status": "error", "message": "Failed to fetch environment history."}
        ), 500
    except Exception as e:
        print(f"Error on /environment-history: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": "Internal server error."}), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


# --- 4. React 정적 파일 서빙 ---
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_react_app(path):
    """
    React 앱의 정적 파일 및 클라이언트 사이드 라우팅 처리
    API 경로가 아니면 모두 React 앱으로 연결
    """
    # API 경로는 Flask가 위에서 먼저 처리함

    # 요청된 경로(path)가 'build' 폴더 내에 실제로 존재하는 파일인지 확인
    # (예: /static/js/main.js, /manifest.json)
    if path != "" and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    else:
        # 존재하지 않는 경로(React 라우터가 처리할 /dashboard 등)는
        # 모두 index.html을 반환
        return send_from_directory(app.static_folder, "index.html")


# --- 7. 사용자 인증/인가 API 엔드포인트 ---
@app.route("/api/auth/register", methods=["POST"])
def register():
    """회원가입: 사용자 이름, 비밀번호를 받아 해싱 후 DB에 저장"""
    data = request.get_json()
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify(
            {"status": "error", "message": "Username and password are required."}
        ), 400

    # 비밀번호 해싱 (UTF-8로 인코딩 후 해시)
    hashed_password = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode(
        "utf-8"
    )

    conn = get_db_connection()
    if conn is None:
        return jsonify(
            {"status": "error", "message": "Database connection failed."}
        ), 500

    try:
        cursor = conn.cursor()
        # 사용자 이름 중복 확인
        cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            return jsonify(
                {"status": "error", "message": "Username already exists."}
            ), 409

        # 사용자 정보 삽입
        query = "INSERT INTO users (username, password_hash) VALUES (%s, %s)"
        cursor.execute(query, (username, hashed_password))
        conn.commit()

        return jsonify(
            {"status": "success", "message": "User registered successfully."}
        ), 201
    except mysql.connector.Error as e:
        print(f"DB error on /register: {e}", file=sys.stderr)
        return jsonify(
            {"status": "error", "message": "Database error during registration."}
        ), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


@app.route("/api/auth/login", methods=["POST"])
def login():
    """로그인: 사용자 이름, 비밀번호 검증 후 Access Token 및 Refresh Token 발급"""
    data = request.get_json()
    username = data.get("username")
    password = data.get("password")

    conn = get_db_connection()
    if conn is None:
        return jsonify(
            {"status": "error", "message": "Database connection failed."}
        ), 500

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, password_hash, is_active FROM users WHERE username = %s",
            (username,),
        )
        user = cursor.fetchone()

        if user and bcrypt.checkpw(
            password.encode("utf-8"), user["password_hash"].encode("utf-8")
        ):
            # 💡 계정 활성화 상태 확인 💡
            # print(user.get('is_active'))
            if not user.get("is_active"):
                return jsonify(
                    {
                        "status": "error",
                        "message": "Account is inactive. Please contact the administrator.",
                    }
                ), 401
            # 비밀번호 일치 -> JWT 토큰 및 Refresh Token 생성
            user_id = user["id"]
            access_token = create_access_token(user_id=user_id)
            refresh_token = create_refresh_token(user_id=user_id)

            response = make_response(
                jsonify(
                    {
                        "status": "success",
                        "message": "Login successful.",
                        "expires_in_minutes": ACCESS_TOKEN_EXPIRE_MINUTES,
                    }
                ),
                200,
            )
            return set_token_cookies(response, access_token, refresh_token)
        else:
            return jsonify(
                {"status": "error", "message": "Invalid username or password."}
            ), 401
    except mysql.connector.Error as e:
        print(f"DB error on /login: {e}", file=sys.stderr)
        return jsonify(
            {"status": "error", "message": "Database error during login."}
        ), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


@app.route("/api/auth/refresh", methods=["POST"])
def refresh_token():
    """Refresh Token 쿠키로 새 Access/Refresh Token 발급 (토큰 로테이션)"""
    old_refresh_token = request.cookies.get("refresh_token_cookie")

    if not old_refresh_token:
        return jsonify({"message": "Refresh token is missing."}), 400

    redis_key = f"refresh:{old_refresh_token}"

    # 1. Redis에서 Refresh Token 확인
    user_id_bytes = redis_client.get(redis_key)

    if not user_id_bytes:
        return jsonify({"message": "Invalid or expired refresh token."}), 401

    try:
        user_id = int(user_id_bytes.decode("utf-8"))
    except ValueError:
        return jsonify({"message": "Invalid user ID stored in token."}), 500

    # 2. 토큰 사용 완료 및 무효화 (Redis에서 삭제)
    redis_client.delete(redis_key)

    # 3. 새 Access Token 및 새 Refresh Token 발급 후 쿠키로 반환
    new_access_token = create_access_token(user_id=user_id)
    new_refresh_token = create_refresh_token(user_id=user_id)

    response = make_response(
        jsonify(
            {
                "status": "success",
                "message": "Tokens refreshed successfully.",
                "expires_in_minutes": ACCESS_TOKEN_EXPIRE_MINUTES,
            }
        ),
        200,
    )
    return set_token_cookies(response, new_access_token, new_refresh_token)


@app.route("/api/user/profile", methods=["GET"])
@login_required
def get_user_profile():
    """회원정보 조회: 로그인된 사용자의 정보 반환"""
    user_id = request.user_id  # login_required 데코레이터가 요청 객체에 저장한 ID

    conn = get_db_connection()
    if conn is None:
        return jsonify(
            {"status": "error", "message": "Database connection failed."}
        ), 500

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, username FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()

        if user:
            return jsonify({"status": "success", "user": user}), 200
        else:
            # 이 경우는 토큰이 유효했지만, DB에서 사용자가 삭제된 경우 (매우 드물게 발생)
            return jsonify({"status": "error", "message": "User not found."}), 404
    except mysql.connector.Error as e:
        print(f"DB error on /profile: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": "Database error."}), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


@app.route("/api/user/update-password", methods=["PUT"])
@login_required
def update_password():
    """회원정보 수정 (비밀번호): 로그인된 사용자의 비밀번호 변경"""
    user_id = request.user_id
    data = request.get_json()
    new_password = data.get("new_password")

    if not new_password:
        return jsonify({"status": "error", "message": "New password is required."}), 400

    # 새 비밀번호 해싱
    hashed_password = bcrypt.hashpw(
        new_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    conn = get_db_connection()
    if conn is None:
        return jsonify(
            {"status": "error", "message": "Database connection failed."}
        ), 500

    try:
        cursor = conn.cursor()
        query = "UPDATE users SET password_hash = %s WHERE id = %s"
        cursor.execute(query, (hashed_password, user_id))
        conn.commit()

        if cursor.rowcount == 0:
            return jsonify(
                {"status": "error", "message": "User not found or password unchanged."}
            ), 404

        return jsonify(
            {"status": "success", "message": "Password updated successfully."}
        ), 200
    except mysql.connector.Error as e:
        print(f"DB error on /update-password: {e}", file=sys.stderr)
        return jsonify(
            {"status": "error", "message": "Database error during update."}
        ), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


@app.route("/api/user/delete", methods=["DELETE"])
@login_required
def delete_user():
    """회원탈퇴: 로그인된 사용자 계정 삭제"""
    user_id = request.user_id

    conn = get_db_connection()
    if conn is None:
        return jsonify(
            {"status": "error", "message": "Database connection failed."}
        ), 500

    try:
        cursor = conn.cursor()
        # 사용자 레코드 삭제
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()

        if cursor.rowcount == 0:
            return jsonify({"status": "error", "message": "User not found."}), 404

        # React 클라이언트 측에서 토큰을 삭제하여 로그아웃 처리
        return jsonify(
            {"status": "success", "message": "Account deleted successfully."}
        ), 200
    except mysql.connector.Error as e:
        print(f"DB error on /delete-user: {e}", file=sys.stderr)
        return jsonify(
            {"status": "error", "message": "Database error during deletion."}
        ), 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "conn" in locals() and conn:
            conn.close()


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    """로그아웃: 쿠키의 Refresh Token을 Redis에서 무효화하고 쿠키 삭제"""
    refresh_token = request.cookies.get("refresh_token_cookie")

    if refresh_token:
        redis_key = f"refresh:{refresh_token}"
        redis_client.delete(redis_key)

    response = make_response(
        jsonify(
            {
                "status": "success",
                "message": "Logout successful. Refresh token invalidated.",
            }
        ),
        200,
    )
    return clear_token_cookies(response)


def get_ssd_temp(device_name="sda"):
    """smartctl을 사용해 SSD 온도를 가져옵니다."""
    try:
        # 'sudo smartctl'이 비밀번호 없이 실행 가능해야 합니다.
        # (필요시 'sudo visudo'로 'kam ALL=(ALL) NOPASSWD: /usr/sbin/smartctl' 추가)
        output = subprocess.check_output(
            ["sudo", "smartctl", "-A", f"/dev/{device_name}"], stderr=subprocess.STDOUT
        ).decode("utf-8")

        # 'Temperature:' 라인에서 숫자 추출
        match = re.search(r"Temperature:\s*(\d+)\s*C", output, re.IGNORECASE)
        if match:
            return f"{match.group(1)}C"

        # NVMe 장치를 위한 대체 검색
        match_nvme = re.search(
            r"Temperature Sensor \d+:\s*(\d+)\s*C", output, re.IGNORECASE
        )
        if match_nvme:
            return f"{match_nvme.group(1)}C"

        return "N/A (Extract Fail)"
    except Exception as e:
        print(f"Smartctl error: {e}", file=sys.stderr)
        return "N/A (smartctl error)"


@app.route("/api/system/stats", methods=["GET"])
@login_required
def get_system_stats():
    """실시간 시스템 하드웨어 상태를 반환합니다."""
    try:
        # --- 쉘 스크립트의 로직을 Python으로 구현 ---

        # 1. CPU 온도 (vcgencmd)
        temp_str = subprocess.check_output(["vcgencmd", "measure_temp"]).decode("utf-8")
        cpu_temp = temp_str.split("=")[1].split("'")[0]

        # 2. CPU 사용률 (psutil, 1초간 측정)
        # 이 함수는 1초간 블로킹되며 정확한 사용률을 반환합니다.
        cpu_usage = psutil.cpu_percent(interval=1)

        # 3. 메모리 사용량 (psutil)
        mem = psutil.virtual_memory()
        mem_total_mb = mem.total / (1024 * 1024)
        # 'available'은 'free'보다 더 정확한 실제 사용 가능 메모리입니다.
        mem_avail_mb = mem.available / (1024 * 1024)
        mem_used_mb = mem_total_mb - mem_avail_mb
        mem_percent = mem.percent  # (이미 psutil이 계산해줌)

        # 4. SSD 온도 (smartctl)
        ssd_temp = get_ssd_temp("sda")  # 쉘 스크립트의 "sda" 기준

        # 5. Disk I/O (psutil, 1초간 측정)
        disk_io_start = psutil.disk_io_counters()
        # (위의 cpu_percent(interval=1)이 1초를 소비했으므로,
        # 여기서는 time.sleep(1)을 또 쓸 필요 없이 바로 측정해도 근사치가 나옵니다)
        disk_io_end = psutil.disk_io_counters()

        disk_read_mb = (disk_io_end.read_bytes - disk_io_start.read_bytes) / (
            1024 * 1024
        )
        disk_write_mb = (disk_io_end.write_bytes - disk_io_start.write_bytes) / (
            1024 * 1024
        )

        # 6. Network I/O (psutil, 1초간 측정)
        net_io_start = psutil.net_io_counters()
        # (위와 동일한 이유로 1초 대기 생략)
        net_io_end = psutil.net_io_counters()

        net_down_mb = (net_io_end.bytes_recv - net_io_start.bytes_recv) / (1024 * 1024)
        net_up_mb = (net_io_end.bytes_sent - net_io_start.bytes_sent) / (1024 * 1024)

        # 7. Disk Usage (psutil)
        # 루트('/') 파일시스템의 사용량을 가져옵니다.
        disk_usage = psutil.disk_usage("/")
        disk_total_gb = disk_usage.total / (1024 * 1024 * 1024)
        disk_used_gb = disk_usage.used / (1024 * 1024 * 1024)
        disk_percent = disk_usage.percent

        # --- JSON으로 데이터 반환 ---
        stats = {
            "cpu_temp": cpu_temp,
            "cpu_usage": cpu_usage,
            "ram_used_mb": round(mem_used_mb, 1),
            "ram_total_mb": round(mem_total_mb, 1),
            "ram_percent": mem_percent,
            "ssd_temp": ssd_temp,
            "disk_total_gb": round(disk_total_gb, 1),
            "disk_used_gb": round(disk_used_gb, 1),
            "disk_percent": disk_percent,
            "net_download_mb": round(net_down_mb, 2),
            "net_upload_mb": round(net_up_mb, 2),
            "disk_read_mb": round(disk_read_mb, 2),
            "disk_write_mb": round(disk_write_mb, 2),
        }
        return jsonify({"status": "success", "data": stats}), 200

    except Exception as e:
        print(f"Error in /api/system/stats: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": str(e)}), 500


# --- 5. 서버 실행 (기존 server.py + server.js) ---
if __name__ == "__main__":
    try:
        # DB 커넥션 풀 생성 (Node.js의 connectionLimit: 10과 동일하게 설정)
        db_pool = mysql.connector.pooling.MySQLConnectionPool(
            pool_name="mypool", pool_size=10, **DB_CONFIG
        )
        print("Database connection pool created successfully.")

        # DB 테이블 초기화 (기존 server.py 로직, 단 원격 DB에 실행)
        print("Initializing database tables (IF NOT EXISTS)...")
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        # 'aircon_data' DB는 이미 있다고 가정 (dbPool.js에 명시됨)
        # cursor.execute("CREATE DATABASE IF NOT EXISTS aircon_data")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                command VARCHAR(255) NOT NULL,
                response VARCHAR(255),
                timestamp DATETIME NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sensor_data (
                id INT AUTO_INCREMENT PRIMARY KEY,
                temperature DECIMAL(5,2) NOT NULL,
                humidity DECIMAL(5,2) NOT NULL,
                timestamp DATETIME NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT FALSE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dust_data (
                id INT AUTO_INCREMENT PRIMARY KEY,
                pm1_0 INT NOT NULL,
                pm2_5 INT NOT NULL,
                pm10  INT NOT NULL,
                timestamp DATETIME NOT NULL
            )
        """)
        # --- Redis 클라이언트 생성 ---
        # global redis_client
        redis_client = redis.StrictRedis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=False
        )
        # 테스트용 Redis ping
        redis_client.ping()
        print("Redis client connected successfully.")

        conn.commit()
        cursor.close()
        conn.close()
        print("Database tables initialized successfully.")
    except redis.exceptions.ConnectionError as e:
        print(f"Error connecting to Redis: {e}", file=sys.stderr)
        sys.exit(1)
    except mysql.connector.Error as e:
        print(f"Error initializing database: {e}", file=sys.stderr)
        sys.exit(1)

    # 백그라운드 센서 데이터 수집 — 둘 다 다음 5분 정각에 첫 실행
    _schedule_next_5min(read_and_save_dht_data_task)
    _schedule_next_5min(read_and_save_dust_data_task)
    print("Background sensor threads started (DHT22 + Dust).")

    # Flask 서버 실행 (Node.js 포트 5000번, 모든 IP에서 접근 가능)
    print(f"Starting server on http://0.0.0.0:{SERVER_PORT}")
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False)
