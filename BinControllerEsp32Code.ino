/*
 * esp32_mqtt_servo.ino — Smart E-Waste Bin (MQTT Servo Controller)
 * ─────────────────────────────────────────────────────────────
 *  SECURE VERSION — MQTT over TLS (MQTTS)
 *
 * A NORMAL ESP32 (not ESP32-CAM) that works BOTH ways:
 *
 *    ONLINE MODE (with server — secure):
 *      Connects to the MQTT broker over TLS (port 8883, encrypted +
 *      certificate-verified). Subscribes to smartbin/<DEVICE_ID>/cmd.
 *      On {"cmd":"open_lid",...} → opens the servo lid → user deposits
 *      waste during a fixed 30-second window → lid auto-closes →
 *      publishes {"event":"lid_closed",...} over TLS → the server
 *      issues the certificate.
 *
 *    STANDALONE MODE (independent — no server / WiFi needed):
 *      Press the physical button → lid opens → auto-closes after 30s.
 *      The servo works purely on the ESP32. MQTT acks are still sent if
 *      the broker is reachable (so the dashboard reflects the lid state),
 *      but NOTHING depends on them. No user details → no certificate.
 *
 *   Button = TOGGLE:  press when CLOSED → open (standalone mode)
 *                     press when OPEN   → close (manual)
 *
 *    SECURITY NOTES:
 *     - Uses WiFiClientSecure → all MQTT traffic is TLS-encrypted.
 *     - MQTT_TLS_VERIFY=true (default): the broker's certificate is
 *       verified against MQTT_ROOT_CA (DigiCert Global Root G2, embedded
 *       below — this is the root CA for the public broker.emqx.io).
 *     - If you switch to another broker (HiveMQ Cloud, EMQX Cloud, your
 *       own Mosquitto), replace MQTT_ROOT_CA with that broker's root CA.
 *       Fetch it with:
 *         openssl s_client -connect YOUR_BROKER:8883 -servername YOUR_BROKER -showcerts
 *     - Set MQTT_TLS_VERIFY=false to skip verification (encryption stays
 *       ON, identity check OFF) — only for quick testing.
 *     - For brokers that require login, fill MQTT_USER / MQTT_PASS.
 *
 *   Required Arduino libraries (Library Manager):
 *     - PubSubClient     by Nick O'Leary
 *     - ESP32Servo       by Kevin Harrington
 *     - ArduinoJson      by Benoit Blanchon
 *     (WiFi + WiFiClientSecure are built into the ESP32 Arduino core)
 *
 *   Wiring (defaults):
 *     Servo signal  → GPIO 13   (power servo from a separate 5V supply, common GND!)
 *     Button        → GPIO 12 to GND (INPUT_PULLUP, LOW = pressed) — opens/closes the bin
 *     IR sensor OUT → GPIO 14   (optional: FC-51/HW-201, LOW = waste detected)
 *     Onboard LED   → GPIO 2    (ON while lid is open)
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ESP32Servo.h>
#include <ArduinoJson.h>
#include <time.h>
#include "lwip/apps/sntp.h"   // SNTP (NTP) time sync — for TLS certificate date checks

// ══════════════ CONFIG — EDIT THESE ══════════════
const char* WIFI_SSID     = "";
const char* WIFI_PASSWORD = "";

// ── Secure MQTT (TLS) ──
const char* MQTT_BROKER   = "";   // must match server MQTT_BROKER
const int   MQTT_PORT     = 8883;               // 8883 = TLS (secure), 1883 = plain
const char* MQTT_USER     = "";                 // leave empty for public brokers
const char* MQTT_PASS     = "";

// Certificate verification (recommended: true)
const bool  MQTT_TLS_VERIFY = true;

// Root CA for broker.emqx.io (DigiCert Global Root G2).
// For a different broker, replace this with that broker's root CA PEM.
const char* MQTT_ROOT_CA = R"EOF(
-----BEGIN CERTIFICATE-----
MIIEfjCCA2agAwIBAgIQD+Ayq4RNAzEGxQyOE8iwaDANBgkqhkiG9w0BAQsFADBh
MQswCQYDVQQGEwJVUzEVMBMGA1UEChMMRGlnaUNlcnQgSW5jMRkwFwYDVQQLExB3
d3cuZGlnaWNlcnQuY29tMSAwHgYDVQQDExdEaWdpQ2VydCBHbG9iYWwgUm9vdCBD
QTAeFw0yNDAxMTgwMDAwMDBaFw0zMTExMDkyMzU5NTlaMGExCzAJBgNVBAYTAlVT
MRUwEwYDVQQKEwxEaWdpQ2VydCBJbmMxGTAXBgNVBAsTEHd3dy5kaWdpY2VydC5j
b20xIDAeBgNVBAMTF0RpZ2lDZXJ0IEdsb2JhbCBSb290IEcyMIIBIjANBgkqhkiG
9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuzfNNNx7a8myaJCtSnX/RrohCgiN9RlUyfuI
2/Ou8jqJkTx65qsGGmvPrC3oXgkkRLpimn7Wo6h+4FR1IAWsULecYxpsMNzaHxmx
1x7e/dfgy5SDN67sH0NO3Xss0r0upS/kqbitOtSZpLYl6ZtrAGCSYP9PIUkY92eQ
q2EGnI/yuum06ZIya7XzV+hdG82MHauVBJVJ8zUtluNJbd134/tJS7SsVQepj5Wz
tCO7TG1F8PapspUwtP1MVYwnSlcUfIKdzXOS0xZKBgyMUNGPHgm+F6HmIcr9g+UQ
vIOlCsRnKPZzFBQ9RnbDhxSJITRNrw9FDKZJobq7nMWxM4MphQIDAQABo4IBMDCC
ASwwDwYDVR0TAQH/BAUwAwEB/zAdBgNVHQ4EFgQUTiJUIBiV5uNu5g/6+rkS7QYX
jzkwHwYDVR0jBBgwFoAUA95QNVbRTLtm8KPiGxvDl7I90VUwDgYDVR0PAQH/BAQD
AgGGMHQGCCsGAQUFBwEBBGgwZjAjBggrBgEFBQcwAYYXaHR0cDovL29jc3AuZGln
aWNlcnQuY24wPwYIKwYBBQUHMAKGM2h0dHA6Ly9jYWNlcnRzLmRpZ2ljZXJ0LmNu
L0RpZ2lDZXJ0R2xvYmFsUm9vdENBLmNydDBABgNVHR8EOTA3MDWgM6Axhi9odHRw
Oi8vY3JsLmRpZ2ljZXJ0LmNuL0RpZ2lDZXJ0R2xvYmFsUm9vdENBLmNybDARBgNV
HSAECjAIMAYGBFUdIAAwDQYJKoZIhvcNAQELBQADggEBAHRBl3jN7+XHBUK0dZnu
hMdoNwD1nCROU3BTIh1TNzRI0bQ0m5+C/dCRzzlqoSAFHUlOi+OiDltWkXTzmQn6
Z8bH5PFBy5sYpc/8cNPoSzhyqcpvvEZvv/Ivc0Up+dzma7vBDJC9WrMRUUlSFSQp
kdXSmphDNkXJsgARmxzc18IN6LYMRiOWlY7RE2F900pPW60BvJHHNCX0bbSRj/Ql
bmVq8wuftBD++D+RS8K++ujpMjFBROyWfBX+woQDGsMazkmgulQdnZrdj476elOL
axRvrSgEorju1kJM7M65z2RUZrfzQYW/1rs8mRUXin6iEtad/Rv1ZI1WGYmWPyBm
pbo=
-----END CERTIFICATE-----
)EOF";

const char* TOPIC_PREFIX  = "smartbin";         // must match server MQTT_TOPIC_PREFIX
const char* DEVICE_ID     = "bin01";            // must match server MQTT_DEVICE_ID

const int SERVO_PIN       = 13;
const int BUTTON_PIN      = 12;                 // open/close button (standalone)
const int IR_PIN          = 14;                 // optional deposit sensor
const int LED_PIN         = 2;                  // onboard LED

const bool USE_IR_SENSOR  = false;              // optional: close early on deposit detection
const int  SERVO_CLOSED   = 0;                  // degrees
const int  SERVO_OPEN     = 90;                 // degrees

const unsigned long AUTO_CLOSE_MS     = 30000;  // fixed 30-second deposit window
const unsigned long DEPOSIT_SETTLE_MS = 3000;   // (IR mode) close 3s after waste settles
const unsigned long HEARTBEAT_MS      = 30000;  // status heartbeat interval
const unsigned long RECONNECT_MS      = 5000;   // MQTT/WiFi retry interval

const char* STANDALONE_REQ_ID = "standalone";  // request id for button (standalone) opens
// ══════════════════════════════════════════════════

char TOPIC_CMD[64];
char TOPIC_ACK[64];
char TOPIC_STATUS[64];

WiFiClientSecure secureClient;                   //  TLS client
PubSubClient     mqtt(secureClient);
Servo            lidServo;

enum LidState { CLOSED, OPEN };
LidState lidState = CLOSED;

String   currentRequestId = "";
unsigned long openedAt         = 0;
unsigned long lastHeartbeat    = 0;
unsigned long lastReconnectAt  = 0;

// IR deposit detection state (optional mode)
bool          depositSeen  = false;
unsigned long irClearSince = 0;

// Button debounce (non-blocking)
bool          lastButtonReading  = HIGH;
bool          buttonLatch        = false;
unsigned long lastButtonChange   = 0;

// ─────────────────────────────────────────────────────
void publishStatus() 
{
  JsonDocument doc;
  doc["online"]     = true;
  doc["lid"]        = (lidState == OPEN) ? "open" : "closed";
  doc["request_id"] = currentRequestId;
  doc["ip"]         = WiFi.localIP().toString();
  char buf[160];
  serializeJson(doc, buf);
  mqtt.publish(TOPIC_STATUS, buf, true);   // retained → server sees it instantly on subscribe
}

void publishAck(const char* event, const String& requestId) 
{
  JsonDocument doc;
  doc["event"]      = event;
  doc["request_id"] = requestId;
  doc["ts"]         = millis() / 1000;
  char buf[128];
  serializeJson(doc, buf);
  mqtt.publish(TOPIC_ACK, buf, true);
  Serial.printf(" ack → %s : %s\n", TOPIC_ACK, buf);
}

void openLid(const String& requestId) 
{
  if (lidState == OPEN) {                       // already open → just re-acknowledge
    publishAck("lid_opened", requestId);
    return;
  }
  Serial.println(" Opening lid…");
  lidServo.write(SERVO_OPEN);
  digitalWrite(LED_PIN, HIGH);
  lidState         = OPEN;
  currentRequestId = requestId;
  openedAt         = millis();
  depositSeen      = false;
  irClearSince     = 0;
  buttonLatch      = false;
  publishAck("lid_opened", requestId);
  publishStatus();
}

void closeLid()
{
  if (lidState == CLOSED) return;
  Serial.println(" Closing lid…");
  lidServo.write(SERVO_CLOSED);
  digitalWrite(LED_PIN, LOW);
  lidState = CLOSED;
  publishAck("lid_closed", currentRequestId);
  publishStatus();
}

void mqttCallback(char* topic, byte* payload, unsigned int length) 
{
  String msg;
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
  Serial.printf(" %s : %s\n", topic, msg.c_str());

  if (String(topic) != String(TOPIC_CMD)) return;

  JsonDocument doc;
  if (deserializeJson(doc, msg) != DeserializationError::Ok) {
    Serial.println("  Bad JSON command");
    return;
  }
  const char* cmd = doc["cmd"] | "";
  String requestId = String(doc["request_id"] | "");

  if (String(cmd) == "open_lid") {
    openLid(requestId);
  }
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf(" Connecting to WiFi %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries++ < 40) {
    delay(250);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n WiFi connected — IP %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n WiFi failed — will retry");
  }
}

void connectMQTT() {
  if (mqtt.connected()) return;
  Serial.printf("  Connecting to MQTT %s:%d (TLS) …\n", MQTT_BROKER, MQTT_PORT);

  // Last Will: if the ESP32 dies, the broker tells the server we are offline.
  // NOTE: PubSubClient has NO setWill() — the will goes straight into connect().
  const char* willMsg = "{\"online\":false}";
  String clientId = "esp32-bin-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  bool ok;
  if (MQTT_USER[0]) {
    ok = mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS,
                      TOPIC_STATUS, 0, true, willMsg);
  } else {
    ok = mqtt.connect(clientId.c_str(), (const char*)NULL, (const char*)NULL,
                      TOPIC_STATUS, 0, true, willMsg);
  }

  if (ok) {
    Serial.println(" MQTT connected (secure TLS)");
    mqtt.subscribe(TOPIC_CMD, 1);
    Serial.printf(" subscribed → %s\n", TOPIC_CMD);
    publishStatus();
  } else {
    Serial.printf(" MQTT connect failed, rc=%d — retry in %lus\n",mqtt.state(), RECONNECT_MS / 1000);
  }
}

void setup() 
{
  Serial.begin(115200);
  delay(500);
  Serial.println("\n  Smart E-Waste Bin — ESP32 MQTT Servo Controller (SECURE TLS)");
  Serial.println("    Button on GPIO 12 opens/closes the bin standalone (no server needed)");

  snprintf(TOPIC_CMD,    sizeof(TOPIC_CMD),    "%s/%s/cmd",    TOPIC_PREFIX, DEVICE_ID);
  snprintf(TOPIC_ACK,    sizeof(TOPIC_ACK),    "%s/%s/ack",    TOPIC_PREFIX, DEVICE_ID);
  snprintf(TOPIC_STATUS, sizeof(TOPIC_STATUS), "%s/%s/status", TOPIC_PREFIX, DEVICE_ID);
  Serial.printf("   topics: %s | %s | %s\n", TOPIC_CMD, TOPIC_ACK, TOPIC_STATUS);

  // ──  TLS setup (must happen before the first MQTT connect) ──
  if (MQTT_TLS_VERIFY) 
  {
    secureClient.setCACert(MQTT_ROOT_CA);      // verify broker certificate
    Serial.println(" TLS: certificate verification ENABLED");
  } 
  else
  {
    secureClient.setInsecure();                // encrypted, but no identity check
    Serial.println(" TLS: encryption ON, verification OFF (testing mode)");
  }

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(IR_PIN, INPUT_PULLUP);   // pull-up keeps the pin stable even before the sensor is wired

  lidServo.attach(SERVO_PIN);
  lidServo.write(SERVO_CLOSED);

  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setCallback(mqttCallback);
  mqtt.setBufferSize(1024);
  mqtt.setKeepAlive(30);

  connectWiFi();

  // ── NTP time sync ── TLS certificate verification checks validity DATES,
  // and the ESP32 clock starts at 1970 after boot — without this the TLS
  // handshake would fail even with the correct root CA.
  if (MQTT_TLS_VERIFY && WiFi.status() == WL_CONNECTED) 
  {
    sntp_setoperatingmode(SNTP_OPMODE_POLL);
    sntp_setservername(0, "pool.ntp.org");
    sntp_init();
    // Wait until the clock reports a sane year (max ~10s)
    time_t now = 0;
    struct tm timeinfo = {0};
    int retry = 0;
    while (timeinfo.tm_year < (2016 - 1900) && retry++ < 20)
      {
      delay(500);
      time(&now);
      localtime_r(&now, &timeinfo);
    }
    if (timeinfo.tm_year >= (2016 - 1900))
    {
      Serial.println(" Time synced via NTP — TLS certificate dates will verify");
    } 
    else 
    {
      Serial.println("  NTP sync failed — TLS certificate verification may fail");
    }
  }

  connectMQTT();
}

void loop() 
{
  // ── WiFi / MQTT resilience ──
  if (WiFi.status() != WL_CONNECTED) 
  {
    if (millis() - lastReconnectAt > RECONNECT_MS)
    {
      lastReconnectAt = millis();
      connectWiFi();
    }
  } 
  else if (!mqtt.connected())
  {
    if (millis() - lastReconnectAt > RECONNECT_MS) 
    {
      lastReconnectAt = millis();
      connectMQTT();
    }
  } 
  else 
  {
    mqtt.loop();
  }

  // ── Heartbeat ──
  if (mqtt.connected() && millis() - lastHeartbeat > HEARTBEAT_MS)
  {
    lastHeartbeat = millis();
    publishStatus();
  }

  // ── Physical button — works STANDALONE (no server / WiFi needed) ──
  // Toggle: press when CLOSED → open (standalone mode, no certificate)
  //         press when OPEN   → close (manual)
  bool reading = digitalRead(BUTTON_PIN);
  if (reading != lastButtonReading) 
  {
    lastButtonChange = millis();
    lastButtonReading = reading;
  }
  if (reading == HIGH)
  {
    buttonLatch = false;                              // released → re-arm
  }
  else if (!buttonLatch && (millis() - lastButtonChange) > 50) 
  {
    buttonLatch = true;                               // fire once per press
    if (lidState == CLOSED) 
    {
      Serial.println(" Button pressed — opening lid (standalone mode)");
      openLid(STANDALONE_REQ_ID);
    } 
    else 
    {
      Serial.println(" Button pressed — closing lid");
      closeLid();
    }
  }

  // ── Only while lid is OPEN ──
  if (lidState == OPEN) 
  {
    // 1) Fixed 30-second deposit window → auto-close.
    //    (Online mode: this lid_closed ack triggers the certificate.
    //     Standalone mode: bin just closes — no certificate.)
    if (millis() - openedAt > AUTO_CLOSE_MS) 
    {
      Serial.println("  30s deposit window over — closing lid");
      closeLid();
    }

    // 2) Optional IR: close early ~3s after a deposit is detected
    if (USE_IR_SENSOR)
    {
      bool beamBroken = (digitalRead(IR_PIN) == LOW);   // waste = LOW on FC-51/HW-201
      if (beamBroken)
      {
        depositSeen  = true;
        irClearSince = 0;
      } 
      else if (depositSeen)
      {
        if (irClearSince == 0) irClearSince = millis();
        if (millis() - irClearSince > DEPOSIT_SETTLE_MS) 
        {
          Serial.println("  Deposit detected — closing lid");
          closeLid();
        }
      }
    }
  }
}
