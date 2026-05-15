# -*- coding: utf-8 -*-
"""에어컨 예약 챗봇 기능 테스트 + DB 정리"""
import sys
import time

import mysql.connector
import requests

BASE_URL  = "http://localhost:5001"
DB_CONFIG = {
    "host": "127.0.0.1", "port": "3306",
    "user": "master", "password": "1234", "database": "smart_home",
}

def post(msg: str) -> str:
    try:
        r = requests.post(
            f"{BASE_URL}/api/chat",
            json={"message": msg},
            timeout=15,
        )
        return r.json().get("reply", "")
    except Exception as e:
        return f"[REQUEST_ERROR] {e}"

def db_max_id() -> int:
    conn   = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(MAX(id), 0) FROM aircon_schedule")
    val = cursor.fetchone()[0]
    cursor.close(); conn.close()
    return val

def db_pending_ids(min_id: int):
    conn   = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM aircon_schedule WHERE id > %s AND status='pending'", (min_id,))
    ids = [r[0] for r in cursor.fetchall()]
    cursor.close(); conn.close()
    return ids

def db_cleanup(min_id: int):
    conn   = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM aircon_schedule WHERE id > %s", (min_id,))
    deleted = cursor.rowcount
    conn.commit()
    cursor.close(); conn.close()
    print(f"\n[정리] aircon_schedule {deleted}건 삭제 완료 (id > {min_id})")


# ── 테스트 케이스 정의 ────────────────────────────────────────────
# (질문, 카테고리, must_have, must_not)
STATIC_TESTS = [
    # ── 예약 생성: 끄기 ──
    ("1시간 후에 에어컨 꺼줘",              "끄기예약",   ["예약"],          ["실패", "오류"]),
    ("2시간 후에 에어컨 꺼줘",              "끄기예약",   ["예약"],          ["실패", "오류"]),
    # ── 예약 생성: 켜기 기본 ──
    ("3시간 후에 에어컨 켜줘",              "켜기예약",   ["예약"],          ["실패", "오류"]),
    ("4시간 후에 에어컨 냉방 25도로 켜줘",   "온도예약",   ["예약", "25"],    ["실패", "오류"]),
    ("5시간 후에 에어컨 26도 냉방으로 켜줘", "온도예약",   ["예약", "26"],    ["실패", "오류"]),
    # ── 예약 생성: 모드 ──
    ("6시간 후에 제습 모드로 에어컨 켜줘",   "제습예약",   ["예약"],          ["실패", "오류"]),
    # ── 예약 생성: 풍량 ──
    ("7시간 후에 에어컨 약풍으로 켜줘",      "약풍예약",   ["예약"],          ["실패", "오류"]),
    ("8시간 후에 에어컨 강풍 냉방 켜줘",     "강풍예약",   ["예약"],          ["실패", "오류"]),
    # ── 예약 생성: 상대 시간 ──
    ("30분 후에 에어컨 꺼줘",               "분후예약",   ["예약"],          ["실패", "오류"]),
    ("90분 후에 에어컨 켜줘",               "분후예약",   ["예약"],          ["실패", "오류"]),
    # ── 예약 목록 조회 ──
    ("에어컨 예약 목록 보여줘",              "목록조회",   ["예약"],          ["오류"]),
    ("예약 확인해줘",                       "목록조회",   ["예약"],          ["오류"]),
    ("에어컨 예약 있어?",                   "목록조회",   ["예약"],          ["오류"]),
    ("에어컨 예약 현황 알려줘",              "목록조회",   ["예약"],          ["오류"]),
    # ── 즉시 제어 (예약 아님) ──
    ("에어컨 켜줘",                         "즉시제어",   [],                ["예약"]),
    ("에어컨 꺼줘",                         "즉시제어",   [],                ["예약"]),
    # ── 예약 + 추론 혼동 방지 ──
    ("에어컨 켜야 할까?",                   "추론제외",   [],                ["예약"]),
]


def run():
    pre_max_id = db_max_id()
    print(f"테스트 전 aircon_schedule max id: {pre_max_id}\n")

    pass_cnt = fail_cnt = err_cnt = 0

    def check(q: str, cat: str, must_have: list, must_not: list) -> bool:
        nonlocal pass_cnt, fail_cnt, err_cnt
        reply = post(q)
        if reply.startswith("[REQUEST_ERROR]"):
            err_cnt += 1
            print(f"⚠️ [{cat:<8}] {q}")
            print(f"         → {reply}")
            return False
        ok, reasons = True, []
        for kw in must_have:
            if kw not in reply:
                ok = False; reasons.append(f"'{kw}' 없음")
        for kw in must_not:
            if kw in reply:
                ok = False; reasons.append(f"'{kw}' 포함됨")
        sym = "✅" if ok else "❌"
        if ok: pass_cnt += 1
        else:   fail_cnt += 1
        print(f"{sym} [{cat:<8}] {q}")
        print(f"         → {reply[:90]}")
        if reasons:
            print(f"         ⚠ {', '.join(reasons)}")
        return ok

    # ── Phase 1: 정적 테스트 ──
    print("=" * 60)
    print("Phase 1: 예약 생성 / 목록 / 즉시 제어 테스트")
    print("=" * 60)
    for q, cat, mh, mn in STATIC_TESTS:
        check(q, cat, mh, mn)
        time.sleep(0.2)

    # ── Phase 2: 동적 취소 테스트 ──
    print("\n" + "=" * 60)
    print("Phase 2: 취소 테스트")
    print("=" * 60)

    pending = db_pending_ids(pre_max_id)

    if len(pending) >= 2:
        # 특정 번호 취소
        target_id = pending[0]
        check(f"{target_id}번 예약 취소해줘", "번호취소", ["취소"], ["실패"])
        time.sleep(0.2)
        # 남은 pending 중 하나 더 취소
        target_id2 = pending[1]
        check(f"{target_id2}번 예약 취소해줘", "번호취소", ["취소"], ["실패"])
        time.sleep(0.2)

    # pending 1개 남기고 "에어컨 예약 취소해줘" 테스트
    pending_now = db_pending_ids(pre_max_id)
    if len(pending_now) == 1:
        check("에어컨 예약 취소해줘", "단건취소", ["취소"], ["실패", "지정해"])
        time.sleep(0.2)
    elif len(pending_now) > 1:
        check("에어컨 예약 취소해줘", "다건취소", ["지정", "번"], ["실패"])
        time.sleep(0.2)
    else:
        check("에어컨 예약 취소해줘", "취소없음", ["없"], ["실패"])
        time.sleep(0.2)

    # 모두 취소 후 목록이 비어있어야 함
    remaining = db_pending_ids(pre_max_id)
    for rid in remaining:
        post(f"{rid}번 예약 취소해줘")
        time.sleep(0.1)

    check("에어컨 예약 목록 보여줘", "빈목록", ["없"], ["오류"])

    # ── 결과 요약 ──
    total = pass_cnt + fail_cnt + err_cnt
    print(f"\n결과: PASS {pass_cnt}/{total}  FAIL {fail_cnt}  ERR {err_cnt}"
          f"  ({pass_cnt * 100 // total if total else 0}%)")

    db_cleanup(pre_max_id)


if __name__ == "__main__":
    run()
