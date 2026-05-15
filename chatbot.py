# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import mysql.connector
import ollama
import psutil
import serial
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
SERIAL_PORT = "/dev/ttyUSB0"
BAUD_RATE = 9600
OLLAMA_MODEL = "qwen2.5:1.5b"
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173")

app = Flask(__name__)
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

def _is_aircon_on():
    """마지막 에어컨 명령이 OFF가 아니면 켜져 있다고 판단"""
    rows = _db_query(
        "SELECT command FROM history ORDER BY timestamp DESC LIMIT 1"
    )
    if not rows:
        return False
    return rows[0].get("command", "") != "SEND 0,5"

# --- 도구 구현 ---
def tool_get_current_temperature(_args):
    rows = _db_query("SELECT temperature, timestamp FROM sensor_data ORDER BY timestamp DESC LIMIT 1")
    if not rows:
        return {"error": "데이터 없음"}
    return {"temperature": rows[0]["temperature"]}

def tool_get_current_humidity(_args):
    rows = _db_query("SELECT humidity, timestamp FROM sensor_data ORDER BY timestamp DESC LIMIT 1")
    if not rows:
        return {"error": "데이터 없음"}
    return {"humidity_percent": rows[0]["humidity"]}

def tool_get_current_dust(_args):
    rows = _db_query("SELECT pm2_5, pm10, timestamp FROM dust_data ORDER BY timestamp DESC LIMIT 1")
    if not rows:
        return {"error": "데이터 없음"}
    return {"pm2_5": rows[0]["pm2_5"], "pm10": rows[0]["pm10"]}

