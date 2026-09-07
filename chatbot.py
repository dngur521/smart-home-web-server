# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types as genai_types
from google.genai import errors as genai_errors
import mysql.connector
import psutil
from dotenv import load_dotenv

load_dotenv()
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

# --- 설정 ---
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": "3306",
    "user": "master",
    "password": "1234",
    "database": "smart_home",
}
APP_INTERNAL_URL  = "http://localhost:5000"
GEMINI_MODEL      = "gemini-flash-latest"
FALLBACK_MODEL    = "gemini-2.5-flash-lite"
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
_gemini_client    = genai.Client(api_key=GEMINI_API_KEY)
FRONTEND_ORIGIN   = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173")
MJPG_SNAPSHOT_URL = "http://127.0.0.1:8080/?action=snapshot"

app = Flask(__name__)
app.json.ensure_ascii = False
CORS(app, resources={r"/api/chat*": {"origins": FRONTEND_ORIGIN, "supports_credentials": True}})

# --- 에어컨 인덱스 매핑 ---
# Aircon.ino codes[] 배열 기준
# 0: 전원 OFF, 1: 냉방ON(약풍18도), 2: 파워냉방
# 3~15: 냉방 약풍 18~30도, 16~28: 냉방 중풍, 29~41: 냉방 강풍, 42~54: 냉방 자동풍
# 55~67: 제습 약풍, 68~80: 제습 중풍, 81~93: 제습 강풍, 94~106: 제습 자동풍
_COOL_BASE  = {"weak": 3,  "medium": 16, "strong": 29, "auto": 42}
_DEHUM_BASE = {"weak": 55, "medium": 68, "strong": 81, "auto": 94}

def _aircon_index(mode, fan="auto", temp=25):
    if mode == "off":
        return 0
    if mode == "power_cool":
        return 2
    t = max(0, min(12, int(temp) - 18))
    if mode == "cool":
        return _COOL_BASE.get(fan, _COOL_BASE["auto"]) + t
    if mode == "dehumidify":
        return _DEHUM_BASE.get(fan, _DEHUM_BASE["auto"]) + t
    return 1

def _decode_aircon_index(index: int) -> dict:
    """codes[] 인덱스 → mode/fan/temp 역변환"""
    if index == 0:
        return {"mode": "off",        "fan": "auto", "temp": 25}
    if index == 1:
        return {"mode": "cool",       "fan": "weak", "temp": 18}
    if index == 2:
        return {"mode": "power_cool", "fan": "auto", "temp": 25}
    for start, end, mode, fan in [
        (3,  15, "cool",       "weak"),
        (16, 28, "cool",       "medium"),
        (29, 41, "cool",       "strong"),
        (42, 54, "cool",       "auto"),
        (55, 67, "dehumidify", "weak"),
        (68, 80, "dehumidify", "medium"),
        (81, 93, "dehumidify", "strong"),
        (94, 106,"dehumidify", "auto"),
    ]:
        if start <= index <= end:
            return {"mode": mode, "fan": fan, "temp": 18 + (index - start)}
    return {"mode": "cool", "fan": "auto", "temp": 25}

def _get_current_aircon_state() -> dict | None:
    """최근 에어컨 이력에서 현재 모드/풍량/온도 추출. 꺼짐 상태면 None."""
    import re as _re
    rows = _db_query("SELECT command FROM history ORDER BY timestamp DESC LIMIT 1")
    if not rows:
        return None
    m = _re.match(r'SEND\s+(\d+),', rows[0].get("command", ""))
    if not m:
        return None
    state = _decode_aircon_index(int(m.group(1)))
    return None if state["mode"] == "off" else state

# --- DB 헬퍼 ---
def _db_query(query, params=None):
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    cursor.execute(query, params or ())
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    from decimal import Decimal
    result = []
    for row in rows:
        formatted = {}
        for k, v in row.items():
            if isinstance(v, datetime):
                formatted[k] = v.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(v, Decimal):
                formatted[k] = float(v)
            else:
                formatted[k] = v
        result.append(formatted)
    return result

def _db_insert(query, params):
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute(query, params)
    conn.commit()
    cursor.close()
    conn.close()

def _is_aircon_on() -> bool:
    """TENT6000 빛센서로 에어컨 켜짐 여부 판별. app.py 내부 API 경유. 실패 시 이력 기반 fallback."""
    try:
        r = requests.get(f"{APP_INTERNAL_URL}/api/internal/aircon-status", timeout=5)
        if r.status_code == 200:
            return r.json().get("is_on", False)
    except Exception:
        pass
    rows = _db_query("SELECT command FROM history ORDER BY timestamp DESC LIMIT 1")
    if not rows:
        return False
    return rows[0].get("command", "") != "SEND 0,5"

