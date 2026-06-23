# Smart Home Web Server — 백엔드

라즈베리파이 5 기반 스마트홈 시스템의 Flask 백엔드 서버.  
에어컨 IR 제어(Arduino 시리얼), DHT22 온습도 수집, PMS7003 미세먼지 수집, 사용자 인증, 시스템 모니터링 API를 제공한다.

프론트엔드: [new-smart-app](https://github.com/dngur521/new-smart-app) (React + Vite)

---

## 기술 스택

- **Python 3** + **Flask** — 웹 서버 (app.py: 5000, chatbot.py: 5001, audio_webrtc.py: 8083)
- **MySQL** — 센서 데이터 / 제어 이력 / 사용자 저장
- **Redis** — Refresh Token 저장 (7일 TTL, 로테이션)
- **adafruit-circuitpython-dht** — DHT22 온습도 센서 (GPIO 26, `use_pulseio=False`)
- **requests** — Wemos D1 미세먼지 센서 HTTP 폴링
- **pyserial** — Arduino 시리얼 통신 (`/dev/arduino`, udev 고정 심볼릭 링크)
- **bcrypt / PyJWT** — 비밀번호 해싱 및 JWT 인증
- **APScheduler** — 에어컨 예약 실행 (매분 정각 cron) + 예약 재부팅 (매일 지정 시각 cron)
- **google-genai** (gemini-2.5-flash) — AI 챗봇 (`chatbot.py`, 무료 API)

---

## 하드웨어 구성

| 장치 | 역할 |
| ---- | ---- |
| 라즈베리파이 5 | 백엔드 서버 실행 |
| DHT22 | 온습도 센서 (GPIO 26, 물리 핀 37) |
| Arduino (USB) | 에어컨 IR 제어 (`/dev/arduino`, 영구 연결 + Lock) |
| TENT6000 빛센서 | 에어컨 켜짐 감지 (Arduino A0, threshold ≥ 20) |
| Logitech C270 (USB) | CCTV 웹캠 (`/dev/cctv`, udev 고정 심볼릭 링크, MJPG 1280x960 30fps) |
| Arduino 클론 CH340 (USB) | Pan-Tilt 서보 제어 (`/dev/ttyUSB0`, `Arduino/Servo/Servo.ino`) |
| Wemos D1 (ESP8266) + PMS7003 | WiFi 미세먼지 센서 모듈 (`Arduino/Sensor/Sensor.ino`) |
| Galaxy S20 (Termux) | 플래시라이트 제어 (`S20_HOST`, `~/torch-server.py`) |

Wemos D1은 독립적인 WiFi HTTP 서버로 동작하며, 라즈베리파이 백엔드가 5분 정각마다 `/dust` 엔드포인트를 폴링해서 DB에 저장한다. IP는 `192.168.0.38`로 정적 고정(`Sensor.ino` 내 `WiFi.config()`).

---

## 실행 전 준비

| 항목            | 설정                                                                |
| --------------- | ------------------------------------------------------------------- |
| MariaDB         | `127.0.0.1:3306`, DB: `smart_home`, user: `master` / `1234`         |
| Redis           | `localhost:6379`                                                    |
| 환경 변수       | `SECRET_KEY` 필수 (미설정 시 불안전한 기본값 사용)                  |
| `DUST_SENSOR_URL` | Wemos D1 IP 주소 (예: `http://192.168.0.38/dust`)                 |
| 프론트엔드 빌드 | `new-smart-app`에서 `npm run build` 후 `dist/`를 이 디렉토리에 복사 |
| smartctl sudo   | `/etc/sudoers.d/smartctl`에 `kam5 ALL=(ALL) NOPASSWD: /usr/sbin/smartctl` 추가 |
| lgpio           | `sudo apt install python3-lgpio` 후 venv에 심볼릭 링크 (Pi 5 필수) |

테이블(`history`, `sensor_data`, `users`, `dust_data`, `aircon_schedule`)은 서버 시작 시 자동 생성된다.

`cctv_config.json`은 `POST /api/system/cctv/config` 최초 호출 시 자동 생성된다. 없으면 기본값 `1280x960 @ 30fps`로 동작한다.

`reboot_schedule.json`은 `POST /api/system/reboot-schedule` 최초 호출 시 자동 생성된다. 없으면 `enabled: false`(비활성)로 동작한다.

---

## 실행

```bash
# pm2로 실행 (권장, systemd 자동시작 등록됨)
pm2 start venv/bin/python3 --name backend -- app.py
pm2 restart backend   # 재시작
pm2 logs backend      # 로그 확인

# 포그라운드 실행
python3 app.py
```

서버는 `http://0.0.0.0:5000`에서 실행되며, `/api` 외 모든 경로는 `dist/index.html`로 서빙된다.

---

## 환경 변수

| 변수              | 설명                                           | 기본값                                        |
| ----------------- | ---------------------------------------------- | --------------------------------------------- |
| `SECRET_KEY`      | JWT 서명 키                                    | `your_super_secret_key_change_me` (변경 필수) |
| `FRONTEND_ORIGIN` | CORS 허용 Origin (개발 시 React dev 서버 주소) | `http://localhost:5173`                       |
| `COOKIE_SECURE`   | HTTPS 환경에서 `true`로 설정                   | `false`                                       |
| `DUST_SENSOR_URL` | Wemos D1 미세먼지 센서 주소                    | `http://192.168.0.38/dust`                    |
| `GEMINI_API_KEY`  | Gemini AI 챗봇 API 키 (`.env` 파일에 저장)     | 필수 (`.env.example` 참고)                    |
| `S20_HOST`        | Galaxy S20 Termux 플래시 서버 주소             | `192.168.0.13:8282` (변경 필수)               |

---

## API 엔드포인트

| 메서드 | 경로                                  | 인증 | 설명                                        |
| ------ | ------------------------------------- | :--: | ------------------------------------------- |
| POST   | `/api/auth/register`                  |  ✗   | 회원가입                                    |
| POST   | `/api/auth/login`                     |  ✗   | 로그인 (HttpOnly 쿠키 발급)                 |
| POST   | `/api/auth/refresh`                   |  ✗   | 토큰 갱신 (쿠키 로테이션)                   |
| POST   | `/api/auth/logout`                    |  ✗   | 로그아웃 (쿠키 삭제)                        |
| GET    | `/api/user/profile`                   |  ✓   | 내 정보 조회                                |
| PUT    | `/api/user/update-password`           |  ✓   | 비밀번호 변경                               |
| DELETE | `/api/user/delete`                    |  ✓   | 계정 삭제                                   |
| POST   | `/api/arduino/send-command`           |  ✓   | Arduino 명령 전송 + 이력 저장               |
| GET    | `/api/arduino/aircon-status`          |  ✓   | 빛센서 기반 에어컨 ON/OFF 조회              |
| GET    | `/api/arduino/dht-sensor`             |  ✓   | 실시간 온습도 조회                          |
| GET    | `/api/arduino/dht-history`            |  ✓   | 온습도 이력 (`?page=&limit=`)               |
| GET    | `/api/arduino/dht-history/today`      |  ✓   | 오늘 온습도 전체 (ASC)                      |
| GET    | `/api/arduino/dht-history/seek`       |  ✓   | timestamp 기준 페이지 번호 반환             |
| GET    | `/api/arduino/aircon-history`         |  ✓   | 에어컨 제어 이력 (`?page=&limit=`)          |
| GET    | `/api/arduino/aircon-history/seek`    |  ✓   | timestamp 기준 페이지 번호 반환             |
| GET    | `/api/arduino/dust-sensor`            |  ✓   | 최신 미세먼지 1건 조회                      |
| GET    | `/api/arduino/dust-history`           |  ✓   | 미세먼지 이력 (`?page=&limit=`) / 범위 조회 (`?from=&to=`, ISO 8601 UTC)  |
| GET    | `/api/arduino/dust-history/today`     |  ✓   | 오늘 미세먼지 전체 (ASC)                    |
| GET    | `/api/arduino/environment-history`    |  ✓   | 온습도+미세먼지 5분 버킷 통합 이력          |
| POST   | `/api/servo/move`                     |  ✓   | PTZ 서보 이동 (`{"direction":"left/right/up/down"}`) |
| GET    | `/api/system/stats`                   |  ✓   | CPU / RAM / 디스크 / 네트워크 통계          |
| GET    | `/api/system/cctv/config`             |  ✓   | 현재 CCTV 해상도/FPS 및 지원 옵션 조회      |
| POST   | `/api/system/cctv/config`             |  ✓   | CCTV 해상도/FPS 변경 (mjpg_streamer 재시작) |
| GET    | `/api/system/reboot-schedule`         |  ✓   | 예약 재부팅 설정 조회 (`enabled`, `hour`, `minute`) |
| POST   | `/api/system/reboot-schedule`         |  ✓   | 예약 재부팅 설정 (`{"enabled":true,"hour":4,"minute":0}`) — `reboot_schedule.json` 저장, APScheduler 즉시 갱신 |
| POST   | `/api/schedule/aircon`                |  ✓   | 에어컨 예약 등록 (`action`, `scheduled_at`, `temperature`, `mode`, `wind`) — 과거 시각 400, 동일 시각 중복 409 |
| GET    | `/api/schedule/aircon`                |  ✓   | 에어컨 예약 목록 전체 조회                  |
| DELETE | `/api/schedule/aircon/:id`            |  ✓   | 특정 예약 취소 (status → cancelled)         |
| DELETE | `/api/schedule/aircon/bulk`           |  ✓   | 예약 일괄 삭제 (`status`, `older_than_days` 필터) |
| POST   | `/api/torch`                          |  ✓   | S20 플래시라이트 제어 (`{"action":"on"/"off"}`)   |
| POST   | `/rtc-offer`                          |  ✓   | WebRTC 시그널링 (SDP offer → answer, 포트 8083)   |
| GET    | `/api/internal/aircon-status`         |  ✗   | chatbot.py 전용 내부 API (localhost만 허용)        |
| POST   | `/api/internal/arduino/send`          |  ✗   | chatbot.py 전용 내부 API (localhost만 허용)        |
| POST   | `/api/internal/torch`                 |  ✗   | chatbot.py 전용 내부 API (localhost만 허용)        |
| POST   | `/api/internal/servo/move`            |  ✗   | chatbot.py 전용 내부 API (localhost만 허용)        |

---

## 인증 방식

JWT를 **HttpOnly 쿠키**로 관리한다.

- **Access Token**: HS256, 30분 만료
- **Refresh Token**: UUID, Redis 저장, 7일 만료, 사용 시마다 로테이션
- 신규 가입 계정은 `is_active = FALSE`로 생성되며, 관리자가 DB에서 직접 활성화해야 로그인 가능

---

## 데이터베이스 스키마

| 테이블           | 컬럼                                                                  |
| ---------------- | --------------------------------------------------------------------- |
| `users`          | `id`, `username` (unique), `password_hash`, `is_active`, `created_at` |
| `sensor_data`    | `id`, `temperature`, `humidity`, `timestamp`                          |
| `history`        | `id`, `command`, `response`, `timestamp`                              |
| `dust_data`      | `id`, `pm1_0`, `pm2_5`, `pm10`, `timestamp`                           |
| `aircon_schedule`| `id`, `action` (on/off), `scheduled_at`, `temperature`, `mode` (cool/dry), `wind` (auto/low/mid/high), `status` (pending/done/cancelled), `created_at` |

---

## 센서 데이터 수집

백그라운드 스레드 2개가 매 5분 정각(`HH:00`, `HH:05`, `HH:10`, …)에 실행된다.

- **DHT22**: 라즈베리파이 GPIO 26 (물리 핀 37)에서 직접 읽어 `sensor_data`에 저장. 읽기 실패 시 2초 간격 5회 재시도
- **PMS7003**: Wemos D1의 `/dust` 엔드포인트를 HTTP GET 후 `dust_data`에 저장

---

## 시스템 모니터링

`monitor.sh`를 실행하면 터미널에서 CPU / NVMe 온도, 메모리, 네트워크, 디스크 I/O를 확인할 수 있다.  
필요 패키지: `sysstat`, `bc`, `smartmontools`

```bash
bash monitor.sh
```

## 서비스 자동 시작 (systemd)

재부팅 후 기동 순서 보장을 위해 두 systemd 설정이 적용되어 있다.

| 파일 | 변경 내용 |
| ---- | --------- |
| `/etc/systemd/system/pm2-kam5.service` | `After=network.target mariadb.service redis.service` + `Wants=mariadb.service redis.service` |
| `/etc/systemd/system/nginx.service.d/override.conf` | `After=network-online.target` + `Wants=network-online.target` |

- **pm2**: MariaDB·Redis 준비 완료 후 시작 → backend DB 연결 실패 방지
- **nginx**: DNS 완전 준비 후 시작 → upstream 도메인(`*.duckdns.org`) 해석 실패 방지

설정 변경 시 `sudo systemctl daemon-reload` 필수.

## 서비스 구성 (pm2)

| 이름 | 역할 | 포트 |
| ---- | ---- | ---- |
| `backend` | Flask API 서버 | 5000 |
| `chatbot` | AI 챗봇 서버 (`chatbot.py`, Google Gemini API, 비전 분석 포함) | 5001 |
| `cctv` | mjpg_streamer (Logitech C270, MJPG 1280x960 30fps) | 8080 |
| `audio-rtc` | WebRTC 오디오 스트림 (`audio_webrtc.py`, Opus, ~50ms 딜레이) | 8083 |
| `ttyd` | 웹 콘솔 (`--writable --base-path /console-ws`) | 7681 |

> **Arduino 업로드 시 주의**: app.py가 `/dev/arduino`를 영구 점유하므로, 업로드 전 반드시 `pm2 stop backend` → 업로드 완료 후 `pm2 start backend`.