def _parse_time_arg(s):
    """LLM이 전달한 날짜 문자열을 datetime으로 파싱 (여러 형식 허용)"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"날짜 형식 인식 불가: {s}")

def _summarize_sensor_rows(rows):
    if not rows:
        return {"message": "해당 기간 데이터 없음"}
    temps = [r["temperature"] for r in rows]
    hums  = [r["humidity"]    for r in rows]
    return {
        "temp_avg": round(sum(temps) / len(temps), 1),
        "temp_min": min(temps),
        "temp_max": max(temps),
        "humidity_avg": round(sum(hums) / len(hums), 1),
    }

def _summarize_dust_rows(rows):
    if not rows:
        return {"message": "해당 기간 데이터 없음"}
    pm25 = [r["pm2_5"] for r in rows]
    pm10 = [r["pm10"]  for r in rows]
    return {
        "pm2_5_avg": round(sum(pm25) / len(pm25), 1),
        "pm2_5_max": max(pm25),
        "pm10_avg":  round(sum(pm10) / len(pm10), 1),
        "pm10_max":  max(pm10),
    }

def tool_get_temperature_at(args):
    """특정 시각에 가장 가까운 온도 데이터 반환"""
    time_str = args.get("time")
    if not time_str:
        return {"error": "time 파라미터 필요"}
    try:
        target = _parse_time_arg(time_str)
    except ValueError as e:
        return {"error": str(e)}
    rows = _db_query(
        "SELECT temperature, timestamp FROM sensor_data "
        "ORDER BY ABS(TIMESTAMPDIFF(SECOND, timestamp, %s)) LIMIT 1",
        (target,),
    )
    if not rows:
        return {"error": "해당 시각 근처 데이터 없음"}
    return {"temperature": rows[0]["temperature"]}

def tool_get_humidity_at(args):
    """특정 시각에 가장 가까운 습도 데이터 반환"""
    time_str = args.get("time")
    if not time_str:
        return {"error": "time 파라미터 필요"}
    try:
        target = _parse_time_arg(time_str)
    except ValueError as e:
        return {"error": str(e)}
    rows = _db_query(
        "SELECT humidity, timestamp FROM sensor_data "
        "ORDER BY ABS(TIMESTAMPDIFF(SECOND, timestamp, %s)) LIMIT 1",
        (target,),
    )
    if not rows:
        return {"error": "해당 시각 근처 데이터 없음"}
    return {"humidity_percent": rows[0]["humidity"]}


def tool_get_dust_at(args):
    """특정 시각에 가장 가까운 미세먼지 데이터 반환"""
    time_str = args.get("time")
    if not time_str:
        return {"error": "time 파라미터 필요"}
    try:
        target = _parse_time_arg(time_str)
    except ValueError as e:
        return {"error": str(e)}
    rows = _db_query(
        "SELECT pm2_5, pm10, timestamp FROM dust_data "
        "ORDER BY ABS(TIMESTAMPDIFF(SECOND, timestamp, %s)) LIMIT 1",
        (target,),
    )
    if not rows:
        return {"error": "해당 시각 근처 데이터 없음"}
    return {"pm2_5": rows[0]["pm2_5"], "pm10": rows[0]["pm10"]}

def tool_get_sensor_history(args):
    hours = int(args.get("hours", 24))
    since = datetime.now() - timedelta(hours=hours)
    rows = _db_query(
        "SELECT temperature, humidity, timestamp FROM sensor_data "
        "WHERE timestamp >= %s ORDER BY timestamp ASC",
        (since,),
    )
    return _summarize_sensor_rows(rows)

def tool_get_dust_history(args):
    hours = int(args.get("hours", 24))
    since = datetime.now() - timedelta(hours=hours)
    rows = _db_query(
        "SELECT pm2_5, pm10, timestamp FROM dust_data "
        "WHERE timestamp >= %s ORDER BY timestamp ASC",
        (since,),
    )
    return _summarize_dust_rows(rows)

def tool_get_aircon_history(args):
    limit = int(args.get("limit", 10))
    rows = _db_query(
        "SELECT command, response, timestamp FROM history ORDER BY timestamp DESC LIMIT %s",
        (limit,),
    )
    return rows if rows else {"message": "에어컨 제어 이력 없음"}

def _send_serial(ser, command):
    ser.write(f"{command}\n".encode("utf-8"))
    response = ser.readline().decode("utf-8").strip()
    _db_insert(
        "INSERT INTO history (command, response, timestamp) VALUES (%s, %s, %s)",
        (command, response, datetime.now()),
    )
    return response

def tool_control_aircon(args):
    mode = args.get("mode", "off")
    fan  = args.get("fan", "auto")
    temp = args.get("temp", 25)
    index   = _aircon_index(mode, fan, temp)
    command = f"SEND {index},5"
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=3)
        time.sleep(2)

        if mode == "off":
            response = _send_serial(ser, command)
        else:
            # 꺼져 있을 때만 전원 ON 먼저 전송
            if not _is_aircon_on():
                _send_serial(ser, "SEND 1,5")
                time.sleep(1)
            response = _send_serial(ser, command)

        ser.close()
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

def tool_get_system_stats(_args):
    try:
        cpu_temp = subprocess.check_output(["vcgencmd", "measure_temp"]).decode().split("=")[1].split("'")[0]
    except Exception:
        cpu_temp = "N/A"
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu  = psutil.cpu_percent(interval=1)
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

# --- 도구 디스패처 ---
TOOL_HANDLERS = {
    "get_current_temperature": tool_get_current_temperature,
    "get_current_humidity":    tool_get_current_humidity,
    "get_current_dust":        tool_get_current_dust,
    "get_temperature_at":  tool_get_temperature_at,
    "get_dust_at":         tool_get_dust_at,
    "get_sensor_history":  tool_get_sensor_history,
    "get_dust_history":    tool_get_dust_history,
    "get_aircon_history":  tool_get_aircon_history,
    "control_aircon":      tool_control_aircon,
    "get_system_stats":    tool_get_system_stats,
}

# --- ollama 도구 스키마 ---
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_temperature",
            "description": "현재 온도(°C)만 조회합니다. 온도를 물을 때 사용.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_humidity",
            "description": "현재 습도(%)만 조회합니다. 습도를 물을 때 사용.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_dust",
            "description": "현재 미세먼지(PM2.5, PM10)를 조회합니다. 미세먼지를 물을 때 사용.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_temperature_at",
            "description": "특정 날짜/시각의 온도를 조회합니다. '몇월 며칠 몇시의 온도'처럼 과거 특정 시점을 물을 때 사용.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time": {"type": "string", "description": "조회할 시각. 형식: 'YYYY-MM-DD HH:MM:SS'. 예: '2026-01-01 00:00:00'"},
                },
                "required": ["time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dust_at",
            "description": "특정 날짜/시각의 미세먼지를 조회합니다. '몇월 며칠 몇시의 미세먼지'처럼 과거 특정 시점을 물을 때 사용.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time": {"type": "string", "description": "조회할 시각. 형식: 'YYYY-MM-DD HH:MM:SS'. 예: '2026-01-01 00:00:00'"},
                },
                "required": ["time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sensor_history",
            "description": "최근 N시간의 온습도 통계(평균/최대/최소)를 조회합니다. '오늘', '최근 몇 시간' 같은 기간 질문에 사용.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "최근 몇 시간 데이터 (기본 24)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dust_history",
            "description": "최근 N시간의 미세먼지 통계(평균/최대)를 조회합니다. '오늘', '최근 몇 시간' 같은 기간 질문에 사용.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "최근 몇 시간 데이터 (기본 24)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_aircon_history",
            "description": "최근 에어컨 제어 이력을 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "조회할 이력 수 (기본 10)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_aircon",
            "description": "에어컨을 제어합니다. 전원 끄기, 냉방, 제습, 파워냉방 지원.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["off", "cool", "dehumidify", "power_cool"],
                        "description": "off=전원끄기, cool=냉방, dehumidify=제습, power_cool=파워냉방",
                    },
                    "fan": {
                        "type": "string",
                        "enum": ["weak", "medium", "strong", "auto"],
                        "description": "weak=약풍, medium=중풍, strong=강풍, auto=자동풍 (기본 auto)",
                    },
                    "temp": {
                        "type": "integer",
                        "description": "희망 온도 (정수, 18~30). 사용자가 말한 온도를 그대로 사용. 예: '24도' → 24. 미언급 시 25.",
                    },
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_stats",
            "description": "라즈베리파이 시스템 상태(CPU 온도/사용률, RAM, 디스크)를 조회합니다.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

# LLM에 전달할 도구는 3개만 — 센서 조회는 Python fast path가 담당
TOOLS_LLM = [
    {
        "type": "function",
        "function": {
            "name": "control_aircon",
            "description": "에어컨을 제어합니다. 전원 끄기, 냉방, 제습, 파워냉방 지원.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["off", "cool", "dehumidify", "power_cool"],
                        "description": "off=전원끄기, cool=냉방, dehumidify=제습, power_cool=파워냉방",
                    },
                    "fan": {
                        "type": "string",
                        "enum": ["weak", "medium", "strong", "auto"],
                        "description": "약풍=weak 중풍=medium 강풍=strong 자동=auto (기본 auto)",
                    },
                    "temp": {
                        "type": "integer",
                        "description": "희망 온도 18~30. 미언급 시 25.",
                    },
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_stats",
            "description": "라즈베리파이 시스템 상태(CPU 온도/사용률, RAM, 디스크)를 조회합니다.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_aircon_history",
            "description": "최근 에어컨 제어 이력을 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "조회할 이력 수 (기본 10)"},
                },
            },
        },
    },
]

SYSTEM_PROMPT = """당신은 스마트홈 AI 어시스턴트입니다. 반드시 한국어로만, 1~2문장으로만 답하세요.
현재 센서: {sensor_context}
오늘: {today} | 어제: {yesterday}

