/*
 * ============================================================
 *  Smart E-Waste Dustbin — ESP32-CAM Firmware
 *  RHYX M21-45 SENSOR  VERSION
 *  ( AP mode for IP viewing & dynamic configuration)
 *  Author - Surya Mani Bajpai 
 * ============================================================
 */

#include "esp_camera.h"
#include "img_converters.h"
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ESP32Servo.h>
#include <Preferences.h>   // ← added for persistent settings

// ==================== USER CONFIGURATION (Defaults) ====================
// These are now stored in NVS and can be changed via the AP portal.
String wifiSSID     = "Vimal";
String wifiPassword = "1234567890";
String serverIp     = "10.175.166.221";
const int   SERVER_PORT   = 5000;
// Servo angles
const int SERVO_OPEN_ANGLE  = 90;
const int SERVO_CLOSE_ANGLE = 0;
const int LID_OPEN_DURATION = 10000;

// Servo GPIO
const int SERVO_PIN = 14;

// Flash LED
const int FLASH_PIN = 4;

// ============================================================

// ---------- AI Thinker ESP32-CAM Pin Map ----------
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

Servo lidServo;
WebServer server(80);

String pendingRequestId = "";
bool photoQueued = false;

// NVS handle for persistent settings
Preferences prefs;

// ==================== CAMERA INIT ====================

bool initCamera() {

  camera_config_t config;

  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;

  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;

  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;

  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;

  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;

  // RHYX M21-45 Stable Settings
  config.xclk_freq_hz = 10000000;

  // SENSOR DOES NOT SUPPORT HARDWARE JPEG
  config.pixel_format = PIXFORMAT_RGB565;

  config.frame_size   = FRAMESIZE_QVGA;

  config.jpeg_quality = 15;

  config.fb_count     = 1;

  esp_err_t err = esp_camera_init(&config);

  if (err != ESP_OK) {

    Serial.printf("Camera init failed: 0x%x\n", err);

    return false;
  }

  sensor_t* s = esp_camera_sensor_get();

  if (s) {

    Serial.printf("Sensor PID: 0x%x\n", s->id.PID);

    s->set_brightness(s, 1);
    s->set_saturation(s, 0);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_exposure_ctrl(s, 1);
    s->set_gain_ctrl(s, 1);
  }

  return true;
}

// ==================== SERVO HELPERS ====================

void openLid() {

  Serial.println(" Opening lid...");

  lidServo.write(SERVO_OPEN_ANGLE);

  delay(500);
}

void closeLid() {

  Serial.println(" Closing lid...");

  lidServo.write(SERVO_CLOSE_ANGLE);

  delay(500);
}

// ==================== PHOTO CAPTURE & UPLOAD ====================

bool captureAndUpload(const String& requestId) {

  Serial.println(" Warming up camera...");

  for (int i = 0; i < 4; i++) {

    camera_fb_t* warmup = esp_camera_fb_get();

    if (warmup) esp_camera_fb_return(warmup);

    delay(150);
  }

  digitalWrite(FLASH_PIN, HIGH);

  delay(100);

  camera_fb_t* fb = esp_camera_fb_get();

  digitalWrite(FLASH_PIN, LOW);

  if (!fb) {

    Serial.println(" Camera capture failed");

    return false;
  }

  uint8_t* jpg_buf = NULL;

  size_t jpg_len = 0;

  // SOFTWARE JPEG CONVERSION
  bool converted = frame2jpg(fb, 80, &jpg_buf, &jpg_len);

  esp_camera_fb_return(fb);

  if (!converted) {

    Serial.println(" JPEG conversion failed");

    return false;
  }

  Serial.printf(" JPEG converted: %u bytes\n", jpg_len);

  String boundary = "----ESP32Boundary7MA4YWxkTrZu0gW";

  String serverUrl =
    "http://" + serverIp + ":" +       // ← now uses the String variable
    String(SERVER_PORT) + "/api/upload-photo";

  String bodyStart =
    "--" + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"request_id\"\r\n\r\n" +
    requestId + "\r\n"
    "--" + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"photo\"; filename=\"capture.jpg\"\r\n"
    "Content-Type: image/jpeg\r\n\r\n";

  String bodyEnd =
    "\r\n--" + boundary + "--\r\n";

  int totalLength =
    bodyStart.length() +
    jpg_len +
    bodyEnd.length();

  HTTPClient http;

  http.begin(serverUrl);

  http.addHeader(
    "Content-Type",
    "multipart/form-data; boundary=" + boundary
  );

  http.addHeader(
    "Content-Length",
    String(totalLength)
  );

  http.setTimeout(15000);

  uint8_t* fullBody =
    (uint8_t*)malloc(totalLength);

  if (!fullBody) {

    Serial.println(" Not enough heap");

    free(jpg_buf);

    return false;
  }

  int pos = 0;

  memcpy(
    fullBody + pos,
    bodyStart.c_str(),
    bodyStart.length()
  );

  pos += bodyStart.length();

  memcpy(
    fullBody + pos,
    jpg_buf,
    jpg_len
  );

  pos += jpg_len;

  memcpy(
    fullBody + pos,
    bodyEnd.c_str(),
    bodyEnd.length()
  );

  free(jpg_buf);

  int httpCode =
    http.POST(fullBody, totalLength);

  free(fullBody);

  if (httpCode == 200) {

    Serial.println(" Photo uploaded successfully!");

    Serial.println(http.getString());

    http.end();

    return true;
  }

  else {

    Serial.printf(" Upload failed: %d\n", httpCode);

    http.end();

    return false;
  }
}

