#include <Servo.h>

Servo pan;
Servo tilt;

const int SPEED   = 10;
const int STEP_MS = 20;

void step(Servo &servo, int val) {
  servo.write(val);
  delay(STEP_MS);
  servo.write(90);
}

void setup() {
  Serial.begin(9600);
  pan.attach(9);
  tilt.attach(10);
  pan.write(90);
  tilt.write(90);
}

void loop() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();

  if (!line.startsWith("MOVE ")) return;

  String dir = line.substring(5);

  if      (dir == "left")  step(pan,  90 - SPEED);
  else if (dir == "right") step(pan,  90 + SPEED);
  else if (dir == "up")    step(tilt, 90 + SPEED);
  else if (dir == "down")  step(tilt, 90 - SPEED);
}
