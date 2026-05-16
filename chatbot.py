# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types as genai_types
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
APP_INTERNAL_URL = "http://localhost:5000"
GEMINI_MODEL    = "gemini-2.5-flash"
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
_gemini_client  = genai.Client(api_key=GEMINI_API_KEY)
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

def _is_aircon_on():
    """TENT6000 빛센서로 에어컨 켜짐 여부 판별. app.py 내부 API 경유. 실패 시 이력 기반으로 fallback."""
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

def _send_internal(command: str) -> str:
    """app.py 내부 API를 통해 Arduino 명령 전송. 응답 문자열 반환."""
    r = requests.post(
        f"{APP_INTERNAL_URL}/api/internal/arduino/send",
        json={"command": command},
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("response", "")

def tool_control_aircon(args):
    mode = args.get("mode", "off")
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

# --- 도구 스키마 (레거시, fast path 미적용 경로에서 참조) ---
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

_GEMINI_TOOLS = [genai_types.Tool(
    function_declarations=[
        genai_types.FunctionDeclaration(
            name="control_aircon",
            description="에어컨을 제어합니다. 전원 끄기, 냉방, 제습, 파워냉방 지원.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "mode": genai_types.Schema(
                        type=genai_types.Type.STRING,
                        enum=["off", "cool", "dehumidify", "power_cool"],
                        description="off=전원끄기, cool=냉방, dehumidify=제습, power_cool=파워냉방",
                    ),
                    "fan": genai_types.Schema(
                        type=genai_types.Type.STRING,
                        enum=["weak", "medium", "strong", "auto"],
                        description="약풍=weak 중풍=medium 강풍=strong 자동=auto (기본 auto)",
                    ),
                    "temp": genai_types.Schema(
                        type=genai_types.Type.INTEGER,
                        description="희망 온도 18~30. 미언급 시 25.",
                    ),
                },
                required=["mode"],
            ),
        ),
        genai_types.FunctionDeclaration(
            name="get_system_stats",
            description="라즈베리파이 시스템 상태(CPU 온도/사용률, RAM, 디스크)를 조회합니다.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={},
            ),
        ),
        genai_types.FunctionDeclaration(
            name="get_aircon_history",
            description="최근 에어컨 제어 이력을 조회합니다.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "limit": genai_types.Schema(
                        type=genai_types.Type.INTEGER,
                        description="조회할 이력 수 (기본 10)",
                    ),
                },
            ),
        ),
    ]
)]

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

    # 날씨 통합 쿼리 → 온습도 동시 반환
    if any(k in text for k in ["날씨 어때", "날씨가 어때", "날씨는 어때"]):
        return "temp_humidity"

    has_temp  = any(k in text for k in ["온도", "기온", "몇도", "몇 도", "덥냐", "덥나", "춥냐", "춥나", "실온"])
    has_hum   = any(k in text for k in ["습도", "습해", "습하", "눅눅", "건조해", "건조한", "건조하다", "습도 높"])
    has_dust  = any(k in text.lower() for k in ["미세먼지", "먼지", "pm", "오염", "대기질", "공기질", "공기"])

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

    # 한/두/세 시간 전 (한글 숫자)
    _ko = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6}
    m = re.search(r'(한|두|세|네|다섯|여섯)\s*시간\s*전', text)
    if m:
        return ("point", now - timedelta(hours=_ko[m.group(1)]))

    # N분 전
    m = re.search(r'(\d+)\s*분\s*전', text)
    if m:
        return ("point", now - timedelta(minutes=int(m.group(1))))

    # 아까
    if "아까" in text:
        return ("point", now - timedelta(hours=1))

    # 방금 (약 10분 전)
    if "방금" in text:
        return ("point", now - timedelta(minutes=10))

    # N일 전 (숫자)
    m = re.search(r'(\d+)\s*일\s*전', text)
    if m:
        return ("point", now - timedelta(days=int(m.group(1))))

    # 이틀 전 / 그저께 / 엊그제 / 그제
    if any(k in text for k in ["이틀 전", "이틀전", "그저께", "엊그제", "그제"]):
        return ("point", now - timedelta(days=2))

    # 사흘 전
    if any(k in text for k in ["사흘 전", "사흘전"]):
        return ("point", now - timedelta(days=3))

    # 일주일 전 / 한 주 전
    if any(k in text for k in ["일주일 전", "일주일전", "1주일 전", "1주 전", "한 주 전", "한주전"]):
        return ("point", now - timedelta(weeks=1))

    # 지난 달 / 저번 달 / 전달
    if any(k in text for k in ["지난 달", "지난달", "저번 달", "저번달", "전달", "전 달", "지난월"]):
        y, mo = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        return ("range", datetime(y, mo, 1), datetime(now.year, now.month, 1))

    # 이번 달
    if any(k in text for k in ["이번 달", "이번달", "이번월"]):
        return ("range", datetime(now.year, now.month, 1), now)

    # 지난 주 / 저번 주
    if any(k in text for k in ["지난 주", "지난주", "저번 주", "저번주", "전주", "전 주"]):
        mon = today_start - timedelta(days=now.weekday())
        return ("range", mon - timedelta(weeks=1), mon)

    # 이번 주
    if any(k in text for k in ["이번 주", "이번주"]):
        mon = today_start - timedelta(days=now.weekday())
        return ("range", mon, now)

    # 어제 / 하루 전
    if "어제" in text or "하루 전" in text:
        yesterday_start = today_start - timedelta(days=1)
        if any(k in text for k in ["평균", "최고", "최저", "전체", "통계", "얼마", "변화", "추이"]):
            return ("range", yesterday_start, today_start)
        return ("point", now - timedelta(days=1))

    # 오늘 + 통계/변화 키워드
    if "오늘" in text and any(k in text for k in ["평균", "최고", "최저", "전체", "통계", "변화", "추이"]):
        return ("range", today_start, now)

    # 최근 N시간
    m = re.search(r'최근\s*(\d+)\s*시간', text)
    if m:
        return ("range", now - timedelta(hours=int(m.group(1))), now)

    # 최근 N일
    m = re.search(r'최근\s*(\d+)\s*일', text)
    if m:
        return ("range", now - timedelta(days=int(m.group(1))), now)

    return None