def _send_internal(command: str) -> str:
    """app.py 내부 API를 통해 Arduino 명령 전송. 응답 문자열 반환."""
    r = requests.post(
        f"{APP_INTERNAL_URL}/api/internal/arduino/send",
        json={"command": command},
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("response", "")

# --- 도구 함수들 ---
def tool_get_current_temperature(_args=None):
    rows = _db_query("SELECT temperature, timestamp FROM sensor_data ORDER BY timestamp DESC LIMIT 1")
    if not rows:
        return {"error": "데이터 없음"}
    return {"temperature": rows[0]["temperature"]}

def tool_get_current_humidity(_args=None):
    rows = _db_query("SELECT humidity, timestamp FROM sensor_data ORDER BY timestamp DESC LIMIT 1")
    if not rows:
        return {"error": "데이터 없음"}
    return {"humidity_percent": rows[0]["humidity"]}

def tool_get_current_dust(_args=None):
    rows = _db_query("SELECT pm2_5, pm10, timestamp FROM dust_data ORDER BY timestamp DESC LIMIT 1")
    if not rows:
        return {"error": "데이터 없음"}
    return {"pm2_5": rows[0]["pm2_5"], "pm10": rows[0]["pm10"]}

def _parse_time_arg(s):
    """날짜 문자열을 datetime으로 파싱"""
    if isinstance(s, datetime):
        return s
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"날짜 형식 인식 불가: {s}")

def _summarize_sensor_rows(rows):
    if not rows:
        return {"message": "해당 기간 데이터 없음"}
    temps = [r["temperature"] for r in rows if r.get("temperature") is not None]
    hums  = [r["humidity"]    for r in rows if r.get("humidity") is not None]
    if not temps:
        return {"message": "해당 기간 데이터 없음"}
    return {
        "temp_avg": round(sum(temps) / len(temps), 1),
        "temp_min": min(temps),
        "temp_max": max(temps),
        "humidity_avg": round(sum(hums) / len(hums), 1) if hums else 0,
    }

def _summarize_dust_rows(rows):
    if not rows:
        return {"message": "해당 기간 데이터 없음"}
    pm25 = [r["pm2_5"] for r in rows if r.get("pm2_5") is not None]
    pm10 = [r["pm10"]  for r in rows if r.get("pm10") is not None]
    if not pm25:
        return {"message": "해당 기간 데이터 없음"}
    return {
        "pm2_5_avg": round(sum(pm25) / len(pm25), 1),
        "pm2_5_max": max(pm25),
        "pm10_avg":  round(sum(pm10) / len(pm10), 1),
        "pm10_max":  max(pm10),
    }

def tool_get_sensor_at(sensor_type: str, target_time: datetime) -> str:
    """특정 시각의 온습도/미세먼지 조회"""
    time_desc = target_time.strftime("%m월 %d일 %H시 %M분") if target_time.minute else target_time.strftime("%m월 %d일 %H시")
    if sensor_type == "dust":
        rows = _db_query(
            "SELECT pm2_5, pm10, timestamp FROM dust_data "
            "ORDER BY ABS(TIMESTAMPDIFF(SECOND, timestamp, %s)) LIMIT 1",
            (target_time,),
        )
        if not rows:
            return "해당 시각 근처 미세먼지 데이터가 없습니다."
        return f"{time_desc} 미세먼지는 PM2.5 {rows[0]['pm2_5']}μg/m³, PM10 {rows[0]['pm10']}μg/m³입니다."
    elif sensor_type == "humidity":
        rows = _db_query(
            "SELECT humidity, timestamp FROM sensor_data "
            "ORDER BY ABS(TIMESTAMPDIFF(SECOND, timestamp, %s)) LIMIT 1",
            (target_time,),
        )
        if not rows:
            return "해당 시각 근처 습도 데이터가 없습니다."
        return f"{time_desc} 습도는 {rows[0]['humidity']}%입니다."
    elif sensor_type == "temp_humidity":
        rows = _db_query(
            "SELECT temperature, humidity, timestamp FROM sensor_data "
            "ORDER BY ABS(TIMESTAMPDIFF(SECOND, timestamp, %s)) LIMIT 1",
            (target_time,),
        )
        if not rows:
            return "해당 시각 근처 온습도 데이터가 없습니다."
        return f"{time_desc} 온도는 {rows[0]['temperature']}°C, 습도는 {rows[0]['humidity']}%입니다."
    else:  # temperature
        rows = _db_query(
            "SELECT temperature, timestamp FROM sensor_data "
            "ORDER BY ABS(TIMESTAMPDIFF(SECOND, timestamp, %s)) LIMIT 1",
            (target_time,),
        )
        if not rows:
            return "해당 시각 근처 온도 데이터가 없습니다."
        return f"{time_desc} 온도는 {rows[0]['temperature']}°C입니다."