// ==================== HTTP ENDPOINTS (original) ====================

void handleTrigger() {

  if (!server.hasArg("request_id")) {

    server.send(
      400,
      "application/json",
      "{\"error\":\"Missing request_id\"}"
    );

    return;
  }

  String reqId = server.arg("request_id");

  Serial.println("\n Trigger received!");

  server.send(
    200,
    "application/json",
    "{\"success\":true}"
  );

  pendingRequestId = reqId;

  photoQueued = true;
}

void handleStatus() {

  String ip = WiFi.localIP().toString();

  server.send(
    200,
    "application/json",
    "{\"status\":\"online\",\"ip\":\"" + ip + "\"}"
  );
}

void handleTestOpen() {

  openLid();

  delay(3000);

  closeLid();

  server.send(200, "text/plain", "OK");
}

void handleNotFound() {

  server.send(404, "text/plain", "Not found");
}

// ==================== REPORT STATUS ====================

void reportLidStatus(
  const String& status,
  const String& reqId
) {

  HTTPClient http;

  String url =
    "http://" + serverIp + ":" +        //  uses the String variable
    String(SERVER_PORT) +
    "/api/lid-status";

  http.begin(url);

  http.addHeader(
    "Content-Type",
    "application/json"
  );

  String body =
    "{\"status\":\"" + status +
    "\",\"request_id\":\"" + reqId + "\"}";

  int code = http.POST(body);

  Serial.printf(
    "Lid status '%s' reported → HTTP %d\n",
    status.c_str(),
    code
  );

  http.end();
}

// ==================== NEW: AP CONFIGURATION HANDLERS ====================

