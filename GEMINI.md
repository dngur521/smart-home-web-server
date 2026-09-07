# GEMINI.md

Antigravity(Gemini) 에이전트를 위한 스마트홈 웹 서버 프로젝트 규칙 및 가이드라인입니다.
(기존 `CLAUDE.md`의 모든 규칙 및 하드웨어 특이사항을 계승합니다.)

## 1. 프로젝트 개요 (Project Overview)
- **플랫폼**: 라즈베리파이 5 (Raspberry Pi 5) 기반 스마트홈 백엔드 시스템
- **메인 백엔드**: `app.py` 단일 파일 Flask 서버 (포트 5000)
- **프론트엔드**: React + Vite (`new-smart-app`), 빌드 결과물 `dist/`를 Flask가 직접 서빙
- **AI 챗봇**: `chatbot.py` (포트 5001, Google Gemini 2.5 Flash, Python-first 아키텍처)
- **CCTV 및 오디오**: Logitech C270 (`/dev/cctv`, mjpg_streamer 8080), WebRTC 오디오 (`audio_webrtc.py`, 포트 8083)
- **DB / 캐시**: MariaDB (`smart_home`, user: master/1234), Redis (토큰 관리, 6379)
- **프로세스 관리**: `pm2` (`backend`, `chatbot`, `cctv`, `audio-rtc`, `ttyd`)

---

## 2. 하드웨어 및 특이사항 (Hardware & Quirks)
- **Arduino 업로드 시 필수**:
  - `app.py`가 시리얼 포트(`/dev/arduino`, `/dev/ttyUSB0`)를 상시 점유하고 있으므로, **업로드 전 반드시 `pm2 stop backend`** -> 업로드 완료 후 `pm2 start backend`.
- **시리얼 안정성 (`select` 5초 타임아웃)**:
  - pyserial의 `timeout`은 USB 재연결 시 OS 레벨 블로킹을 막지 못하므로 `select()`를 통한 5초 하드 타임아웃 필수.
- **서보 모터 (CH340 `/dev/ttyUSB0`)**:
  - Pan(좌우): 핀 9, Tilt(상하): 핀 10.
  - **하드웨어 배선 반전으로 백엔드(`app.py`)에서 up/down 방향을 swap**하여 전송 (`up` -> `down`, `down` -> `up`).
- **TENT6000 빛센서**:
  - Arduino A0 핀 연결. 에어컨 디스플레이 빛 감지(threshold ≥ 20 = ON).
- **Wemos D1 (ESP8266) + PMS7003**:
  - 미세먼지 센서. 정적 IP `192.168.0.38` 고정.
  - 백엔드가 5분 정각마다 `/dust` 엔드포인트 폴링하여 DB 저장.
- **DHT22 온습도 센서**:
  - GPIO 26. `use_pulseio=False` 필수.
  - Pi 5 환경에서 `lgpio`는 venv에 심볼릭 링크 필수.
- **S20 플래시 제어**:
  - Galaxy S20 Termux 서버 (`S20_HOST`, 기본값 `192.168.0.13:8282`) HTTP 프록시.

---

## 3. 백엔드 개발 규칙 (Backend Conventions)
- **단일 파일 구조**: 백엔드 로직은 `app.py`에 유지.
- **DB 헬퍼 함수 일원화**: 모든 MariaDB 접근은 커넥션 풀 및 아래 5개 공통 헬퍼를 사용:
  - `_db_fetchone(query, params)`
  - `_db_fetchall(query, params)`
  - `_db_insert(query, params)`
  - `_db_execute(query, params)`
  - `_record_history(command, response)`
- **타임스탬프**: DB에는 KST 로컬 시간으로 저장되며, API 응답 시 `format_rows_datetime()`을 통해 `+09:00` 포맷 적용.
- **인증**:
  - Access Token: 30분 만료 JWT, HttpOnly 쿠키 (`access_token_cookie`)
  - Refresh Token: Redis 저장 7일 만료, 매 요청 시 로테이션
- **챗봇 시리얼 직접 접근 금지**:
  - `chatbot.py`는 시리얼 포트를 직접 열지 않고 `app.py`의 내부 API(`http://localhost:5000/api/internal/...`)를 통해 제어.

---

## 4. 노션 정리 포맷 규칙
사용자가 **"노션 정리용 작성해줘"** 라고 요청할 경우 코드블록 없이 아래 raw 마크다운 형식으로 출력:

- [영역] 작업 제목
    - **문제**: 어떤 문제가 있었는지 (한 줄)
    - **원인**: 왜 발생했는지 (한 줄)
    - **해결**:
        - 해결 방법 항목 1
        - 해결 방법 항목 2

*(영역 태그: `[프론트]`, `[백엔드]`, `[프론트 + 백엔드]`, `[아두이노]`, `[챗봇]`)*