def _is_sensor_query(text):
    """에어컨·시스템 제어가 아닌 순수 센서 조회 질문이면 True"""
    aircon_kw = ["에어컨", "냉방", "제습", "파워", "전원", "켜줘", "꺼줘", "바람", "풍량", "설정해", "도로"]
    system_kw = ["cpu", "램", "메모리", "디스크", "시스템", "서버"]
    if any(k in text for k in aircon_kw + system_kw):
        return False
    sensor_kw = ["온도", "기온", "습도", "습해", "습하", "미세먼지", "먼지", "pm", "오염", "대기질", "공기질", "공기", "센서", "날씨", "데이터", "통계", "변화"]
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
    tl = text.lower()
    is_dust     = any(k in tl for k in ["미세먼지", "먼지", "pm", "오염", "대기질", "공기질", "공기"])
    is_humidity = any(k in text for k in ["습도", "습해", "습하", "눅눅"])
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

# ── 에어컨 예약 ────────────────────────────────────────────────────

def _parse_schedule_datetime(text: str):
    """텍스트에서 예약 시각 추출. 반환: datetime or None"""
    import re as _re
    now = datetime.now()

    # N시간 후
    m = _re.search(r'(\d+)\s*시간\s*후', text)
    if m:
        return now + timedelta(hours=int(m.group(1)))

    # 한/두/세 시간 후 (한글 숫자)
    _ko_hr = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6}
    m = _re.search(r'(한|두|세|네|다섯|여섯)\s*시간\s*후', text)
    if m:
        return now + timedelta(hours=_ko_hr[m.group(1)])

    # N분 후
    m = _re.search(r'(\d+)\s*분\s*후', text)
    if m:
        return now + timedelta(minutes=int(m.group(1)))

    # 자정 = 다음 날 00:00
    if "자정" in text:
        tomorrow = now.date() + timedelta(days=1)
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0)

    has_tomorrow = "내일" in text
    base = (now + timedelta(days=1)).date() if has_tomorrow else now.date()
    is_pm = any(k in text for k in ["오후", "저녁", "밤", "야간"])
    is_am = any(k in text for k in ["오전", "아침"])

    # N시 M분
    m = _re.search(r'(\d{1,2})시\s*(\d{1,2})분', text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if is_pm and hour < 12:
            hour += 12
        dt = datetime(base.year, base.month, base.day, hour % 24, minute)
        if not has_tomorrow and dt <= now:
            dt += timedelta(days=1)
        return dt

    # N시 (시간 단위 표현 '시간' 은 제외)
    m = _re.search(r'(\d{1,2})시(?!\s*간)', text)
    if m:
        hour = int(m.group(1))
        if is_pm and hour < 12:
            hour += 12
        dt = datetime(base.year, base.month, base.day, hour % 24, 0)
        if not has_tomorrow and dt <= now:
            dt += timedelta(days=1)
        return dt

    return None


def _detect_schedule_intent(text: str):
    """
    예약 관련 의도 감지.
    반환: ('create', args_dict) | ('list', {}) | ('cancel', args_dict) | None
    """
    import re as _re

    # 목록 조회
    if any(k in text for k in ["예약 목록", "예약 보여", "예약 확인", "예약된", "예약 있어", "예약 알려", "예약 현황",
                                "예약돼", "예약됐", "예약 언제"]):
        return ("list", {})

    # 취소
    if "예약 취소" in text or ("취소" in text and "예약" in text):
        m = _re.search(r'(\d+)\s*번', text)
        sid = int(m.group(1)) if m else None
        return ("cancel", {"id": sid})

    # 생성: 미래 시각 + 에어컨 키워드 + 제어 동사
    scheduled_at = _parse_schedule_datetime(text)
    if scheduled_at is None:
        return None
    if not any(k in text for k in ["에어컨", "냉방", "제습", "에어콘"]):
        return None
    if not any(k in text for k in ["켜줘", "켜", "틀어줘", "꺼줘", "꺼", "예약해", "예약"]):
        return None

    # 끄기
    if any(k in text for k in ["꺼줘", "끄기", "꺼 줘"]):
        return ("create", {"action": "off", "scheduled_at": scheduled_at})

    # 켜기 — 모드/온도/풍량
    mode = "dry" if any(k in text for k in ["제습"]) else "cool"

    if any(k in text for k in ["약풍", "약하게"]):
        wind = "low"
    elif any(k in text for k in ["중풍", "중간"]):
        wind = "mid"
    elif any(k in text for k in ["강풍", "강하게", "파워냉방", "파워"]):
        wind = "high"
    else:
        wind = "auto"

    m = _re.search(r'(\d+)\s*도', text)
    temp = int(m.group(1)) if m and 18 <= int(m.group(1)) <= 30 else 25

    return ("create", {
        "action": "on",
        "scheduled_at": scheduled_at,
        "temperature": temp,
        "mode": mode,
        "wind": wind,
    })


def tool_create_aircon_schedule(args: dict) -> dict:
    action       = args.get("action")
    scheduled_at = args.get("scheduled_at")
    temperature  = args.get("temperature")
    mode         = args.get("mode")
    wind         = args.get("wind")
    try:
        conn   = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
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


def tool_list_aircon_schedules(_args: dict) -> dict:
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


def _detect_aircon_command(text: str):
    """명령형 에어컨 제어 감지. 반환: dict(mode,fan,temp) 또는 None"""
    import re as _re
    # 예약 시간 표현이 있으면 즉시 제어가 아님
    if _parse_schedule_datetime(text) is not None:
        return None
    # 상태 확인 문맥 제외 ("켜져 있어?", "꺼져 있어?" / "작동 중이야" 등)
    if any(k in text for k in ["켜져 있", "꺼져 있", "켜져있", "꺼져있",
                                "켜있", "꺼있", "작동 중", "가동 중", "돌고 있"]):
        return None
    # 의문/추론/권고문 제외
    if any(k in text for k in ["할까", "해야", "켜야", "꺼야", "될까", "어때", "괜찮", "좋을까",
                                 "켤까", "꺼볼까", "켜볼까", "도 돼", "됩니까", "됩니다", "어떨까",
                                 "필요해", "필요없", "필요 없", "좋겠어", "좋겠다", "게 좋", "야 해"]):
        return None

    # 이력/정보 조회 패턴 — 명령이 아닌 쿼리
    if any(k in text for k in ["언제", "마지막", "기록", "이력", "횟수", "몇 번", "몇번", "최근 제어", "최근 명령"]):
        return None
    # 과거형 끄기 상태 (꺼진 시간 등)
    if any(k in text for k in ["꺼진", "꺼짐", "꺼졌"]):
        return None

    has_aircon    = any(k in text for k in ["에어컨", "냉방", "제습", "파워냉방", "에어콘", "파워"])
    has_control   = any(k in text for k in ["켜줘", "켜", "틀어줘", "틀어", "해줘", "줄래", "시작", "켜라", "가동", "작동",
                                             "약풍", "중풍", "강풍", "자동풍", "켤 수", "파워모드"]) or \
                    bool(_re.search(r'\bon\b', text, _re.IGNORECASE))
    has_mode_kw   = any(k in text for k in ["냉방", "제습", "파워냉방", "파워모드"])
    has_off       = any(k in text for k in ["꺼줘", "끄기", "꺼 줘", "끄줘", "종료", "정지", "중지", "끊어",
                                             "전원 내려", "오프"]) or \
                    bool(_re.search(r'\boff\b', text, _re.IGNORECASE))
    has_weather_feel = any(k in text for k in ["덥다", "더워", "시원하게", "습해", "눅눅", "더운데", "덥네", "더운"])
    has_temp_spec = bool(_re.search(r'\d+\s*도', text))

    # 꺼짐 명령 (꺼진/꺼짐/꺼있/꺼져있 등은 위 exclusion에서 이미 제외됨)
    if has_aircon and has_off:
        return {"mode": "off", "fan": "auto", "temp": 25}
    if has_aircon and _re.search(r'끄|꺼', text):
        return {"mode": "off", "fan": "auto", "temp": 25}

    # 켜기 명령: (에어컨 키워드 OR 날씨 체감) AND (제어 동사 OR 냉방/제습 명시 OR 온도 지정 OR 날씨체감+에어컨 동시)
    if not ((has_aircon or has_weather_feel) and
            (has_control or has_mode_kw or has_temp_spec or (has_weather_feel and has_aircon))):
        return None

    # 변경/조절 의도 감지 — 부분 파라미터만 지정 시 현재 상태를 보존
    is_modify = any(k in text for k in ["바꿔", "변경", "올려", "내려", "높여", "낮춰", "조절"])

    # 모드
    if any(k in text for k in ["파워냉방", "파워 냉방", "강력냉방", "파워"]):
        mode = "power_cool"
    elif any(k in text for k in ["제습", "습해", "습하다", "눅눅"]):
        mode = "dehumidify"
    elif any(k in text for k in ["냉방"]):
        mode = "cool"
    else:
        mode = None if is_modify else "cool"

    # 풍량
    if any(k in text for k in ["약풍", "약하게"]):
        fan = "weak"
    elif any(k in text for k in ["중풍", "중간"]):
        fan = "medium"
    elif any(k in text for k in ["강풍", "강하게"]):
        fan = "strong"
    elif any(k in text for k in ["자동풍"]):
        fan = "auto"
    else:
        fan = None if is_modify else "auto"

    # 온도 (18~30)
    m = _re.search(r'(\d+)\s*도', text)
    if m and 18 <= int(m.group(1)) <= 30:
        temp = int(m.group(1))
    else:
        temp = None if is_modify else 25

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
    frozenset(["뭘 할 수 있", "무엇을 할 수 있", "뭐 할 수 있", "어떤 기능", "기능이 뭐", "뭐가 돼",
               "도움말", "도움 받", "뭐 도움", "기능 소개", "무엇을 물어", "사용법", "도와줘"]):
        "온도·습도·미세먼지 조회, 에어컨 제어(냉방/제습/파워냉방), 에어컨 예약(N시간 후/N시에 켜기·끄기), 시스템 상태 확인, 센서 이력 조회가 가능합니다.",
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

    # ── Fast path H: 에어컨 예약 ──
    sched_intent = _detect_schedule_intent(user_message)
    if sched_intent:
        intent_type, args = sched_intent
        if intent_type == "create":
            result = tool_create_aircon_schedule(args)
            return jsonify({"reply": _format_schedule_create(result)})
        elif intent_type == "list":
            result = tool_list_aircon_schedules(args)
            return jsonify({"reply": _format_schedule_list(result)})
        elif intent_type == "cancel":
            result = tool_cancel_aircon_schedule(args)
            return jsonify({"reply": _format_schedule_cancel(result)})

    # ── Fast path C: 에어컨 명령 (Python 파싱) ──
    aircon_cmd = _detect_aircon_command(user_message)
    if aircon_cmd:
        # None 파라미터는 현재 에어컨 상태로 채움 (부분 변경 요청 처리)
        if any(v is None for v in aircon_cmd.values()):
            current = _get_current_aircon_state()
            if current:
                for k in ("mode", "fan", "temp"):
                    if aircon_cmd[k] is None:
                        aircon_cmd[k] = current[k]
        if aircon_cmd["mode"] is None: aircon_cmd["mode"] = "cool"
        if aircon_cmd["fan"]  is None: aircon_cmd["fan"]  = "auto"
        if aircon_cmd["temp"] is None: aircon_cmd["temp"] = 25
        result = tool_control_aircon(aircon_cmd)
        return jsonify({"reply": _format_aircon_result(result)})

    # ── Fast path D: 환기/창문 조언 ──
    if any(k in user_message for k in ["환기", "창문 열", "창문 여", "바깥 공기"]):
        return jsonify({"reply": _ventilation_advice()})

    # ── Fast path D2: 에어컨 관련 추론·조언 (명령 아님) ──
    _D2_KW = [
        "에어컨 켜야", "냉방 켜야", "에어컨 켤까", "냉방 켤까", "에어컨 킬까", "냉방 킬까",
        "냉방 필요", "에어컨 필요",
        "냉방 꺼야", "에어컨 꺼야",
        "에어컨 끄는 게", "냉방 끄는 게",
        "에어컨 켜는 게", "냉방 켜는 게",
        "에어컨 켜볼까", "냉방 켜볼까",
        "냉방 강도", "에어컨 강도",
        "에어컨 안 켜도", "냉방 안 켜도",
        "냉방 돌려야", "에어컨 돌려야",
        "에어컨 세게", "에어컨 온도 얼마",
        "시원한 편", "쾌적한 편",
        # 환경 상태 발언 → 센서 기반 응답
        "쾌적해", "쾌적하다", "지금 쾌적", "날씨 쾌적",
        "시원해", "덥지 않", "안 더워",
        "냉방 좀", "에어컨 켤 필요",
        "켜도 돼", "켜도 될까", "켜도 됩니까",
    ]
    if any(k in user_message for k in _D2_KW):
        return jsonify({"reply": _aircon_advice()})

    # ── Fast path E: 대화 (인사/감사) ──
    casual = _detect_casual(user_message)
    if casual:
        return jsonify({"reply": casual})

    # ── Fast path G: 에어컨 오늘 횟수 쿼리 ──
    if any(k in user_message for k in ["몇 번", "몇번", "횟수", "얼마나 켰"]) and \
       any(k in user_message for k in ["에어컨", "냉방"]):
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = _db_query("SELECT COUNT(*) AS cnt FROM history WHERE timestamp >= %s", (today_start,))
        cnt  = rows[0]["cnt"] if rows else 0
        reply = f"오늘 에어컨을 {cnt}번 제어했습니다." if cnt else "오늘 에어컨 동작 기록이 없습니다."
        return jsonify({"reply": reply})

    # ── Fast path G2: 에어컨 제어 이력 조회 ──
    _hist_q_kw = ["이력", "기록", "언제", "마지막", "켠 시간", "꺼진 시간", "제어한 게", "최근 제어"]
    if any(k in user_message for k in ["에어컨", "냉방"]) and \
       any(k in user_message for k in _hist_q_kw) and \
       not any(k in user_message for k in ["예약돼", "예약된", "예약 있", "예약됐", "예약 언제"]):
        rows = _db_query(
            "SELECT command, response, timestamp FROM history ORDER BY id DESC LIMIT 5"
        )
        if not rows:
            return jsonify({"reply": "에어컨 제어 기록이 없습니다."})
        lines = []
        for r in rows:
            ts = r.get("timestamp", "")
            if hasattr(ts, "strftime"):
                ts = ts.strftime("%Y-%m-%d %H:%M")
            cmd = r.get("command", "")
            lines.append(f"{ts}: {cmd}")
        return jsonify({"reply": "에어컨 최근 제어 기록:\n" + "\n".join(lines)})

    # ── Fast path F: 시스템 통계 (LLM 우회 — 불안정하므로 Python 직접 조회) ──
    _sys_direct_kw = [
        "메모리", "ram", "램", "디스크", "저장 공간", "저장공간",
        "서버 상태", "서버상태", "서버 좀", "서버 알려",
        "라즈베리파이", "라즈베리 파이",
        "시스템 상태", "시스템상태", "시스템 정보", "시스템 전체",
        "cpu 온도", "cpu온도", "cpu 사용",
        "자원 상태", "리소스", "resource",
    ]
    if any(k in user_message.lower() for k in _sys_direct_kw):
        result = tool_get_system_stats({})
        return jsonify({"reply": _format_system_stats(result)})

    # ── Fast path I: 에어컨/냉방 켜짐 상태 조회 ──
    _state_kw = ["켜져", "꺼져", "작동 중", "상태 어", "켜있", "꺼있",
                 "동작 중", "가동 중", "돌고 있", "지금 어때", "현재 상태", "어때"]
    if any(k in user_message for k in ["에어컨", "냉방"]) and \
       any(k in user_message for k in _state_kw):
        is_on = _is_aircon_on()
        reply = "에어컨이 현재 켜져 있는 것으로 보입니다." if is_on else "에어컨이 현재 꺼져 있는 것으로 보입니다."
        return jsonify({"reply": reply})

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
    # 대화 히스토리를 Gemini 형식으로 변환
    gemini_history = []
    for h in history[-10:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
            role = "model" if h["role"] == "assistant" else "user"
            gemini_history.append(genai_types.Content(
                role=role,
                parts=[genai_types.Part.from_text(text=h.get("content", ""))],
            ))

    chat_session = _gemini_client.chats.create(
        model=GEMINI_MODEL,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_content,
            tools=_GEMINI_TOOLS,
        ),
        history=gemini_history,
    )
    response = chat_session.send_message(user_message)

    # Tool calling 루프
    for _ in range(3):
        parts    = (response.candidates[0].content.parts if response.candidates else []) or []
        fn_calls = [p for p in parts if p.function_call and p.function_call.name]

        if not fn_calls:
            text = "".join(p.text for p in parts if hasattr(p, "text") and p.text)
            return jsonify({"reply": text})

        tool_responses = []
        for p in fn_calls:
            fc      = p.function_call
            handler = TOOL_HANDLERS.get(fc.name)
            args    = dict(fc.args) if fc.args else {}
            result  = handler(args) if handler else {"error": f"알 수 없는 도구: {fc.name}"}
            tool_responses.append(
                genai_types.Part.from_function_response(
                    name=fc.name,
                    response={"result": json.dumps(result, ensure_ascii=False, default=str)},
                )
            )
        response = chat_session.send_message(tool_responses)

    parts = (response.candidates[0].content.parts if response.candidates else []) or []
    text  = "".join(p.text for p in parts if hasattr(p, "text") and p.text)
    return jsonify({"reply": text or "처리 중 오류가 발생했습니다."})


@app.route("/api/chat/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": GEMINI_MODEL})


if __name__ == "__main__":
    print(f"Chatbot starting on http://0.0.0.0:5001 (model: {GEMINI_MODEL})")
    app.run(host="0.0.0.0", port=5001, debug=False)
