# Smart Home Web Server — 백엔드

라즈베리파이 2 기반 스마트홈 시스템의 Flask 백엔드 서버.  
에어컨 IR 제어(Arduino 시리얼), DHT22 온습도 수집, 사용자 인증, 시스템 모니터링 API를 제공한다.

프론트엔드: [new-smart-app](https://github.com/dngur521/new-smart-app) (React + Vite)

---

## 기술 스택

- **Python 3** + **Flask** — 웹 서버
- **MySQL** — 센서 데이터 / 제어 이력 / 사용자 저장
- **Redis** — Refresh Token 저장 (7일 TTL, 로테이션)
- **Adafruit_DHT** — DHT22 온습도 센서 (GPIO 26)
- **pyserial** — Arduino 시리얼 통신 (`/dev/ttyUSB0`)
- **bcrypt / PyJWT** — 비밀번호 해싱 및 JWT 인증

---

## 실행 전 준비

| 항목            | 설정                                                                |
| --------------- | ------------------------------------------------------------------- |
| MySQL           | `127.0.0.1:3306`, DB: `smart_home`, user: `master` / `1234`         |
| Redis           | `localhost:6379`                                                    |
| 환경 변수       | `SECRET_KEY` 필수 (미설정 시 불안전한 기본값 사용)                  |
| 프론트엔드 빌드 | `new-smart-app`에서 `npm run build` 후 `dist/`를 이 디렉토리에 복사 |
| smartctl sudo   | `sudo visudo`로 `smartctl` passwordless 허용 (SSD 온도 조회용)      |

테이블(`history`, `sensor_data`, `users`)은 서버 시작 시 자동 생성된다.

---

## 실행

```bash
# 백그라운드 실행 (로그: ./log/app_YYYYMMDD_HHMMSS.log)
bash start_server.sh

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

---

## API 엔드포인트

| 메서드 | 경로                          | 인증 | 설명                               |
| ------ | ----------------------------- | :--: | ---------------------------------- |
| POST   | `/api/auth/register`          |  ✗   | 회원가입                           |
| POST   | `/api/auth/login`             |  ✗   | 로그인 (HttpOnly 쿠키 발급)        |
| POST   | `/api/auth/refresh`           |  ✗   | 토큰 갱신 (쿠키 로테이션)          |
| POST   | `/api/auth/logout`            |  ✗   | 로그아웃 (쿠키 삭제)               |
| GET    | `/api/user/profile`           |  ✓   | 내 정보 조회                       |
| PUT    | `/api/user/update-password`   |  ✓   | 비밀번호 변경                      |
| DELETE | `/api/user/delete`            |  ✓   | 계정 삭제                          |
| POST   | `/api/arduino/send-command`   |  ✓   | Arduino 명령 전송 + 이력 저장      |
| GET    | `/api/arduino/dht-sensor`     |  ✓   | 실시간 온습도 조회                 |
| GET    | `/api/arduino/dht-history`    |  ✓   | 온습도 이력 (`?page=&limit=`)      |
| GET    | `/api/arduino/aircon-history` |  ✓   | 에어컨 제어 이력 (`?page=&limit=`) |
| GET    | `/api/system/stats`           |  ✓   | CPU / RAM / 디스크 / 네트워크 통계 |

---

## 인증 방식

JWT를 **HttpOnly 쿠키**로 관리한다.

- **Access Token**: HS256, 30분 만료
- **Refresh Token**: UUID, Redis 저장, 7일 만료, 사용 시마다 로테이션
- 신규 가입 계정은 `is_active = FALSE`로 생성되며, 관리자가 DB에서 직접 활성화해야 로그인 가능

---

## 데이터베이스 스키마

| 테이블        | 컬럼                                                                  |
| ------------- | --------------------------------------------------------------------- |
| `users`       | `id`, `username` (unique), `password_hash`, `is_active`, `created_at` |
| `sensor_data` | `id`, `temperature`, `humidity`, `timestamp`                          |
| `history`     | `id`, `command`, `response`, `timestamp`                              |

---

## 센서 데이터 수집

백그라운드 스레드가 매 5분 정각(`HH:00`, `HH:05`, `HH:10`, …)에 DHT22 센서를 읽어 `sensor_data`에 저장한다.

---

## 시스템 모니터링

`monitor.sh`를 실행하면 터미널에서 CPU / SSD 온도, 메모리, 네트워크, 디스크 I/O를 확인할 수 있다.  
필요 패키지: `sysstat`, `bc`, `smartmontools`

```bash
bash monitor.sh
```