def tool_get_sensor_history(sensor_type: str, start_time: datetime, end_time: datetime) -> str:
    """특정 기간 통계 조회"""
    if sensor_type == "dust":
        rows = _db_query(
            "SELECT pm2_5, pm10 FROM dust_data "
            "WHERE timestamp >= %s AND timestamp <= %s ORDER BY timestamp ASC",
            (start_time, end_time),
        )
        summary = _summarize_dust_rows(rows)
        if "pm2_5_avg" in summary:
            return f"PM2.5 평균 {summary['pm2_5_avg']}μg/m³(최고 {summary['pm2_5_max']}), PM10 평균 {summary['pm10_avg']}μg/m³입니다."
        return "해당 기간의 미세먼지 기록이 없습니다."
    else:
        rows = _db_query(
            "SELECT temperature, humidity FROM sensor_data "
            "WHERE timestamp >= %s AND timestamp <= %s ORDER BY timestamp ASC",
            (start_time, end_time),
        )
        summary = _summarize_sensor_rows(rows)
        if "temp_avg" in summary:
            parts = [f"평균 {summary['temp_avg']}°C", f"최저 {summary['temp_min']}°C", f"최고 {summary['temp_max']}°C"]
            if summary.get("humidity_avg"):
                parts.append(f"평균 습도 {summary['humidity_avg']}%")
            return ", ".join(parts) + "입니다."
        return "해당 기간의 온습도 기록이 없습니다."

