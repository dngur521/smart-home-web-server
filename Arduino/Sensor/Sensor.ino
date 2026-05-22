#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <SoftwareSerial.h>

const char* ssid     = "Kam_2.4G";
const char* password = "1!kamkurgi521";

// PMS7003: TX→D7(GPIO13), RX→D6(GPIO12)
SoftwareSerial pmsSerial(13, 12);

// MQ-135 공기질 센서
// 핀: A0 (Wemos D1의 유일한 아날로그 핀)
// 연결: AOUT → A0 직결 (모듈 PCB에 부하저항+LM393 내장 → 별도 전압 분배기 불필요)
// 전원: VCC → 3.3V 또는 5V, GND → GND
// 주의: 최초 사용 시 24~48시간 예열 필요 (초기엔 ppm 값이 비정상적으로 높을 수 있음)
const int MQ135_PIN     = A0;
const int MQ135_SAMPLES = 10;   // 노이즈 감소를 위한 평균 샘플 수
const int MQ135_DELAY   = 50;   // 샘플 간 딜레이(ms)

ESP8266WebServer server(80);

struct PmsData {
  uint16_t pm1_0 = 0;
  uint16_t pm2_5 = 0;
  uint16_t pm10  = 0;
  bool valid     = false;
};

PmsData latestDust;

bool readPMS(PmsData& data) {
  if (pmsSerial.available() < 32) return false;

  while (pmsSerial.available() >= 32) {
    if (pmsSerial.read() != 0x42) continue;
    if (pmsSerial.peek() != 0x4D) continue;
    pmsSerial.read();

    uint8_t buf[30];
    pmsSerial.readBytes(buf, 30);

    uint16_t checksum = 0x42 + 0x4D;
    for (int i = 0; i < 28; i++) checksum += buf[i];
    uint16_t received = (buf[28] << 8) | buf[29];
    if (checksum != received) return false;

    data.pm1_0 = (buf[4] << 8) | buf[5];
    data.pm2_5 = (buf[6] << 8) | buf[7];
    data.pm10  = (buf[8] << 8) | buf[9];
    data.valid = true;
    return true;
  }
  return false;
}

// MQ-135 아날로그 값을 읽어 ppm으로 환산
// ADC 10회 평균 → ppm 근사 공식 적용 (데이터시트 기반 선형 근사)
// 반환값 기준: 0~400=좋음, 401~1000=보통, 1001~2000=나쁨, 2001+=위험
float readMQ135Ppm() {
  float total = 0;
  for (int i = 0; i < MQ135_SAMPLES; i++) {
    total += analogRead(MQ135_PIN);
    delay(MQ135_DELAY);
  }
  int avg = total / MQ135_SAMPLES;
  // 선형 근사: ADC 200 → 0ppm 기준점, 최대 5000ppm 스케일
  return max((avg - 200) * 5000.0 / 823.0, 0.0);
}

void setup() {
  Serial.begin(9600);
  pmsSerial.begin(9600);

  IPAddress local_IP(192, 168, 0, 38);
  IPAddress gateway(192, 168, 0, 1);
  IPAddress subnet(255, 255, 255, 0);
  WiFi.config(local_IP, gateway, subnet);

  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  WiFi.begin(ssid, password);
  Serial.print("WiFi 연결 중");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n연결 완료! IP: " + WiFi.localIP().toString());

  // /dust: 미세먼지(PMS7003) + 공기질(MQ-135) 통합 응답
  // 라즈베리파이 백엔드가 5분 정각마다 폴링
  server.on("/dust", []() {
    if (!latestDust.valid) {
      server.send(503, "application/json", "{\"status\":\"error\",\"message\":\"No data yet\"}");
      return;
    }
    float airPpm = readMQ135Ppm();
    String json = "{\"status\":\"success\","
                  "\"pm1_0\":"   + String(latestDust.pm1_0) + ","
                  "\"pm2_5\":"   + String(latestDust.pm2_5) + ","
                  "\"pm10\":"    + String(latestDust.pm10)  + ","
                  "\"air_ppm\":" + String(airPpm, 1)        + "}";
    server.send(200, "application/json", json);
  });

  server.begin();
  Serial.println("웹서버 시작. /dust 로 조회 가능");
}

void loop() {
  readPMS(latestDust);
  server.handleClient();
}