void handleRoot() {
  // Show current IPs and a configuration form
  String staIP = WiFi.localIP().toString();
  String apIP  = WiFi.softAPIP().toString();

  String html = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>E-Waste Bin Setup</title>
  <style>
    body { font-family: Arial; margin: 2em; background: #f0f0f0; }
    .box { background: white; padding: 2em; border-radius: 10px; max-width: 400px; margin: auto; }
    input, button { width: 100%; padding: 10px; margin: 8px 0; box-sizing: border-box; }
    button { background: #007BFF; color: white; border: none; font-size: 16px; cursor: pointer; }
  </style>
</head>
<body>
  <div class="box">
    <h2>E-Waste Bin Config</h2>
    <p><b>STA IP:</b> )rawliteral" + staIP + R"rawliteral(</p>
    <p><b>AP IP:</b> )rawliteral" + apIP + R"rawliteral(</p>
    <form action="/save" method="POST">
      <label>Wi-Fi SSID</label>
      <input type="text" name="ssid" value=")rawliteral" + wifiSSID + R"rawliteral(" required>
      <label>Wi-Fi Password</label>
      <input type="password" name="pass" value=")rawliteral" + wifiPassword + R"rawliteral(" required>
      <label>Server IP</label>
      <input type="text" name="server_ip" value=")rawliteral" + serverIp + R"rawliteral(" required pattern="\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}">
      <button type="submit">Save & Reboot</button>
    </form>
  </div>
</body>
</html>
)rawliteral";

  server.send(200, "text/html", html);
}

void handleSave() {
  // Read POST parameters
  if (server.hasArg("ssid") && server.hasArg("pass") && server.hasArg("server_ip")) {
    wifiSSID     = server.arg("ssid");
    wifiPassword = server.arg("pass");
    serverIp     = server.arg("server_ip");

    // Save to NVS
    prefs.putString("ssid",      wifiSSID);
    prefs.putString("password",  wifiPassword);
    prefs.putString("server_ip", serverIp);

    server.send(200, "text/html", "<!DOCTYPE html><html><body>"
                "<h2>Settings saved. Rebooting...</h2>"
                "<p>Reconnect to the device after it restarts.</p>"
                "</body></html>");

    delay(1000);
    ESP.restart();
  } else {
    server.send(400, "text/plain", "Missing fields");
  }
}

// ==================== SETUP ====================

void setup() {

  Serial.begin(115200);

  Serial.println("\n\n Smart E-Waste Bin Booting...");

  pinMode(FLASH_PIN, OUTPUT);
  digitalWrite(FLASH_PIN, LOW);

  // Load stored settings (or use defaults)
  prefs.begin("ewaste", false);
  wifiSSID     = prefs.getString("ssid",      wifiSSID);
  wifiPassword = prefs.getString("password",  wifiPassword);
  serverIp     = prefs.getString("server_ip", serverIp);
  // If nothing was stored, save the defaults for next time
  if (!prefs.isKey("ssid")) {
    prefs.putString("ssid",      wifiSSID);
    prefs.putString("password",  wifiPassword);
    prefs.putString("server_ip", serverIp);
  }

  // Servo
  ESP32PWM::allocateTimer(0);
  lidServo.setPeriodHertz(50);
  lidServo.attach(SERVO_PIN, 500, 2400);
  lidServo.write(SERVO_CLOSE_ANGLE);
  Serial.println(" Servo initialised (closed)");

  // Camera
  if (!initCamera()) {
    Serial.println(" Camera failed — rebooting in 5s");
    delay(5000);
    ESP.restart();
  }
  Serial.println(" Camera initialised");

  // ---------- WiFi (AP + STA simultaneous) ----------
  Serial.print(" Setting up Wi-Fi STA + AP... ");
  WiFi.mode(WIFI_AP_STA);

  // Start AP (fixed SSID/password for configuration portal)
  WiFi.softAP("E-Waste-Bin", "12345678");
  Serial.print(" AP IP: ");
  Serial.println(WiFi.softAPIP());

  // Connect to stored network (STA)
  Serial.print(" Connecting to Wi-Fi: ");
  Serial.println(wifiSSID);
  WiFi.begin(wifiSSID.c_str(), wifiPassword.c_str());

  int retries = 0;
  while (WiFi.status() != WL_CONNECTED && retries < 30) {
    delay(500);
    Serial.print(".");
    retries++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n Wi-Fi connected!");
    Serial.print(" STA IP Address: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\n Wi-Fi STA failed  device still reachable via AP for setup.");
  }

  // HTTP Routes (original + new)
  server.on("/trigger",   HTTP_GET,  handleTrigger);
  server.on("/status",    HTTP_GET,  handleStatus);
  server.on("/test-open", HTTP_GET,  handleTestOpen);
  server.on("/",          HTTP_GET,  handleRoot);      // ← config page
  server.on("/save",      HTTP_POST, handleSave);      // ← save new settings
  server.onNotFound(handleNotFound);

  server.begin();
  Serial.println(" HTTP server started (AP + STA)");
}

// ==================== MAIN LOOP ====================

void loop() {

  server.handleClient();

  if (photoQueued) {

    photoQueued = false;

    String reqId = pendingRequestId;

    openLid();

    reportLidStatus("open", reqId);

    Serial.println(" Waiting for deposit...");

    delay(3000);

    bool uploaded =
      captureAndUpload(reqId);

    if (!uploaded) {

      Serial.println(" Retrying upload...");

      delay(1000);

      captureAndUpload(reqId);
    }

    int remainOpen =
      LID_OPEN_DURATION - 5000;

    if (remainOpen > 0) {

      delay(remainOpen);
    }

    closeLid();

    reportLidStatus("closed", reqId);

    Serial.println(" Cycle complete!");
  }

  delay(10);
}






/*
 * ============================================================
 *  Smart E-Waste Dustbin — ESP32-CAM Firmware
 *  RHYX M21-45 SENSOR  VERSION (older without Ap ip viewing)
 * ============================================================
 

#include "esp_camera.h"
#include "img_converters.h"
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ESP32Servo.h>

// ==================== USER CONFIGURATION ====================

const char* WIFI_SSID     = "Vimal";
const char* WIFI_PASSWORD = "1234567890";

const char* SERVER_IP     = "10.175.166.221";
const int   SERVER_PORT   = 5000;

// Servo angles
const int SERVO_OPEN_ANGLE  = 90;
const int SERVO_CLOSE_ANGLE = 0;
const int LID_OPEN_DURATION = 10000;

// Servo GPIO
const int SERVO_PIN = 14;

// Flash LED
const int FLASH_PIN = 4;

// ============================================================

// ---------- AI Thinker ESP32-CAM Pin Map ----------
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

Servo lidServo;
WebServer server(80);

String pendingRequestId = "";
bool photoQueued = false;

// ==================== CAMERA INIT ====================

bool initCamera() {

  camera_config_t config;

  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;

  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;

  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;

  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;

  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;

  // RHYX M21-45 Stable Settings
  config.xclk_freq_hz = 10000000;

  // SENSOR DOES NOT SUPPORT HARDWARE JPEG
  config.pixel_format = PIXFORMAT_RGB565;

  config.frame_size   = FRAMESIZE_QVGA;

  config.jpeg_quality = 15;

  config.fb_count     = 1;

  esp_err_t err = esp_camera_init(&config);

  if (err != ESP_OK) {

    Serial.printf("Camera init failed: 0x%x\n", err);

    return false;
  }

  sensor_t* s = esp_camera_sensor_get();

  if (s) {

    Serial.printf("Sensor PID: 0x%x\n", s->id.PID);

    s->set_brightness(s, 1);
    s->set_saturation(s, 0);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_exposure_ctrl(s, 1);
    s->set_gain_ctrl(s, 1);
  }

  return true;
}

// ==================== SERVO HELPERS ====================

void openLid() {

  Serial.println(" Opening lid...");

  lidServo.write(SERVO_OPEN_ANGLE);

  delay(500);
}

void closeLid() {

  Serial.println(" Closing lid...");

  lidServo.write(SERVO_CLOSE_ANGLE);

  delay(500);
}

// ==================== PHOTO CAPTURE & UPLOAD ====================

bool captureAndUpload(const String& requestId) {

  Serial.println(" Warming up camera...");

  for (int i = 0; i < 4; i++) {

    camera_fb_t* warmup = esp_camera_fb_get();

    if (warmup) esp_camera_fb_return(warmup);

    delay(150);
  }

  digitalWrite(FLASH_PIN, HIGH);

  delay(100);

  camera_fb_t* fb = esp_camera_fb_get();

  digitalWrite(FLASH_PIN, LOW);

  if (!fb) {

    Serial.println(" Camera capture failed");

    return false;
  }

  uint8_t* jpg_buf = NULL;

  size_t jpg_len = 0;

  // SOFTWARE JPEG CONVERSION
  bool converted = frame2jpg(fb, 80, &jpg_buf, &jpg_len);

  esp_camera_fb_return(fb);

  if (!converted) {

    Serial.println(" JPEG conversion failed");

    return false;
  }

  Serial.printf(" JPEG converted: %u bytes\n", jpg_len);

  String boundary = "----ESP32Boundary7MA4YWxkTrZu0gW";

  String serverUrl =
    "http://" + String(SERVER_IP) + ":" +
    String(SERVER_PORT) + "/api/upload-photo";

  String bodyStart =
    "--" + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"request_id\"\r\n\r\n" +
    requestId + "\r\n"
    "--" + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"photo\"; filename=\"capture.jpg\"\r\n"
    "Content-Type: image/jpeg\r\n\r\n";

  String bodyEnd =
    "\r\n--" + boundary + "--\r\n";

  int totalLength =
    bodyStart.length() +
    jpg_len +
    bodyEnd.length();

  HTTPClient http;

  http.begin(serverUrl);

  http.addHeader(
    "Content-Type",
    "multipart/form-data; boundary=" + boundary
  );

  http.addHeader(
    "Content-Length",
    String(totalLength)
  );

  http.setTimeout(15000);

  uint8_t* fullBody =
    (uint8_t*)malloc(totalLength);

  if (!fullBody) {

    Serial.println(" Not enough heap");

    free(jpg_buf);

    return false;
  }

  int pos = 0;

  memcpy(
    fullBody + pos,
    bodyStart.c_str(),
    bodyStart.length()
  );

  pos += bodyStart.length();

  memcpy(
    fullBody + pos,
    jpg_buf,
    jpg_len
  );

  pos += jpg_len;

  memcpy(
    fullBody + pos,
    bodyEnd.c_str(),
    bodyEnd.length()
  );

  free(jpg_buf);

  int httpCode =
    http.POST(fullBody, totalLength);

  free(fullBody);

  if (httpCode == 200) {

    Serial.println(" Photo uploaded successfully!");

    Serial.println(http.getString());

    http.end();

    return true;
  }

  else {

    Serial.printf(" Upload failed: %d\n", httpCode);

    http.end();

    return false;
  }
}

// ==================== HTTP ENDPOINTS ====================

void handleTrigger() {

  if (!server.hasArg("request_id")) {

    server.send(
      400,
      "application/json",
      "{\"error\":\"Missing request_id\"}"
    );

    return;
  }

  String reqId = server.arg("request_id");

  Serial.println("\n Trigger received!");

  server.send(
    200,
    "application/json",
    "{\"success\":true}"
  );

  pendingRequestId = reqId;

  photoQueued = true;
}

void handleStatus() {

  String ip = WiFi.localIP().toString();

  server.send(
    200,
    "application/json",
    "{\"status\":\"online\",\"ip\":\"" + ip + "\"}"
  );
}

void handleTestOpen() {

  openLid();

  delay(3000);

  closeLid();

  server.send(200, "text/plain", "OK");
}

void handleNotFound() {

  server.send(404, "text/plain", "Not found");
}

// ==================== REPORT STATUS ====================

void reportLidStatus(
  const String& status,
  const String& reqId
) {

  HTTPClient http;

  String url =
    "http://" + String(SERVER_IP) +
    ":" + String(SERVER_PORT) +
    "/api/lid-status";

  http.begin(url);

  http.addHeader(
    "Content-Type",
    "application/json"
  );

  String body =
    "{\"status\":\"" + status +
    "\",\"request_id\":\"" + reqId + "\"}";

  int code = http.POST(body);

  Serial.printf(
    "Lid status '%s' reported → HTTP %d\n",
    status.c_str(),
    code
  );

  http.end();
}

// ==================== SETUP ====================

void setup() {

  Serial.begin(115200);

  Serial.println("\n\n Smart E-Waste Bin Booting...");

  pinMode(FLASH_PIN, OUTPUT);

  digitalWrite(FLASH_PIN, LOW);

  // Servo
  ESP32PWM::allocateTimer(0);

  lidServo.setPeriodHertz(50);

  lidServo.attach(SERVO_PIN, 500, 2400);

  lidServo.write(SERVO_CLOSE_ANGLE);

  Serial.println(" Servo initialised (closed)");

  // Camera
  if (!initCamera()) {

    Serial.println(" Camera failed — rebooting in 5s");

    delay(5000);

    ESP.restart();
  }

  Serial.println(" Camera initialised");

  // WiFi
  Serial.print(" Connecting to WiFi: ");

  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int retries = 0;

  while (
    WiFi.status() != WL_CONNECTED &&
    retries < 30
  ) {

    delay(500);

    Serial.print(".");

    retries++;
  }

  if (WiFi.status() != WL_CONNECTED) {

    Serial.println("\n WiFi failed");

    ESP.restart();
  }

  Serial.println("\n WiFi connected!");

  Serial.print(" ESP32-CAM IP Address: ");

  Serial.println(WiFi.localIP());

  // HTTP Routes
  server.on("/trigger", HTTP_GET, handleTrigger);

  server.on("/status", HTTP_GET, handleStatus);

  server.on("/test-open", HTTP_GET, handleTestOpen);

  server.onNotFound(handleNotFound);

  server.begin();

  Serial.println(" HTTP server started");
}

// ==================== MAIN LOOP ====================

void loop() {

  server.handleClient();

  if (photoQueued) {

    photoQueued = false;

    String reqId = pendingRequestId;

    openLid();

    reportLidStatus("open", reqId);

    Serial.println(" Waiting for deposit...");

    delay(3000);

    bool uploaded =
      captureAndUpload(reqId);

    if (!uploaded) {

      Serial.println(" Retrying upload...");

      delay(1000);

      captureAndUpload(reqId);
    }

    int remainOpen =
      LID_OPEN_DURATION - 5000;

    if (remainOpen > 0) {

      delay(remainOpen);
    }

    closeLid();

    reportLidStatus("closed", reqId);

    Serial.println(" Cycle complete!");
  }

  delay(10);
}*/