def tool_get_aircon_history(query_type: str = "recent", limit: int = 5) -> str:
    """에어컨 동작 이력 또는 오늘 횟수 조회"""
    if query_type == "count_today":
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = _db_query("SELECT COUNT(*) AS cnt FROM history WHERE timestamp >= %s", (today_start,))
        cnt = rows[0]["cnt"] if rows else 0
        if cnt > 0:
            return f"오늘 에어컨을 {cnt}번 제어했습니다."
        return "오늘 에어컨 동작 기록이 없습니다."
    else:
        rows = _db_query(
            "SELECT command, response, timestamp FROM history ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        if not rows:
            return "에어컨 제어 기록이 없습니다."
        lines = []
        for r in rows:
            ts = r.get("timestamp", "")
            if hasattr(ts, "strftime"):
                ts = ts.strftime("%Y-%m-%d %H:%M")
            lines.append(f"{ts}: {r.get('command', '')}")
        return "에어컨 최근 제어 기록:\n" + "\n".join(lines)

def tool_control_aircon(args: dict) -> dict:
    mode = args.get("mode", "cool")
    fan  = args.get("fan", "auto")
    temp = args.get("temp", 25)
    index   = _aircon_index(mode, fan, temp)
    command = f"SEND {index},5"
    try:
        if mode == "off":
            response = _send_internal(command)
        else:
            if not _is_aircon_on():
                _send_internal("SEND 1,5")
                time.sleep(1)
            response = _send_internal(command)

        mode_label = {"off": "전원 끄기", "cool": "냉방", "dehumidify": "제습", "power_cool": "파워냉방"}.get(mode, mode)
        fan_label  = {"weak": "약풍", "medium": "중풍", "strong": "강풍", "auto": "자동풍"}.get(fan, fan)
        return {
            "success": True,
            "executed": command,
            "mode": mode_label,
            "fan": fan_label if mode not in ("off", "power_cool") else None,
            "temp": temp if mode not in ("off", "power_cool") else None,
            "arduino_response": response,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

def _format_aircon_result(result: dict) -> str:
    if not result.get("success"):
        return f"에어컨 제어 실패: {result.get('error', '알 수 없는 오류')}"
    mode  = result.get("mode", "")
    fan   = result.get("fan")
    temp  = result.get("temp")
    if mode == "전원 끄기":
        return "에어컨을 꺼드렸습니다."
    parts = [f"에어컨 {mode} 완료."]
    if fan:  parts.append(f"풍량: {fan}")
    if temp: parts.append(f"온도: {temp}°C")
    return " ".join(parts)

def tool_control_torch(args: dict) -> dict:
    action = args.get("action", "off")
    if action not in ("on", "off"):
        return {"success": False, "error": "on 또는 off만 가능합니다."}
    try:
        r = requests.post(
            f"{APP_INTERNAL_URL}/api/internal/torch",
            json={"action": action},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        return {"success": data.get("ok", False), "action": action}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _format_torch_result(result: dict, action: str) -> str:
    if result.get("success"):
        return f"플래시라이트를 {'켰습니다' if action == 'on' else '껐습니다'}."
    return f"플래시라이트 제어 실패: {result.get('error', '알 수 없는 오류')}"

def tool_control_servo(args: dict) -> dict:
    direction = args.get("direction", "")
    if direction not in ("left", "right", "up", "down"):
        return {"success": False, "error": "left/right/up/down 중 하나여야 합니다."}
    try:
        r = requests.post(
            f"{APP_INTERNAL_URL}/api/internal/servo/move",
            json={"direction": direction},
            timeout=5,
        )
        r.raise_for_status()
        return {"success": True, "direction": direction}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _format_servo_result(result: dict) -> str:
    if result.get("success"):
        label = {"left": "왼쪽", "right": "오른쪽", "up": "위", "down": "아래"}.get(result.get("direction", ""), "")
        return f"카메라를 {label}으로 이동했습니다."
    return f"카메라 이동 실패: {result.get('error', '알 수 없는 오류')}"

def tool_get_system_stats(_args=None) -> dict:
    try:
        cpu_temp = subprocess.check_output(["vcgencmd", "measure_temp"]).decode().split("=")[1].split("'")[0]
    except Exception:
        cpu_temp = "N/A"
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu  = psutil.cpu_percent(interval=0.2)
    return {
        "cpu_temp": f"{cpu_temp}°C",
        "cpu_usage_percent": cpu,
        "ram_used_mb":   round(mem.used  / 1024 / 1024),
        "ram_total_mb":  round(mem.total / 1024 / 1024),
        "ram_percent":   mem.percent,
        "disk_used_gb":  round(disk.used  / 1024 ** 3, 1),
        "disk_total_gb": round(disk.total / 1024 ** 3, 1),
        "disk_percent":  disk.percent,
    }

# --- 에어컨 예약 도구 ---
def tool_create_aircon_schedule(args: dict) -> dict:
    action       = args.get("action", "on")
    scheduled_at = args.get("scheduled_at")
    temperature  = args.get("temperature", 25)
    mode         = args.get("mode", "cool")
    wind         = args.get("wind", "auto")

    try:
        if isinstance(scheduled_at, str):
            scheduled_at = _parse_time_arg(scheduled_at)
    except Exception as e:
        return {"success": False, "message": f"예약 시각 형식 오류: {e}"}

    if isinstance(scheduled_at, datetime) and scheduled_at <= datetime.now():
        return {"success": False, "message": "예약 시간은 현재 시각 이후여야 합니다."}
    try:
        conn   = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id FROM aircon_schedule WHERE scheduled_at=%s AND status='pending'",
            (scheduled_at,),
        )
        if cursor.fetchone():
            cursor.close(); conn.close()
            return {"success": False, "message": "이미 예약된 시간입니다."}
        cursor.execute(
            "INSERT INTO aircon_schedule (action, scheduled_at, temperature, mode, wind)"
            " VALUES (%s, %s, %s, %s, %s)",
            (action, scheduled_at, temperature, mode, wind),
        )
        conn.commit()
        new_id = cursor.lastrowid
        cursor.execute("SELECT * FROM aircon_schedule WHERE id=%s", (new_id,))
        row = cursor.fetchone()
        cursor.close(); conn.close()
        if row:
            for k in ("scheduled_at", "created_at"):
                if isinstance(row.get(k), datetime):
                    row[k] = row[k].strftime("%Y-%m-%d %H:%M:%S")
        return {"success": True, "schedule": row}
    except Exception as e:
        return {"success": False, "message": str(e)}

def tool_list_aircon_schedules(_args: dict = None) -> dict:
    rows = _db_query(
        "SELECT * FROM aircon_schedule WHERE status='pending' ORDER BY scheduled_at ASC"
    )
    return {"schedules": rows}

def tool_cancel_aircon_schedule(args: dict) -> dict:
    sid = args.get("id")
    try:
        if sid:
            conn = mysql.connector.connect(**DB_CONFIG)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE aircon_schedule SET status='cancelled'"
                " WHERE id=%s AND status='pending'",
                (sid,),
            )
            conn.commit()
            affected = cursor.rowcount
            cursor.close(); conn.close()
            if affected == 0:
                return {"success": False, "message": f"{sid}번 예약을 찾을 수 없거나 이미 완료/취소 상태입니다."}
            return {"success": True, "cancelled_id": sid}
        else:
            rows = _db_query(
                "SELECT id, action, scheduled_at, temperature, mode"
                " FROM aircon_schedule WHERE status='pending' ORDER BY scheduled_at ASC"
            )
            if not rows:
                return {"success": False, "message": "취소할 예약이 없습니다."}
            if len(rows) == 1:
                only_id = rows[0]["id"]
                conn = mysql.connector.connect(**DB_CONFIG)
                cursor = conn.cursor()
                cursor.execute("UPDATE aircon_schedule SET status='cancelled' WHERE id=%s", (only_id,))
                conn.commit()
                cursor.close(); conn.close()
                return {"success": True, "cancelled_id": only_id}
            return {"success": False, "pending_list": rows, "message": "취소할 예약 번호를 지정해 주세요."}
    except Exception as e:
        return {"success": False, "message": str(e)}

def _fmt_sched_time(sat) -> str:
    try:
        dt = datetime.strptime(str(sat)[:19], "%Y-%m-%d %H:%M:%S") if isinstance(sat, str) else sat
        return dt.strftime("%-m월 %-d일 %H:%M") if dt.minute else dt.strftime("%-m월 %-d일 %H시")
    except Exception:
        return str(sat)[:16]

def _format_schedule_create(data: dict) -> str:
    if not data.get("success"):
        return f"예약 등록 실패: {data.get('message', '오류')}"
    s = data.get("schedule", {})
    tstr   = _fmt_sched_time(s.get("scheduled_at", ""))
    action = s.get("action")
    sid    = s.get("id")
    if action == "off":
        return f"{tstr}에 에어컨 끄기 예약 완료했습니다. (#{sid})"
    ml   = {"cool": "냉방", "dry": "제습"}.get(s.get("mode", "cool"), "냉방")
    wl   = {"auto": "자동", "low": "약풍", "mid": "중풍", "high": "강풍"}.get(s.get("wind", "auto"), "자동")
    temp = s.get("temperature", 25)
    return f"{tstr}에 에어컨 {ml} {temp}도 {wl} 켜기 예약 완료했습니다. (#{sid})"

def _format_schedule_list(data: dict) -> str:
    schedules = data.get("schedules", [])
    if not schedules:
        return "현재 대기 중인 예약이 없습니다."
    lines = [f"대기 중인 예약 {len(schedules)}건입니다:"]
    for s in schedules:
        action = s.get("action")
        if action == "off":
            desc = "끄기"
        else:
            ml   = {"cool": "냉방", "dry": "제습"}.get(s.get("mode", "cool"), "냉방")
            desc = f"켜기({ml} {s.get('temperature', 25)}도)"
        lines.append(f"  [{s['id']}번] {_fmt_sched_time(s.get('scheduled_at',''))} — {desc}")
    return "\n".join(lines)

def _format_schedule_cancel(data: dict) -> str:
    if not data.get("success"):
        if "pending_list" in data:
            rows  = data["pending_list"]
            lines = ["취소할 예약 번호를 지정해 주세요. 대기 중인 예약:"]
            for s in rows:
                action = s.get("action")
                desc   = "끄기" if action == "off" else f"켜기({s.get('temperature', 25)}도)"
                lines.append(f"  [{s['id']}번] {_fmt_sched_time(s.get('scheduled_at',''))} — {desc}")
            return "\n".join(lines)
        return f"취소 실패: {data.get('message', '오류')}"
    return f"{data.get('cancelled_id')}번 예약이 취소되었습니다."

# --- CCTV 영상 분석 ---
def _capture_cctv_frame() -> bytes | None:
    try:
        r = requests.get(MJPG_SNAPSHOT_URL, timeout=5)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:
        pass
    return None

def _vision_query(user_message: str, frame: bytes) -> str:
    prompt = (
        "이것은 스마트홈 실내 CCTV 이미지입니다. "
        "아래 질문에 한국어로 1~2문장으로 간결하게 답하세요.\n"
        f"질문: {user_message}"
    )
    for model_name in [GEMINI_MODEL, FALLBACK_MODEL]:
        try:
            response = _gemini_client.models.generate_content(
                model=model_name,
                contents=[
                    genai_types.Part.from_bytes(data=frame, mime_type="image/jpeg"),
                    genai_types.Part.from_text(text=prompt),
                ],
            )
            return response.text or "이미지를 분석할 수 없습니다."
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                continue
            return f"이미지 분석 중 오류가 발생했습니다: {e}"
    return "현재 AI 사용량 한도로 인해 이미지 분석을 일시적으로 완료할 수 없습니다."

# --- 시스템 프롬프트 및 컨텍스트 빌더 ---
def _build_system_prompt() -> str:
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    weekdays = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
    weekday_str = weekdays[now.weekday()]

    # 실시간 센서
    cur_s = tool_get_current_temperature({})
    cur_h = tool_get_current_humidity({})
    cur_d = tool_get_current_dust({})
    temp_val = cur_s.get("temperature", "확인불가")
    hum_val  = cur_h.get("humidity_percent", "확인불가")
    pm25_val = cur_d.get("pm2_5", "확인불가")
    pm10_val = cur_d.get("pm10", "확인불가")

    # 오늘 통계
    today_start = datetime(now.year, now.month, now.day)
    stats = _db_query(
        "SELECT ROUND(AVG(temperature),1) ta, ROUND(MIN(temperature),1) tmin,"
        " ROUND(MAX(temperature),1) tmax, ROUND(AVG(humidity),1) ha"
        " FROM sensor_data WHERE timestamp >= %s",
        (today_start,),
    )
    today_stats_str = "오늘 통계 없음"
    if stats and stats[0].get("ta") is not None:
        s = stats[0]
        today_stats_str = f"평균 {s['ta']}°C, 최저 {s['tmin']}°C, 최고 {s['tmax']}°C, 평균 습도 {s['ha']}%"

    dust_stats = _db_query(
        "SELECT ROUND(AVG(pm2_5),1) p25a, ROUND(MAX(pm2_5),1) p25m,"
        " ROUND(AVG(pm10),1) p10a, ROUND(MAX(pm10),1) p10m"
        " FROM dust_data WHERE timestamp >= %s",
        (today_start,),
    )
    today_dust_stats_str = ""
    if dust_stats and dust_stats[0].get("p25a") is not None:
        ds = dust_stats[0]
        today_dust_stats_str = f"PM2.5 평균 {ds['p25a']}μg/m³(최고 {ds['p25m']}), PM10 평균 {ds['p10a']}μg/m³"

    # 에어컨 상태
    is_on = _is_aircon_on()
    aircon_state = _get_current_aircon_state()
    aircon_status_str = f"현재 실제 상태: {'켜짐(ON)' if is_on else '꺼짐(OFF)'}"
    if aircon_state:
        aircon_status_str += f", 최근 설정: 모드={aircon_state['mode']}, 온도={aircon_state['temp']}°C, 풍량={aircon_state['fan']}"

    # 대기 중인 예약
    schedules = _db_query("SELECT id, action, scheduled_at, temperature, mode FROM aircon_schedule WHERE status='pending' ORDER BY scheduled_at ASC")
    if schedules:
        sched_list = [f"[#{s['id']}] {s['scheduled_at']}: {'켜기' if s['action']=='on' else '끄기'}" for s in schedules]
        pending_str = ", ".join(sched_list)
    else:
        pending_str = "없음"

    # 시스템 통계
    sys_stats = tool_get_system_stats({})
    sys_str = f"CPU 온도: {sys_stats.get('cpu_temp')}, CPU 사용률: {sys_stats.get('cpu_usage_percent')}%, RAM: {sys_stats.get('ram_used_mb')}MB/{sys_stats.get('ram_total_mb')}MB ({sys_stats.get('ram_percent')}%), 디스크 사용: {sys_stats.get('disk_percent')}%"

    prompt = f"""당신은 스마트홈 AI 어시스턴트입니다.
사용자의 질문 또는 명령을 분석하여 반드시 지정된 JSON 형식으로만 응답해야 합니다.

[현재 실시간 시스템 및 환경 정보]
- 현재 기준 시각: {now.strftime("%Y-%m-%d %H:%M:%S")} KST ({weekday_str})
- 오늘: {today_str} | 어제: {yesterday_str} | 내일: {tomorrow_str}
- 현재 실내 온습도: 실내 온도 {temp_val}°C, 실내 습도 {hum_val}%
- 현재 실내 미세먼지: PM2.5 {pm25_val}μg/m³, PM10 {pm10_val}μg/m³
- 오늘 온습도 통계: {today_stats_str}
- 오늘 미세먼지 통계: {today_dust_stats_str}
- 에어컨 상태: {aircon_status_str}
- 대기 중인 에어컨 예약: {pending_str}
- 시스템 상태: {sys_str}

[응답 JSON 스키마]
반드시 아래 JSON 형식으로만 응답하세요:
{{
  "thought": "사용자의 의도 분석 및 선택한 동작 이유",
  "action": "none" | "control_aircon" | "create_aircon_schedule" | "list_aircon_schedules" | "cancel_aircon_schedule" | "control_torch" | "control_servo" | "get_sensor_at" | "get_sensor_history" | "get_aircon_history" | "vision",
  "params": {{}},
  "reply": "사용자에게 전달할 친절한 한국어 답변 (1~2문장)"
}}

[Action별 규칙 및 파라미터]
1. "none":
   - 추가 하드웨어/DB 작업 없이 즉시 답변할 때 사용.
   - 현재 온습도/미세먼지 질문 ("지금 몇도야?", "습도 어때?", "미세먼지 얼마야?", "공기질 어때?", "온도랑 습도 둘 다")
     * 온도를 답할 때는 반드시 '°C' 또는 '도'를 포함할 것.
     * 습도를 답할 때는 반드시 '%'를 포함할 것.
     * 미세먼지를 답할 때는 반드시 'PM2.5', 'PM10' 또는 '미세먼지'를 포함할 것.
   - 시스템 상태 질문 ("CPU 온도 알려줘", "메모리 사용량", "시스템 상태")
     * 위 [시스템 상태]의 수치를 활용해 답변할 것.
   - 에어컨 켜짐 상태 질문 ("에어컨 켜져 있어?", "지금 작동 중이야?")
     * 위 [에어컨 상태]를 확인하고 켜져 있는지 꺼져 있는지 명확히 답변할 것.
   - 조언/추론/판단 질문 ("에어컨 켜야 할까?", "환기해야 할까?", "창문 열어도 돼?", "지금 쾌적해?")
     * 현재 온습도/미세먼지 수치를 바탕으로 친절하게 조언할 것. (에어컨을 제어하지 마세요!)
   - 일반 대화 ("안녕", "고마워", "뭘 할 수 있어?")

2. "control_aircon":
   - 사용자가 '지금 즉시' 에어컨을 켜거나 끄거나 설정을 바꾸라고 직접 명령할 때만 사용.
   - params:
     * "mode": "off" (끄기) | "cool" (냉방) | "dehumidify" (제습) | "power_cool" (파워냉방)
     * "temp": 희망 온도 (정수 18~30, 미언급 시 최근 설정 또는 25)
     * "fan": 풍량 ("weak" | "medium" | "strong" | "auto", 미언급 시 auto)
   - "에어컨 켜야 할까?" 같은 조언 질문이나 "N시간 뒤에 꺼줘" 같은 예약은 절대 control_aircon이 아닙니다!

3. "create_aircon_schedule":
   - 미래 시각에 에어컨 켜기/끄기 예약을 요청할 때 사용 (예: "오늘 밤 11시에 꺼줘", "1시간 뒤에 켜줘", "내일 오전 7시에 25도 냉방 켜줘").
   - params:
     * "action": "on" | "off"
     * "scheduled_at": "YYYY-MM-DD HH:MM:SS" (현재 기준 시각을 바탕으로 정확히 계산된 미래 시각)
     * "mode": "cool" | "dry" (on일 때, 기본 "cool")
     * "temperature": 정수 18~30 (on일 때, 기본 25)
     * "wind": "auto" | "low" | "mid" | "high" (on일 때, 기본 "auto")

4. "list_aircon_schedules":
   - 에어컨 예약 목록 확인/조회 요청 ("예약 보여줘", "예약된 거 있어?")
   - params: {{}}

5. "cancel_aircon_schedule":
   - 에어컨 예약 취소 요청 ("예약 취소해줘", "1번 예약 취소")
   - params: {{"id": int 또는 null}}

6. "control_torch":
   - 스마트폰 플래시/손전등/불 켜기 및 끄기 명령 ("플래시 켜줘", "손전등 꺼줘", "불 켜줘", "불 꺼줘")
   - params: {{"action": "on" | "off"}}

7. "control_servo":
   - CCTV 카메라 방향 조절 명령 ("카메라 왼쪽으로", "카메라 위로 올려줘", "웹캠 오른쪽")
   - params: {{"direction": "left" | "right" | "up" | "down"}}

8. "get_sensor_at":
   - 과거 특정 시점의 온습도/미세먼지 질문 ("1시간 전 온도", "30분 전 미세먼지", "2026년 1월 1일 00시 온도", "어제 3시 습도")
   - params:
     * "sensor": "temperature" | "humidity" | "dust" | "temp_humidity"
     * "target_time": "YYYY-MM-DD HH:MM:SS" (현재 기준 시각에서 계산한 과거 시각)

9. "get_sensor_history":
   - 특정 기간/범위의 통계 질문 ("오늘 평균 온도", "어제 최고 온도", "최근 3시간 미세먼지 통계", "26년도 1월 평균 온도")
   - params:
     * "sensor": "temperature" | "humidity" | "dust"
     * "start_time": "YYYY-MM-DD HH:MM:SS"
     * "end_time": "YYYY-MM-DD HH:MM:SS"

10. "get_aircon_history":
    - 에어컨 제어 기록 또는 오늘 제어 횟수 질문 ("오늘 에어컨 몇 번 켰어?", "가장 최근 에어컨 동작 기록")
    - params:
      * "query_type": "count_today" (오늘 몇 번 켰는지) | "recent" (최근 제어 기록 목록)
      * "limit": int (recent일 때 이력 개수, 기본 5)

11. "vision":
    - CCTV 영상/방 상태를 직접 확인해야 하는 시각적 질문 ("방에 불 켜져 있어?", "방 어때?", "실내 모습 봐줘", "방 어두워?")
    - params: {{}}
"""
    return prompt

def _clean_and_parse_json(text: str) -> dict:
    """Gemini 응답에서 마크다운 및 공백 제거 후 JSON 파싱"""
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return json.loads(t)

def _call_gemini_json(user_message: str, history: list, system_prompt: str) -> dict:
    contents = []
    # 이전 대화 기록 전달 (최근 6개)
    for h in history[-6:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
            role = "model" if h["role"] == "assistant" else "user"
            content_text = h.get("content", "")
            if content_text:
                contents.append(genai_types.Content(role=role, parts=[genai_types.Part.from_text(text=content_text)]))
    contents.append(genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=user_message)]))

    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        system_instruction=system_prompt,
        temperature=0.2,
    )

    models_to_try = [GEMINI_MODEL, FALLBACK_MODEL]
    last_err = None

    for model_name in models_to_try:
        delay = 2.0
        for attempt in range(3):
            try:
                res = _gemini_client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )
                raw_text = res.text or "{}"
                return _clean_and_parse_json(raw_text)
            except Exception as e:
                last_err = e
                err_msg = str(e)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                    time.sleep(delay)
                    delay *= 2
                    continue
                break

    return {
        "thought": f"Gemini API 오류: {last_err}",
        "action": "none",
        "params": {},
        "reply": "AI 서비스 연결 중 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    }

def _execute_action(parsed: dict, user_message: str) -> str:
    action = parsed.get("action", "none")
    params = parsed.get("params") or {}
    reply  = parsed.get("reply", "")

    if action == "control_aircon":
        # 파라미터 정규화
        mode = params.get("mode")
        if not mode and params.get("operation"):
            op = str(params.get("operation")).lower()
            mode = "off" if "off" in op or "끄" in op else "cool"
        if not mode:
            mode = "cool"

        # 온도 및 풍량
        temp = params.get("temp") or params.get("temperature") or params.get("target_temp")
        fan  = params.get("fan") or params.get("wind") or params.get("fan_speed")

        # 부분 변경 시 기존 상태 상속
        if mode != "off" and (temp is None or fan is None):
            current = _get_current_aircon_state()
            if current:
                if temp is None: temp = current.get("temp")
                if fan is None:  fan = current.get("fan")

        temp = int(temp) if temp is not None else 25
        fan  = str(fan) if fan is not None else "auto"

        # 한국어 풍량 정규화
        fan_map = {"약풍": "weak", "중풍": "medium", "강풍": "strong", "자동": "auto", "자동풍": "auto", "low": "weak", "mid": "medium", "high": "strong"}
        fan = fan_map.get(fan, fan)

        res = tool_control_aircon({"mode": mode, "fan": fan, "temp": temp})
        return _format_aircon_result(res)

    elif action == "create_aircon_schedule":
        sched_at = params.get("scheduled_at") or params.get("time")
        act = params.get("action", "on")
        if not act and params.get("command"):
            act = "off" if "off" in str(params.get("command")).lower() else "on"
        temp = params.get("temperature") or params.get("temp") or 25
        mode = params.get("mode", "cool")
        if mode in ("dehumidify", "제습"):
            mode = "dry"
        wind = params.get("wind") or params.get("fan") or "auto"
        res = tool_create_aircon_schedule({
            "action": act,
            "scheduled_at": sched_at,
            "temperature": temp,
            "mode": mode,
            "wind": wind,
        })
        return _format_schedule_create(res)

    elif action == "list_aircon_schedules":
        res = tool_list_aircon_schedules({})
        return _format_schedule_list(res)

    elif action == "cancel_aircon_schedule":
        sid = params.get("id")
        res = tool_cancel_aircon_schedule({"id": sid})
        return _format_schedule_cancel(res)

    elif action == "control_torch":
        torch_act = params.get("action") or params.get("power") or "off"
        if str(torch_act).lower() in ("true", "1", "on", "켜기"):
            torch_act = "on"
        elif str(torch_act).lower() in ("false", "0", "off", "끄기"):
            torch_act = "off"
        res = tool_control_torch({"action": torch_act})
        return _format_torch_result(res, torch_act)

    elif action == "control_servo":
        direction = params.get("direction", "")
        dir_map = {"왼쪽": "left", "오른쪽": "right", "위": "up", "아래": "down"}
        direction = dir_map.get(direction, direction)
        res = tool_control_servo({"direction": direction})
        return _format_servo_result(res)

    elif action == "get_sensor_at":
        target_time_str = params.get("target_time") or params.get("time")
        sensor_type = params.get("sensor", "temperature")
        try:
            target_dt = _parse_time_arg(target_time_str)
            return tool_get_sensor_at(sensor_type, target_dt)
        except Exception as e:
            return f"센서 기록 조회 중 오류가 발생했습니다: {e}"

    elif action == "get_sensor_history":
        start_str = params.get("start_time") or params.get("from")
        end_str   = params.get("end_time") or params.get("to")
        sensor_type = params.get("sensor", "temperature")
        now = datetime.now()
        try:
            start_dt = _parse_time_arg(start_str) if start_str else now - timedelta(hours=24)
            end_dt   = _parse_time_arg(end_str) if end_str else now
            return tool_get_sensor_history(sensor_type, start_dt, end_dt)
        except Exception as e:
            return f"센서 통계 조회 중 오류가 발생했습니다: {e}"

    elif action == "get_aircon_history":
        q_type = params.get("query_type", "recent")
        limit  = params.get("limit", 5)
        return tool_get_aircon_history(query_type=q_type, limit=limit)

    elif action == "vision":
        frame = _capture_cctv_frame()
        if not frame:
            return "현재 카메라 연결이 되지 않아 영상을 확인할 수 없습니다."
        return _vision_query(user_message, frame)

    # action == "none" or fallback
    return reply or "네, 무엇을 도와드릴까요?"


@app.route("/api/chat", methods=["POST"])
def chat():
    body         = request.get_json(silent=True) or {}
    user_message = (body.get("message") or "").strip()
    history      = body.get("history") or []

    if not user_message:
        return jsonify({"error": "메시지가 비어 있습니다."}), 400

    # 1. 실시간 컨텍스트 및 시스템 프롬프트 구성
    system_prompt = _build_system_prompt()

    # 2. Gemini API 호출 (구조화된 JSON 응답 생성)
    parsed = _call_gemini_json(user_message, history, system_prompt)

    # 3. 모델이 결정한 액션 수행 및 응답 반환
    reply = _execute_action(parsed, user_message)

    return jsonify({"reply": reply})


@app.route("/api/chat/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": GEMINI_MODEL})


if __name__ == "__main__":
    print(f"Chatbot starting on http://0.0.0.0:5001 (model: {GEMINI_MODEL})")
    app.run(host="0.0.0.0", port=5001, debug=False)
