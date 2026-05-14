#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <SoftwareSerial.h>

const char* ssid     = "Kam_2.4G";
const char* password = "1!kamkurgi521";

// PMS7003: TX→D7(GPIO13), RX→D6(GPIO12)
SoftwareSerial pmsSerial(13, 12);

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

void setup() {
  Serial.begin(9600);
  pmsSerial.begin(9600);

  WiFi.setSleepMode(WIFI_NONE_SLEEP);
  WiFi.begin(ssid, password);
  Serial.print("WiFi 연결 중");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n연결 완료! IP: " + WiFi.localIP().toString());

  server.on("/dust", []() {
    if (!latestDust.valid) {
      server.send(503, "application/json", "{\"status\":\"error\",\"message\":\"No data yet\"}");
      return;
    }
    String json = "{\"status\":\"success\","
                  "\"pm1_0\":" + String(latestDust.pm1_0) + ","
                  "\"pm2_5\":" + String(latestDust.pm2_5) + ","
                  "\"pm10\":"  + String(latestDust.pm10)  + "}";
    server.send(200, "application/json", json);
  });

  server.begin();
  Serial.println("웹서버 시작. /dust 로 조회 가능");
}

void loop() {
  readPMS(latestDust);
  server.handleClient();
}