[규칙] 도구명·파라미터명 응답 금지. 요청한 것만 답하세요.
현재 온도·습도·미세먼지는 위 '현재 센서' 값을 사용해 도구 없이 직접 답하세요.
[도구 호출] 에어컨 제어→control_aircon | 시스템 상태→get_system_stats | 에어컨 이력→get_aircon_history
[에어컨] 냉방/덥다/시원→cool | 제습/습해→dehumidify | 파워/강력→power_cool | 끄기→off | 온도미언급→25 | 풍량미언급→auto"""


# --- 오늘 센서 컨텍스트 캐시 (5분 TTL) ---
_context_cache: dict = {"data": "", "ts": 0.0}

def _build_today_context() -> str:
    """현재 센서값 + 오늘 통계 한 줄 반환 (5분 캐시).
    시간별 전체 데이터 주입은 1.5B 모델 속도에 치명적이므로 최소화."""
    now = datetime.now()
    if now.timestamp() - _context_cache["ts"] < 300 and _context_cache["data"]:
        return _context_cache["data"]

    today_start = datetime(now.year, now.month, now.day)
    cur_s  = _db_query("SELECT temperature, humidity FROM sensor_data ORDER BY timestamp DESC LIMIT 1")
    cur_d  = _db_query("SELECT pm2_5, pm10 FROM dust_data ORDER BY timestamp DESC LIMIT 1")
    stats  = _db_query(
        "SELECT ROUND(AVG(temperature),1) ta, ROUND(MIN(temperature),1) tmin,"
        " ROUND(MAX(temperature),1) tmax, ROUND(AVG(humidity),1) ha"
        " FROM sensor_data WHERE timestamp >= %s",
        (today_start,),
    )

    parts = []
    if cur_s:
        r = cur_s[0]
        parts.append(f"현재 온도:{r['temperature']}°C 습도:{r['humidity']}%")
    if cur_d:
        d = cur_d[0]
        parts.append(f"현재 PM2.5:{d['pm2_5']} PM10:{d['pm10']}")
    if stats and stats[0].get("ta") is not None:
        s = stats[0]
        parts.append(f"오늘 평균:{s['ta']}°C 최저:{s['tmin']} 최고:{s['tmax']} 습도평균:{s['ha']}%")

    result = " | ".join(parts)
    _context_cache["data"] = result
    _context_cache["ts"] = now.timestamp()
    return result


def _detect_current_sensor(text):
    """현재 센서 조회인지 감지. 반환: 'temperature'|'humidity'|'dust'|'temp_humidity'|None"""
    # 시간 표현이 있으면 현재 조회 아님
    if _detect_time_context(text):
        return None
    # 에어컨·시스템 명령이면 제외
    if any(k in text for k in ["에어컨", "냉방", "제습", "파워", "cpu", "디스크", "서버", "시스템", "켜줘", "꺼줘", "켜 줘", "꺼 줘"]):
        return None

    has_temp  = any(k in text for k in ["온도", "기온", "몇도", "몇 도"])
    has_hum   = any(k in text for k in ["습도"])
    has_dust  = any(k in text.lower() for k in ["미세먼지", "먼지", "pm", "오염"])

    if has_dust:            return "dust"
    if has_temp and has_hum: return "temp_humidity"
    if has_temp:             return "temperature"
    if has_hum:              return "humidity"
    return None


def _fetch_current_sensors(sensor_type: str) -> dict:
    if sensor_type == "temperature":  return tool_get_current_temperature({})
    if sensor_type == "humidity":     return tool_get_current_humidity({})
    if sensor_type == "dust":         return tool_get_current_dust({})
    if sensor_type == "temp_humidity":
        t = tool_get_current_temperature({})
        h = tool_get_current_humidity({})
        return {**t, **h}
    return {"error": "알 수 없는 타입"}


def _quick_format(result: dict, is_current: bool = False) -> str:
    """LLM 없이 Python으로 자연스러운 한국어 한 문장 생성"""
    if "error" in result:
        return "해당 시각 데이터가 없습니다."
    if "message" in result:
        return result["message"]

    keys   = set(result.keys())
    prefix = "현재 " if is_current else ""

    if keys == {"temperature"}:
        return f"{prefix}온도는 {result['temperature']}°C입니다."
    if keys == {"humidity_percent"}:
        return f"{prefix}습도는 {result['humidity_percent']}%입니다."
    if "temperature" in keys and "humidity_percent" in keys:
        return f"{prefix}온도 {result['temperature']}°C, 습도 {result['humidity_percent']}%입니다."
    if keys <= {"pm2_5", "pm10"}:
        return f"{prefix}PM2.5 {result['pm2_5']}μg/m³, PM10 {result['pm10']}μg/m³입니다."
    if "temp_avg" in keys:
        parts = [f"평균 {result['temp_avg']}°C", f"최저 {result['temp_min']}°C", f"최고 {result['temp_max']}°C"]
        if "humidity_avg" in keys:
            parts.append(f"평균 습도 {result['humidity_avg']}%")
        return ", ".join(parts) + "입니다."
    if "pm2_5_avg" in keys:
        return (f"PM2.5 평균 {result['pm2_5_avg']}μg/m³(최고 {result['pm2_5_max']}), "
                f"PM10 평균 {result['pm10_avg']}μg/m³입니다.")
    return _humanize_result(result) + "입니다."


def _detect_time_context(text):
    """
    시간 표현 감지. 반환값:
        ("point", datetime)             — 특정 시점
        ("range", datetime_from, datetime_to) — 기간
        None                            — 감지 불가
    """
    import re
    now         = datetime.now()
    today_start = datetime(now.year, now.month, now.day)

    # YYYY년 MM월 DD일 [HH시] — 절대 일자
    m = re.search(r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일(?:\s*(\d{1,2})시)?', text)
    if m:
        hour = int(m.group(4)) if m.group(4) else 0
        return ("point", datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), hour))

    # YY/YYYY년[도] MM월 (일 없음) — 월 전체 범위
    m = re.search(r'(\d{2,4})년\s*도?\s*(\d{1,2})월(?!\s*\d+일)', text)
    if m:
        year  = int(m.group(1))
        if year < 100:
            year += 2000
        month  = int(m.group(2))
        dt_from = datetime(year, month, 1)
        dt_to   = datetime(year + (1 if month == 12 else 0),
                           1 if month == 12 else month + 1, 1)
        return ("range", dt_from, dt_to)

    # N시간 전
    m = re.search(r'(\d+)\s*시간\s*전', text)
    if m:
        return ("point", now - timedelta(hours=int(m.group(1))))

    # N분 전
    m = re.search(r'(\d+)\s*분\s*전', text)
    if m:
        return ("point", now - timedelta(minutes=int(m.group(1))))

    # 아까
    if "아까" in text:
        return ("point", now - timedelta(hours=1))

    # 어제 / 하루 전
    if "어제" in text or "하루 전" in text:
        yesterday_start = today_start - timedelta(days=1)
        if any(k in text for k in ["평균", "최고", "최저", "전체", "통계", "얼마"]):
            return ("range", yesterday_start, today_start)
        return ("point", now - timedelta(days=1))

    # 오늘 + 통계 키워드
    if "오늘" in text and any(k in text for k in ["평균", "최고", "최저", "전체", "통계"]):
        return ("range", today_start, now)

    # 최근 N시간
    m = re.search(r'최근\s*(\d+)\s*시간', text)
    if m:
        return ("range", now - timedelta(hours=int(m.group(1))), now)

    return None


def _is_sensor_query(text):
    """에어컨·시스템 제어가 아닌 순수 센서 조회 질문이면 True"""
    aircon_kw = ["에어컨", "냉방", "제습", "파워", "전원", "켜줘", "꺼줘", "바람", "풍량", "설정해", "도로"]
    system_kw = ["cpu", "램", "메모리", "디스크", "시스템", "서버"]
    if any(k in text for k in aircon_kw + system_kw):
        return False
    sensor_kw = ["온도", "기온", "습도", "미세먼지", "먼지", "pm", "오염", "센서", "날씨"]
    return any(k in text.lower() for k in sensor_kw)


_FIELD_LABELS = {
    "temperature":  ("온도",    "°C"),
    "temp_avg":     ("평균 온도", "°C"),
    "temp_min":     ("최저 온도", "°C"),
    "temp_max":     ("최고 온도", "°C"),
    "humidity_avg": ("평균 습도", "%"),
    "pm2_5":        ("PM2.5",   ""),
    "pm2_5_avg":    ("평균 PM2.5", ""),
    "pm2_5_max":    ("최고 PM2.5", ""),
    "pm10":         ("PM10",    ""),
    "pm10_avg":     ("평균 PM10", ""),
    "pm10_max":     ("최고 PM10", ""),
}

def _humanize_result(result: dict) -> str:
    """도구 결과 dict를 한국어 레이블 문자열로 변환하여 LLM hallucination 방지"""
    if "error" in result:
        return f"오류: {result['error']}"
    if "message" in result:
        return result["message"]
    parts = []
    for k, v in result.items():
        label, unit = _FIELD_LABELS.get(k, (k, ""))
        parts.append(f"{label}: {v}{unit}")
    return ", ".join(parts)


def _dispatch_sensor_query(text, time_ctx):
    """시간 컨텍스트가 확정된 센서 쿼리를 DB에서 직접 조회"""
    is_dust    = any(k in text for k in ["미세먼지", "먼지", "pm", "오염"])
    is_humidity = any(k in text for k in ["습도"])
    kind        = time_ctx[0]

    if kind == "point":
        time_str = time_ctx[1].strftime("%Y-%m-%d %H:%M:%S")
        if is_dust:      return tool_get_dust_at({"time": time_str})
        if is_humidity:  return tool_get_humidity_at({"time": time_str})
        return tool_get_temperature_at({"time": time_str})

    # range
    dt_from, dt_to = time_ctx[1], time_ctx[2]
    if is_dust:
        rows = _db_query(
            "SELECT pm2_5, pm10 FROM dust_data"
            " WHERE timestamp >= %s AND timestamp < %s ORDER BY timestamp ASC",
            (dt_from, dt_to),
        )
        return _summarize_dust_rows(rows)
    else:
        rows = _db_query(
            "SELECT temperature, humidity FROM sensor_data"
            " WHERE timestamp >= %s AND timestamp < %s ORDER BY timestamp ASC",
            (dt_from, dt_to),
        )
        return _summarize_sensor_rows(rows)

def _detect_aircon_command(text: str):
    """명령형 에어컨 제어 감지. 반환: dict(mode,fan,temp) 또는 None"""
    import re as _re
    # 의문/추론문 제외
    if any(k in text for k in ["할까", "해야", "켜야", "꺼야", "될까", "어때", "괜찮", "좋을까"]):
        return None

    has_aircon    = any(k in text for k in ["에어컨", "냉방", "제습", "파워냉방", "에어콘"])
    has_control   = any(k in text for k in ["켜줘", "켜", "틀어줘", "틀어", "해줘", "줄래", "시작", "켜라"])
    has_mode_kw   = any(k in text for k in ["냉방", "제습", "파워냉방"])  # 단어만으로 제어 의도 확인
    has_off       = any(k in text for k in ["꺼줘", "끄기", "꺼 줘", "끄줘"])
    has_weather_feel = any(k in text for k in ["덥다", "더워", "시원하게", "습해", "눅눅"])
    has_temp_spec = bool(_re.search(r'\d+\s*도', text))

    # 꺼짐 명령
    if has_aircon and has_off:
        return {"mode": "off", "fan": "auto", "temp": 25}
    if has_aircon and _re.search(r'끄|꺼', text):
        return {"mode": "off", "fan": "auto", "temp": 25}

    # 켜기 명령: (에어컨 키워드 OR 날씨 체감) AND (제어 동사 OR 냉방/제습 명시 OR 온도 지정)
    if not ((has_aircon or has_weather_feel) and (has_control or has_mode_kw or has_temp_spec)):
        return None

    # 모드
    if any(k in text for k in ["파워냉방", "파워 냉방", "강력냉방", "파워"]):
        mode = "power_cool"
    elif any(k in text for k in ["제습", "습해", "습하다", "눅눅"]):
        mode = "dehumidify"
    else:
        mode = "cool"

    # 풍량
    if any(k in text for k in ["약풍", "약하게"]):
        fan = "weak"
    elif any(k in text for k in ["중풍", "중간"]):
        fan = "medium"
    elif any(k in text for k in ["강풍", "강하게"]):
        fan = "strong"
    else:
        fan = "auto"

    # 온도 (18~30)
    m = _re.search(r'(\d+)\s*도', text)
    temp = int(m.group(1)) if m and 18 <= int(m.group(1)) <= 30 else 25

    return {"mode": mode, "fan": fan, "temp": temp}


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


def _format_system_stats(result: dict) -> str:
    parts = []
    if "cpu_temp"         in result: parts.append(f"CPU 온도: {result['cpu_temp']}")
    if "cpu_usage_percent" in result: parts.append(f"CPU 사용률: {result['cpu_usage_percent']}%")
    if "ram_used_mb"      in result:
        parts.append(f"RAM: {result['ram_used_mb']}MB / {result['ram_total_mb']}MB ({result['ram_percent']}%)")
    if "disk_used_gb"     in result:
        parts.append(f"디스크: {result['disk_used_gb']}GB / {result['disk_total_gb']}GB ({result['disk_percent']}%)")
    return ", ".join(parts) + "입니다."


_CASUAL_RESPONSES = {
    frozenset(["고마워", "고맙다", "감사해", "감사합니다", "고맙습니다", "땡큐"]): "천만에요! 더 필요한 것 있으면 말씀해 주세요.",
    frozenset(["안녕", "하이", "반가워"]): "안녕하세요! 온도·습도·에어컨 제어 무엇이든 도와드릴게요.",
    frozenset(["뭘 할 수 있", "무엇을 할 수 있", "뭐 할 수 있", "어떤 기능", "기능이 뭐", "뭐가 돼"]):
        "온도·습도·미세먼지 조회, 에어컨 제어(냉방/제습/파워냉방), 시스템 상태 확인, 센서 이력 조회가 가능합니다.",
}

def _detect_casual(text: str):
    t = text.strip()
    for keys, reply in _CASUAL_RESPONSES.items():
        if any(t.startswith(k) or k in t for k in keys):
            return reply
    return None


def _aircon_advice() -> str:
    """현재 온도/습도 기반 에어컨 켜기 추천 여부"""
    t = tool_get_current_temperature({}).get("temperature", 25)
    h = tool_get_current_humidity({}).get("humidity_percent", 50)
    if t >= 28:
        return f"현재 온도 {t}°C로 더운 편이니 냉방을 켜는 걸 권장합니다."
    elif t >= 25:
        return f"현재 온도 {t}°C, 습도 {h}%로 다소 따뜻합니다. 필요하다면 냉방을 고려해 보세요."
    else:
        return f"현재 온도 {t}°C로 쾌적한 편이어서 에어컨이 필요하지 않을 것 같습니다."


def _ventilation_advice() -> str:
    dust = tool_get_current_dust({})
    if "pm2_5" not in dust:
        return "현재 미세먼지 데이터를 확인할 수 없어 환기 여부를 판단하기 어렵습니다."
    pm25 = dust["pm2_5"]
    if pm25 < 15:
        return f"현재 PM2.5가 {pm25}μg/m³로 좋음 수준입니다. 환기하셔도 좋습니다."
    if pm25 < 35:
        return f"현재 PM2.5가 {pm25}μg/m³로 보통 수준입니다. 잠깐 환기는 괜찮습니다."
    return f"현재 PM2.5가 {pm25}μg/m³로 높은 편입니다. 환기를 자제해 주세요."


def _parse_text_tool_call(content):
    """LLM이 tool_calls 대신 content에 JSON으로 출력한 도구 호출 파싱"""
    import re as _re
    if not content:
        return None
    try:
        data = json.loads(content.strip())
        if isinstance(data, dict) and "name" in data and "arguments" in data:
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    match = _re.search(r'\{.*?"name".*?"arguments".*?\}', content, _re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if "name" in data and "arguments" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    return None


@app.route("/api/chat", methods=["POST"])
def chat():
    body         = request.get_json(silent=True) or {}
    user_message = (body.get("message") or "").strip()
    history      = body.get("history") or []

    if not user_message:
        return jsonify({"error": "메시지가 비어 있습니다."}), 400

    # ── Fast path A: 시간 표현 + 센서 쿼리 ──
    time_ctx = _detect_time_context(user_message)
    if time_ctx and _is_sensor_query(user_message):
        result = _dispatch_sensor_query(user_message, time_ctx)
        return jsonify({"reply": _quick_format(result, is_current=False)})

    # ── Fast path B: 현재 센서 조회 ──
    sensor_type = _detect_current_sensor(user_message)
    if sensor_type:
        result = _fetch_current_sensors(sensor_type)
        return jsonify({"reply": _quick_format(result, is_current=True)})

    # ── Fast path C: 에어컨 명령 (Python 파싱) ──
    aircon_cmd = _detect_aircon_command(user_message)
    if aircon_cmd:
        result = tool_control_aircon(aircon_cmd)
        return jsonify({"reply": _format_aircon_result(result)})

    # ── Fast path D: 환기/창문 조언 ──
    if any(k in user_message for k in ["환기", "창문 열", "바깥 공기"]):
        return jsonify({"reply": _ventilation_advice()})

    # ── Fast path D2: 에어컨 켜야 할까? 추론 ──
    if any(k in user_message for k in ["에어컨 켜야", "냉방 켜야", "에어컨 켤까", "에어컨 킬까", "냉방 킬까"]):
        return jsonify({"reply": _aircon_advice()})

    # ── Fast path E: 대화 (인사/감사) ──
    casual = _detect_casual(user_message)
    if casual:
        return jsonify({"reply": casual})

    # ── Fast path G: 에어컨 오늘 횟수 쿼리 ──
    if any(k in user_message for k in ["몇 번", "몇번", "횟수", "얼마나 켰"]) and "에어컨" in user_message:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = _db_query("SELECT COUNT(*) AS cnt FROM history WHERE timestamp >= %s", (today_start,))
        cnt  = rows[0]["cnt"] if rows else 0
        reply = f"오늘 에어컨을 {cnt}번 제어했습니다." if cnt else "오늘 에어컨 동작 기록이 없습니다."
        return jsonify({"reply": reply})

    # ── Fast path F: 시스템 통계 (LLM 우회 — 불안정하므로 Python 직접 조회) ──
    _sys_direct_kw = [
        "메모리", "ram", "램", "디스크", "저장 공간", "저장공간",
        "서버 상태", "서버상태", "서버 좀", "서버 알려",
        "라즈베리파이", "라즈베리 파이",
        "시스템 상태", "시스템상태", "시스템 정보",
        "cpu 온도", "cpu온도", "cpu 사용",
    ]
    if any(k in user_message.lower() for k in _sys_direct_kw):
        result = tool_get_system_stats({})
        return jsonify({"reply": _format_system_stats(result)})

    # ── LLM path: 에어컨 제어·시스템 상태·추론·대화 ─────────────────────────
    now           = datetime.now()
    today         = now.strftime("%Y-%m-%d")
    yesterday     = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    sensor_ctx    = _build_today_context()
    system_content = (
        SYSTEM_PROMPT
        .replace("{today}", today)
        .replace("{yesterday}", yesterday)
        .replace("{sensor_context}", sensor_ctx)
    )
    messages = [{"role": "system", "content": system_content}]
    for h in history[-10:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
            messages.append({"role": h["role"], "content": h.get("content", "")})
    messages.append({"role": "user", "content": user_message})

    # Tool calling 루프 — TOOLS_LLM (3개) 만 사용
    for _ in range(3):
        response   = ollama.chat(model=OLLAMA_MODEL, messages=messages, tools=TOOLS_LLM, keep_alive=-1)
        msg        = response.message
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            fallback = _parse_text_tool_call(msg.content)
            if fallback:
                name    = fallback["name"]
                args    = fallback.get("arguments", {})
                handler = TOOL_HANDLERS.get(name)
                result  = handler(args) if handler else {"error": f"알 수 없는 도구: {name}"}
                messages.append({"role": "assistant", "content": ""})
                messages.append({"role": "tool",
                                  "content": json.dumps(result, ensure_ascii=False, default=str)})
                continue
            return jsonify({"reply": msg.content})

        messages.append(msg)
        for tc in tool_calls:
            name    = tc.function.name
            args    = tc.function.arguments if isinstance(tc.function.arguments, dict) else {}
            handler = TOOL_HANDLERS.get(name)
            result  = handler(args) if handler else {"error": f"알 수 없는 도구: {name}"}
            messages.append({"role": "tool",
                              "content": json.dumps(result, ensure_ascii=False, default=str)})

    final = ollama.chat(model=OLLAMA_MODEL, messages=messages, keep_alive=-1)
    return jsonify({"reply": final.message.content})


@app.route("/api/chat/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": OLLAMA_MODEL})


if __name__ == "__main__":
    print(f"Chatbot starting on http://0.0.0.0:5001 (model: {OLLAMA_MODEL})")
    app.run(host="0.0.0.0", port=5001, debug=False)
