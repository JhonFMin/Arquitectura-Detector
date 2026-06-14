/*
  ESP32-CAM · OV3660 · Panel de seguridad completo
  ─────────────────────────────────────────────────
  CORRECCIONES vs. versión original (v3 — fix definitivo):

  DIAGNÓSTICO REAL:
  El ejemplo oficial de Arduino solo toca 3 parámetros del OV3660:
    vflip=1, brightness=1, saturation=-2
  y deja TODOS los demás en los defaults del driver esp32-camera.
  El código original sobreescribía gainceiling, aec_value, bpc, wpc, etc.
  con valores que limitaban la capacidad del AEC de compensar escenas oscuras.
  Eso causaba la imagen oscura, no el XCLK.

  CAMBIOS:
  1. applyInitialCameraProfile() reducida a EXACTAMENTE lo que hace el ejemplo
     oficial para OV3660: vflip, brightness, saturation, framesize.
     NADA más. Todo lo demás queda en defaults del driver.
  2. initCamera(): arranca en UXGA (necesario para calibrar el AEC del OV3660),
     luego baja a VGA en applyInitialCameraProfile.
  3. XCLK: 20 MHz (correcto para OV3660 en AI Thinker).
  4. handleCapture(): delay 300 ms + descarte de 1 frame para DeepFace.
  5. 2 frames de calentamiento descartados post-init.
  6. Stream, PIR, relay, flash, panel web y rutas: sin cambios.
*/

#include "esp_camera.h"
#include <WiFi.h>
#include <WebServer.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include "esp_wifi.h"
#include "freertos/semphr.h"

// ================= WIFI =================
const char* ssid     = "KAREN";
const char* password = "27122006";

// ================= IP FIJA =================
IPAddress local_IP(192, 168, 0, 50);
IPAddress gateway(192, 168, 0, 1);
IPAddress subnet(255, 255, 255, 0);
IPAddress primaryDNS(8, 8, 8, 8);
IPAddress secondaryDNS(1, 1, 1, 1);

// ================= AJUSTES =================
#define STREAM_PORT            81
#define CONTROL_PORT           80
#define ACTIVE_HOLD_MS      30000UL
#define PIR_VERIFY_DELAY_MS  5000UL
#define ACCESS_OPEN_MS       5000UL
#define FRAME_INTERVAL_MS      120UL

#define STREAM_QUALITY          10
#define CAPTURE_QUALITY          6

// ================= PINES AI THINKER =================
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

#define FLASH_GPIO_NUM     4
#define PIR_GPIO_NUM      14
#define RELAY_GPIO_NUM    12

#define RELAY_ACTIVE_LEVEL LOW
#define RELAY_IDLE_LEVEL   HIGH

WebServer controlServer(CONTROL_PORT);
WebServer streamServer(STREAM_PORT);
SemaphoreHandle_t camMutex = nullptr;

// ================= ESTADO =================
volatile bool streamActive = false;
volatile uint32_t frameCount = 0;
volatile uint32_t captureCount = 0;

int pirState = LOW;
int lastPirState = LOW;
unsigned long pirRisingCount = 0;
unsigned long pirFallingCount = 0;
unsigned long lastMotionMillis = 0;
bool motionWindowActive = false;

bool pirArmed = true;

bool relayManualOn = false;
bool flashManualOn = false;

bool accessGranted = false;
unsigned long relayOpenUntil = 0;

bool verificationRequested = false;
unsigned long verificationRequestId = 0;

bool pirDelayRunning = false;
unsigned long pirDelayStart = 0;

unsigned long lastWiFiCheck = 0;

// ================= UTIL =================
bool isSystemActive() {
  if (!motionWindowActive) return false;
  return (millis() - lastMotionMillis) < ACTIVE_HOLD_MS;
}

unsigned long remainingActiveMs() {
  if (!motionWindowActive) return 0;
  unsigned long elapsed = millis() - lastMotionMillis;
  if (elapsed >= ACTIVE_HOLD_MS) return 0;
  return ACTIVE_HOLD_MS - elapsed;
}

void setRelay(bool on) {
  digitalWrite(RELAY_GPIO_NUM, on ? RELAY_ACTIVE_LEVEL : RELAY_IDLE_LEVEL);
}

void setFlashLevel(int level) {
  level = constrain(level, 0, 255);
  analogWrite(FLASH_GPIO_NUM, level);
}

void setFlash(bool on) {
  digitalWrite(FLASH_GPIO_NUM, on ? HIGH : LOW);
}

void applyOutputs() {
  bool active = isSystemActive();
  if (!active) motionWindowActive = false;

  bool relayOn = relayManualOn || accessGranted;
  bool flashOn = flashManualOn || active || pirDelayRunning;

  setRelay(relayOn);
  setFlash(flashOn);
}

void openAccessRelay(unsigned long durationMs = ACCESS_OPEN_MS) {
  accessGranted = true;
  relayOpenUntil = millis() + durationMs;
  setRelay(true);
}

void closeAccessRelay() {
  accessGranted = false;
  relayOpenUntil = 0;
  if (!relayManualOn) setRelay(false);
}

framesize_t parseFrameSize(const String& s) {
  if (s == "qqvga") return FRAMESIZE_QQVGA;
  if (s == "qvga")  return FRAMESIZE_QVGA;
  if (s == "cif")   return FRAMESIZE_CIF;
  if (s == "vga")   return FRAMESIZE_VGA;
  if (s == "svga")  return FRAMESIZE_SVGA;
  if (s == "xga")   return FRAMESIZE_XGA;
  if (s == "sxga")  return FRAMESIZE_SXGA;
  if (s == "uxga")  return FRAMESIZE_UXGA;
  return FRAMESIZE_VGA;
}

String frameSizeName(int fs) {
  switch (fs) {
    case FRAMESIZE_QQVGA: return "qqvga";
    case FRAMESIZE_QVGA:  return "qvga";
    case FRAMESIZE_CIF:   return "cif";
    case FRAMESIZE_VGA:   return "vga";
    case FRAMESIZE_SVGA:  return "svga";
    case FRAMESIZE_XGA:   return "xga";
    case FRAMESIZE_SXGA:  return "sxga";
    case FRAMESIZE_UXGA:  return "uxga";
    default: return "vga";
  }
}

// ================= WIFI =================
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);

  WiFi.config(local_IP, gateway, subnet, primaryDNS, secondaryDNS);
  WiFi.begin(ssid, password);

  Serial.print("Conectando a WiFi");
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 40) {
    delay(500);
    Serial.print(".");
    tries++;
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("No se pudo conectar al WiFi. Reiniciando...");
    delay(3000);
    ESP.restart();
  }

  esp_wifi_set_ps(WIFI_PS_NONE);

  Serial.println("WiFi conectado");
  Serial.print("IP ESP32: ");
  Serial.println(WiFi.localIP());
  Serial.print("Panel: http://");
  Serial.println(WiFi.localIP());
  Serial.print("Stream: http://");
  Serial.print(WiFi.localIP());
  Serial.println(":81/stream");
}

// ================= PERFIL INICIAL CAMARA (OV3660) =================
/*
  Replica EXACTAMENTE lo que hace el ejemplo oficial de Arduino para OV3660.
  Solo se tocan 4 cosas; todo lo demás queda en los defaults del driver
  esp32-camera. Sobreescribir gainceiling / aec_value / bpc / etc. limita
  la capacidad del AEC de compensar escenas oscuras — de ahí la imagen oscura.
*/
void applyInitialCameraProfile(sensor_t* s) {
  if (!s) return;

  s->set_vflip(s, 1);       // el OV3660 sale invertido de fábrica
  s->set_brightness(s, 1);  // +1 leve, igual que el ejemplo oficial
  s->set_saturation(s, -2); // el OV3660 sotura demasiado por defecto
  s->set_framesize(s, FRAMESIZE_VGA); // bajar de UXGA a VGA para el stream

  // TODO lo demás (AEC, AGC, gainceiling, AWB, BPC, WPC, lenc, dcw...)
  // queda en los defaults del driver. NO tocar — el AEC del sensor
  // los calibra solo y producen la exposición correcta.
}

// ================= CAMARA =================
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

  config.xclk_freq_hz = 20000000;  // 20 MHz — correcto para OV3660 en AI Thinker
  config.pixel_format = PIXFORMAT_JPEG;

  /*
    FIX PRINCIPAL: arrancar siempre en UXGA.
    El OV3660 calibra su AEC y circuitos analógicos internos según la
    resolución de arranque. Si se arranca en VGA (modo binning), los
    registros quedan mal calibrados y la imagen sale oscura.
    Después de esp_camera_init() bajamos a VGA en applyInitialCameraProfile().
  */
  if (psramFound()) {
    config.frame_size   = FRAMESIZE_UXGA;   // CAMBIO: era VGA — arrancar en máxima resolución
    config.jpeg_quality = 10;
    config.fb_count     = 2;
    config.grab_mode    = CAMERA_GRAB_LATEST;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
  } else {
    config.frame_size   = FRAMESIZE_SVGA;   // sin PSRAM, SVGA es el máximo seguro
    config.jpeg_quality = 12;
    config.fb_count     = 1;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;  // CAMBIO: era 2/GRAB_WHEN_EMPTY, más estable sin PSRAM
    config.fb_location  = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = ESP_FAIL;
  for (int i = 0; i < 3 && err != ESP_OK; i++) {
    err = esp_camera_init(&config);
    if (err != ESP_OK) {
      Serial.printf("[CAM] Fallo intento %d/3: 0x%x\n", i + 1, err);
      esp_camera_deinit();
      delay(500);
    }
  }
  if (err != ESP_OK) return false;

  sensor_t* s = esp_camera_sensor_get();
  // applyInitialCameraProfile baja la resolución de UXGA a VGA
  // y aplica todos los ajustes específicos del OV3660
  applyInitialCameraProfile(s);

  // Descartar 2 frames de calentamiento para que el AEC converja
  for (int i = 0; i < 2; i++) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb) esp_camera_fb_return(fb);
    delay(80);
  }

  Serial.println("[CAM] Inicializada (OV3660 · 20 MHz · UXGA→VGA)");
  return true;
}

// ================= JSON =================
void sendJSON(String payload) {
  controlServer.sendHeader("Access-Control-Allow-Origin", "*");
  controlServer.send(200, "application/json", payload);
}

// ================= STATUS =================
void handleStatus() {
  sensor_t* s = esp_camera_sensor_get();

  String json = "{";
  json += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  json += "\"pir\":" + String(pirState) + ",";
  json += "\"pirArmed\":" + String(pirArmed ? "true" : "false") + ",";
  json += "\"pirDelayRunning\":" + String(pirDelayRunning ? "true" : "false") + ",";
  json += "\"pirRisingCount\":" + String(pirRisingCount) + ",";
  json += "\"pirFallingCount\":" + String(pirFallingCount) + ",";
  json += "\"motionWindowActive\":" + String(isSystemActive() ? "true" : "false") + ",";
  json += "\"remainingActiveMs\":" + String(remainingActiveMs()) + ",";
  json += "\"relayManualOn\":" + String(relayManualOn ? "true" : "false") + ",";
  json += "\"flashManualOn\":" + String(flashManualOn ? "true" : "false") + ",";
  json += "\"relayOn\":" + String((relayManualOn || accessGranted) ? "true" : "false") + ",";
  json += "\"flashOn\":" + String((flashManualOn || isSystemActive() || pirDelayRunning) ? "true" : "false") + ",";
  json += "\"verificationRequested\":" + String(verificationRequested ? "true" : "false") + ",";
  json += "\"verificationRequestId\":" + String(verificationRequestId) + ",";
  json += "\"capturesServed\":" + String(captureCount) + ",";
  json += "\"framesStreamed\":" + String(frameCount) + ",";

  if (s) {
    json += "\"framesize\":" + String(s->status.framesize) + ",";
    json += "\"quality\":" + String(s->status.quality) + ",";
    json += "\"brightness\":" + String(s->status.brightness) + ",";
    json += "\"contrast\":" + String(s->status.contrast) + ",";
    json += "\"saturation\":" + String(s->status.saturation) + ",";
    json += "\"special_effect\":" + String(s->status.special_effect) + ",";
    json += "\"awb\":" + String(s->status.awb) + ",";
    json += "\"awb_gain\":" + String(s->status.awb_gain) + ",";
    json += "\"wb_mode\":" + String(s->status.wb_mode) + ",";
    json += "\"aec\":" + String(s->status.aec) + ",";
    json += "\"aec2\":" + String(s->status.aec2) + ",";
    json += "\"ae_level\":" + String(s->status.ae_level) + ",";
    json += "\"aec_value\":" + String(s->status.aec_value) + ",";
    json += "\"agc\":" + String(s->status.agc) + ",";
    json += "\"agc_gain\":" + String(s->status.agc_gain) + ",";
    json += "\"gainceiling\":" + String(s->status.gainceiling) + ",";
    json += "\"bpc\":" + String(s->status.bpc) + ",";
    json += "\"wpc\":" + String(s->status.wpc) + ",";
    json += "\"raw_gma\":" + String(s->status.raw_gma) + ",";
    json += "\"lenc\":" + String(s->status.lenc) + ",";
    json += "\"hmirror\":" + String(s->status.hmirror) + ",";
    json += "\"vflip\":" + String(s->status.vflip) + ",";
    json += "\"dcw\":" + String(s->status.dcw) + ",";
    json += "\"colorbar\":" + String(s->status.colorbar);
  } else {
    json += "\"framesize\":8";
  }

  json += "}";
  sendJSON(json);
}

// ================= CONTROL CAMARA =================
void handleControl() {
  if (!controlServer.hasArg("var") || !controlServer.hasArg("val")) {
    controlServer.send(400, "text/plain", "faltan var o val");
    return;
  }

  String variable = controlServer.arg("var");
  int val = controlServer.arg("val").toInt();

  sensor_t* s = esp_camera_sensor_get();
  if (!s) {
    controlServer.send(500, "text/plain", "sensor no disponible");
    return;
  }

  int res = 0;

  if (variable == "framesize") {
    res = s->set_framesize(s, (framesize_t)val);
  } else if (variable == "quality") {
    res = s->set_quality(s, val);
  } else if (variable == "contrast") {
    res = s->set_contrast(s, val);
  } else if (variable == "brightness") {
    res = s->set_brightness(s, val);
  } else if (variable == "saturation") {
    res = s->set_saturation(s, val);
  } else if (variable == "special_effect") {
    res = s->set_special_effect(s, val);
  } else if (variable == "awb") {
    res = s->set_whitebal(s, val);
  } else if (variable == "awb_gain") {
    res = s->set_awb_gain(s, val);
  } else if (variable == "wb_mode") {
    res = s->set_wb_mode(s, val);
  } else if (variable == "aec") {
    res = s->set_exposure_ctrl(s, val);
  } else if (variable == "aec2") {
    res = s->set_aec2(s, val);
  } else if (variable == "ae_level") {
    res = s->set_ae_level(s, val);
  } else if (variable == "aec_value") {
    res = s->set_aec_value(s, val);
  } else if (variable == "agc") {
    res = s->set_gain_ctrl(s, val);
  } else if (variable == "agc_gain") {
    res = s->set_agc_gain(s, val);
  } else if (variable == "gainceiling") {
    res = s->set_gainceiling(s, (gainceiling_t)val);
  } else if (variable == "bpc") {
    res = s->set_bpc(s, val);
  } else if (variable == "wpc") {
    res = s->set_wpc(s, val);
  } else if (variable == "raw_gma") {
    res = s->set_raw_gma(s, val);
  } else if (variable == "lenc") {
    res = s->set_lenc(s, val);
  } else if (variable == "hmirror") {
    res = s->set_hmirror(s, val);
  } else if (variable == "vflip") {
    res = s->set_vflip(s, val);
  } else if (variable == "dcw") {
    res = s->set_dcw(s, val);
  } else if (variable == "colorbar") {
    res = s->set_colorbar(s, val);
  } else {
    controlServer.send(400, "text/plain", "variable no soportada");
    return;
  }

  if (res == 0) {
    sendJSON("{\"ok\":true}");
  } else {
    controlServer.send(500, "text/plain", "no se pudo aplicar");
  }
}

// ================= HANDLERS SISTEMA =================
void handleRelayOn() {
  relayManualOn = true;
  applyOutputs();
  sendJSON("{\"ok\":true,\"relayManualOn\":true}");
}

void handleRelayOff() {
  relayManualOn = false;
  if (!accessGranted) setRelay(false);
  sendJSON("{\"ok\":true,\"relayManualOn\":false}");
}

void handleFlashOn() {
  flashManualOn = true;
  applyOutputs();
  sendJSON("{\"ok\":true,\"flashManualOn\":true}");
}

void handleFlashOff() {
  flashManualOn = false;
  applyOutputs();
  sendJSON("{\"ok\":true,\"flashManualOn\":false}");
}

void handlePirArm() {
  pirArmed = true;
  sendJSON("{\"ok\":true,\"pirArmed\":true}");
}

void handlePirDisarm() {
  pirArmed = false;
  pirDelayRunning = false;
  sendJSON("{\"ok\":true,\"pirArmed\":false}");
}

void handleAccessOpen() {
  unsigned long duration = ACCESS_OPEN_MS;
  if (controlServer.hasArg("ms")) {
    duration = constrain(controlServer.arg("ms").toInt(), 1000, 15000);
  }
  openAccessRelay(duration);
  sendJSON("{\"ok\":true,\"relay\":\"OPEN\"}");
}

void handleAccessClose() {
  closeAccessRelay();
  sendJSON("{\"ok\":true,\"relay\":\"CLOSED\"}");
}

void handleVerifyAck() {
  verificationRequested = false;
  sendJSON("{\"ok\":true,\"verifyRequested\":false}");
}

// ================= CAPTURE =================
void handleCapture() {
  int quality = CAPTURE_QUALITY;
  if (controlServer.hasArg("quality")) {
    quality = constrain(controlServer.arg("quality").toInt(), 4, 40);
  }

  framesize_t fsize = FRAMESIZE_VGA;
  if (controlServer.hasArg("size")) {
    fsize = parseFrameSize(controlServer.arg("size"));
  }

  sensor_t* s = esp_camera_sensor_get();
  int oldQuality = STREAM_QUALITY;
  framesize_t oldFsize = FRAMESIZE_VGA;

  if (s) {
    oldQuality = s->status.quality;
    oldFsize   = (framesize_t)s->status.framesize;
    s->set_quality(s, quality);
    s->set_framesize(s, fsize);
    delay(300);  // esperar que el AEC se estabilice con la nueva config
  }

  if (xSemaphoreTake(camMutex, pdMS_TO_TICKS(2500)) != pdTRUE) {
    controlServer.send(503, "text/plain", "Camara ocupada");
    return;
  }

  // Descartar 1 frame para asegurar buffer limpio
  camera_fb_t* discard = esp_camera_fb_get();
  if (discard) esp_camera_fb_return(discard);

  camera_fb_t* fb = esp_camera_fb_get();
  xSemaphoreGive(camMutex);

  if (s) {
    s->set_quality(s, oldQuality);
    s->set_framesize(s, oldFsize);
  }

  if (!fb) {
    controlServer.send(503, "text/plain", "Error al capturar");
    return;
  }

  controlServer.sendHeader("Access-Control-Allow-Origin", "*");
  controlServer.sendHeader("Content-Disposition", "inline; filename=capture.jpg");
  controlServer.send_P(200, "image/jpeg", (const char*)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  captureCount++;
}

// ================= PANEL WEB =================
void handleRoot() {
  String html = R"rawliteral(
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ESP32-CAM Panel Completo</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:16px}
h1,h2{margin:0 0 10px}
p{color:#cbd5e1}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.card{background:#111827;border:1px solid #334155;border-radius:12px;padding:14px}
.label{font-size:12px;color:#94a3b8}
.value{font-size:22px;font-weight:bold;margin-top:8px}
.buttons{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
button,select,input[type=range]{
  background:#1e293b;color:#fff;padding:10px 12px;border:none;border-radius:10px;
  font-weight:bold
}
button{cursor:pointer}
button:hover{background:#334155}
img{max-width:100%;border-radius:12px;border:1px solid #334155;margin-top:16px}
.ok{color:#22c55e}
.bad{color:#ef4444}
#msg{margin-top:10px;font-size:14px;color:#93c5fd}
.wrap{display:grid;grid-template-columns:1.2fr 1fr;gap:16px;align-items:start}
@media(max-width:1000px){.wrap{grid-template-columns:1fr}}
.ctrl-row{display:grid;grid-template-columns:130px 1fr 60px;gap:10px;align-items:center;margin:8px 0}
.switch-row{display:grid;grid-template-columns:130px auto 60px;gap:10px;align-items:center;margin:8px 0}
small{color:#94a3b8}
.section-title{margin-top:8px;margin-bottom:10px;color:#93c5fd}
</style>
</head>
<body>
  <h1>ESP32-CAM Seguridad</h1>
  <p>Panel con stream, PIR, relay, flash y configuración de cámara en vivo.</p>

  <div class="buttons">
    <button onclick="openCapture()">Captura JPG</button>
    <button onclick="sendCmd('/relay/on')">Relay ON</button>
    <button onclick="sendCmd('/relay/off')">Relay OFF</button>
    <button onclick="sendCmd('/flash/on')">Flash ON</button>
    <button onclick="sendCmd('/flash/off')">Flash OFF</button>
    <button onclick="sendCmd('/pir/arm')">PIR ON</button>
    <button onclick="sendCmd('/pir/disarm')">PIR OFF</button>
    <button onclick="sendCmd('/access/open')">Abrir Cerradura</button>
    <button onclick="sendCmd('/access/close')">Cerrar Cerradura</button>
    <button onclick="sendCmd('/verify/ack')">Reset Verify</button>
  </div>

  <div id="msg">Listo.</div>

  <div class="grid" style="margin-top:12px">
    <div class="card"><div class="label">IP</div><div class="value" id="ip">-</div></div>
    <div class="card"><div class="label">PIR</div><div class="value" id="pir">-</div></div>
    <div class="card"><div class="label">PIR ARMADO</div><div class="value" id="pirarmed">-</div></div>
    <div class="card"><div class="label">DELAY 5s</div><div class="value" id="delayrun">-</div></div>
    <div class="card"><div class="label">RELAY</div><div class="value" id="relay">-</div></div>
    <div class="card"><div class="label">FLASH</div><div class="value" id="flash">-</div></div>
    <div class="card"><div class="label">VERIFY</div><div class="value" id="verify">-</div></div>
    <div class="card"><div class="label">REQUEST ID</div><div class="value" id="rid">-</div></div>
    <div class="card"><div class="label">CAPTURAS</div><div class="value" id="captures">-</div></div>
    <div class="card"><div class="label">FRAMES</div><div class="value" id="frames">-</div></div>
  </div>

  <div class="wrap" style="margin-top:16px">
    <div class="card">
      <h2>Vista</h2>
      <img id="stream" src="" alt="stream">
    </div>

    <div class="card">
      <h2>Config cámara</h2>
      <div class="section-title">Imagen</div>

      <div class="ctrl-row">
        <label>Resolución</label>
        <select id="framesize" onchange="setCam('framesize', this.value)">
          <option value="0">QQVGA</option>
          <option value="3">QVGA</option>
          <option value="5">CIF</option>
          <option value="8">VGA</option>
          <option value="9">SVGA</option>
          <option value="10">XGA</option>
          <option value="11">SXGA</option>
          <option value="13">UXGA</option>
        </select>
        <span id="framesize_val">-</span>
      </div>

      <div class="ctrl-row">
        <label>Quality</label>
        <input id="quality" type="range" min="4" max="63" oninput="showVal('quality')" onchange="setCam('quality', this.value)">
        <span id="quality_val">-</span>
      </div>

      <div class="ctrl-row">
        <label>Brightness</label>
        <input id="brightness" type="range" min="-2" max="2" oninput="showVal('brightness')" onchange="setCam('brightness', this.value)">
        <span id="brightness_val">-</span>
      </div>

      <div class="ctrl-row">
        <label>Contrast</label>
        <input id="contrast" type="range" min="-2" max="2" oninput="showVal('contrast')" onchange="setCam('contrast', this.value)">
        <span id="contrast_val">-</span>
      </div>

      <div class="ctrl-row">
        <label>Saturation</label>
        <input id="saturation" type="range" min="-2" max="2" oninput="showVal('saturation')" onchange="setCam('saturation', this.value)">
        <span id="saturation_val">-</span>
      </div>

      <div class="ctrl-row">
        <label>Special FX</label>
        <select id="special_effect" onchange="setCam('special_effect', this.value)">
          <option value="0">No Effect</option>
          <option value="1">Negative</option>
          <option value="2">Grayscale</option>
          <option value="3">Red Tint</option>
          <option value="4">Green Tint</option>
          <option value="5">Blue Tint</option>
          <option value="6">Sepia</option>
        </select>
        <span id="special_effect_val">-</span>
      </div>

      <div class="section-title">Balance / exposición</div>

      <div class="switch-row">
        <label>AWB</label>
        <input id="awb" type="checkbox" onchange="setCam('awb', this.checked?1:0)">
        <span id="awb_val">-</span>
      </div>

      <div class="switch-row">
        <label>AWB Gain</label>
        <input id="awb_gain" type="checkbox" onchange="setCam('awb_gain', this.checked?1:0)">
        <span id="awb_gain_val">-</span>
      </div>

      <div class="ctrl-row">
        <label>WB Mode</label>
        <select id="wb_mode" onchange="setCam('wb_mode', this.value)">
          <option value="0">Auto</option>
          <option value="1">Sunny</option>
          <option value="2">Cloudy</option>
          <option value="3">Office</option>
          <option value="4">Home</option>
        </select>
        <span id="wb_mode_val">-</span>
      </div>

      <div class="switch-row">
        <label>AEC Sensor</label>
        <input id="aec" type="checkbox" onchange="setCam('aec', this.checked?1:0)">
        <span id="aec_val">-</span>
      </div>

      <div class="switch-row">
        <label>AEC DSP</label>
        <input id="aec2" type="checkbox" onchange="setCam('aec2', this.checked?1:0)">
        <span id="aec2_val">-</span>
      </div>

      <div class="ctrl-row">
        <label>AE Level</label>
        <input id="ae_level" type="range" min="-2" max="2" oninput="showVal('ae_level')" onchange="setCam('ae_level', this.value)">
        <span id="ae_level_val">-</span>
      </div>

      <div class="ctrl-row">
        <label>AEC Value</label>
        <input id="aec_value" type="range" min="0" max="1200" oninput="showVal('aec_value')" onchange="setCam('aec_value', this.value)">
        <span id="aec_value_val">-</span>
      </div>

      <div class="switch-row">
        <label>AGC</label>
        <input id="agc" type="checkbox" onchange="setCam('agc', this.checked?1:0)">
        <span id="agc_val">-</span>
      </div>

      <div class="ctrl-row">
        <label>AGC Gain</label>
        <input id="agc_gain" type="range" min="0" max="30" oninput="showVal('agc_gain')" onchange="setCam('agc_gain', this.value)">
        <span id="agc_gain_val">-</span>
      </div>

      <div class="ctrl-row">
        <label>Gain Ceiling</label>
        <input id="gainceiling" type="range" min="0" max="6" oninput="showVal('gainceiling')" onchange="setCam('gainceiling', this.value)">
        <span id="gainceiling_val">-</span>
      </div>

      <div class="section-title">Correcciones</div>

      <div class="switch-row"><label>BPC</label><input id="bpc" type="checkbox" onchange="setCam('bpc', this.checked?1:0)"><span id="bpc_val">-</span></div>
      <div class="switch-row"><label>WPC</label><input id="wpc" type="checkbox" onchange="setCam('wpc', this.checked?1:0)"><span id="wpc_val">-</span></div>
      <div class="switch-row"><label>Raw GMA</label><input id="raw_gma" type="checkbox" onchange="setCam('raw_gma', this.checked?1:0)"><span id="raw_gma_val">-</span></div>
      <div class="switch-row"><label>Lens Corr</label><input id="lenc" type="checkbox" onchange="setCam('lenc', this.checked?1:0)"><span id="lenc_val">-</span></div>
      <div class="switch-row"><label>H-Mirror</label><input id="hmirror" type="checkbox" onchange="setCam('hmirror', this.checked?1:0)"><span id="hmirror_val">-</span></div>
      <div class="switch-row"><label>V-Flip</label><input id="vflip" type="checkbox" onchange="setCam('vflip', this.checked?1:0)"><span id="vflip_val">-</span></div>
      <div class="switch-row"><label>DCW</label><input id="dcw" type="checkbox" onchange="setCam('dcw', this.checked?1:0)"><span id="dcw_val">-</span></div>
      <div class="switch-row"><label>Color Bar</label><input id="colorbar" type="checkbox" onchange="setCam('colorbar', this.checked?1:0)"><span id="colorbar_val">-</span></div>
    </div>
  </div>

<script>
const host = location.hostname;
document.getElementById("stream").src = "http://" + host + ":81/stream";

function setMsg(text){ document.getElementById("msg").textContent = text; }

async function sendCmd(url){
  try{
    setMsg("Enviando: " + url);
    await fetch(url, {method:"GET"});
    setMsg("Comando ejecutado: " + url);
    setTimeout(updateStatus, 200);
  }catch(e){
    console.error(e);
    setMsg("Error enviando comando");
  }
}

function openCapture(){
  window.open("/capture?quality=6&size=vga", "_blank");
}

function paintBool(id, value, trueText="SI", falseText="NO"){
  const el = document.getElementById(id);
  el.textContent = value ? trueText : falseText;
  el.className = "value " + (value ? "ok" : "bad");
}

function showVal(id){
  const el = document.getElementById(id);
  const out = document.getElementById(id + "_val");
  if(out) out.textContent = el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value;
}

async function setCam(variable, value){
  try{
    setMsg("Aplicando " + variable + "=" + value);
    const r = await fetch(`/control?var=${variable}&val=${value}`);
    if(!r.ok) throw new Error("fallo");
    setMsg("Aplicado: " + variable + "=" + value);
    setTimeout(updateStatus, 150);
  }catch(e){
    console.error(e);
    setMsg("Error aplicando " + variable);
  }
}

function syncUI(s){
  const mapRange = ["quality","brightness","contrast","saturation","ae_level","aec_value","agc_gain","gainceiling"];
  const mapCheck = ["awb","awb_gain","aec","aec2","agc","bpc","wpc","raw_gma","lenc","hmirror","vflip","dcw","colorbar"];
  const mapSelect = ["framesize","special_effect","wb_mode"];

  mapRange.forEach(k=>{
    const el=document.getElementById(k);
    if(el && s[k] !== undefined){ el.value=s[k]; showVal(k); }
  });

  mapCheck.forEach(k=>{
    const el=document.getElementById(k);
    if(el && s[k] !== undefined){ el.checked=!!Number(s[k]); showVal(k); }
  });

  mapSelect.forEach(k=>{
    const el=document.getElementById(k);
    if(el && s[k] !== undefined){
      el.value=String(s[k]);
      const out=document.getElementById(k+"_val");
      if(out) out.textContent=s[k];
    }
  });
}

async function updateStatus(){
  try{
    const r = await fetch("/status");
    const s = await r.json();

    document.getElementById("ip").textContent = s.ip;
    document.getElementById("pir").textContent = s.pir;

    paintBool("pirarmed", s.pirArmed);
    paintBool("delayrun", s.pirDelayRunning);
    paintBool("verify", s.verificationRequested);
    paintBool("relay", s.relayOn, "ON", "OFF");
    paintBool("flash", s.flashOn, "ON", "OFF");

    document.getElementById("rid").textContent = s.verificationRequestId;
    document.getElementById("captures").textContent = s.capturesServed;
    document.getElementById("frames").textContent = s.framesStreamed;

    syncUI(s);
  }catch(e){
    console.error(e);
    setMsg("No se pudo leer /status");
  }
}

setInterval(updateStatus, 1500);
updateStatus();
</script>
</body>
</html>
)rawliteral";

  controlServer.send(200, "text/html", html);
}

// ================= STREAM =================
void handleStream() {
  WiFiClient client = streamServer.client();
  client.setNoDelay(true);

  client.print(
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
    "Cache-Control: no-cache\r\n"
    "Pragma: no-cache\r\n\r\n"
  );

  streamActive = true;
  unsigned long lastFrame = 0;

  while (client.connected()) {
    unsigned long now = millis();
    if (now - lastFrame < FRAME_INTERVAL_MS) {
      delay(2);
      continue;
    }

    if (xSemaphoreTake(camMutex, pdMS_TO_TICKS(600)) != pdTRUE) {
      delay(5);
      continue;
    }

    camera_fb_t * fb = esp_camera_fb_get();
    xSemaphoreGive(camMutex);

    if (!fb) {
      delay(30);
      continue;
    }

    char part[64];
    int len = snprintf(part, sizeof(part),
      "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
      (unsigned)fb->len
    );
    client.write((const uint8_t*)part, len);
    client.write(fb->buf, fb->len);
    client.write((const uint8_t*)"\r\n", 2);

    esp_camera_fb_return(fb);
    frameCount++;
    lastFrame = millis();
  }

  streamActive = false;
}

void handleNotFound() {
  controlServer.send(404, "text/plain", "404");
}

void streamTask(void* pvParameters) {
  for (;;) {
    streamServer.handleClient();
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

// ================= SETUP =================
void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);
  Serial.begin(115200);
  delay(1000);

  pinMode(PIR_GPIO_NUM, INPUT);
  pinMode(RELAY_GPIO_NUM, OUTPUT);
  pinMode(FLASH_GPIO_NUM, OUTPUT);

  setRelay(false);
  setFlash(false);

  pirState = digitalRead(PIR_GPIO_NUM);
  lastPirState = pirState;

  camMutex = xSemaphoreCreateMutex();

  if (!initCamera()) {
    Serial.println("Fallo camara. Reiniciando...");
    delay(3000);
    ESP.restart();
  }

  connectWiFi();

  controlServer.on("/", HTTP_GET, handleRoot);
  controlServer.on("/status", HTTP_GET, handleStatus);
  controlServer.on("/capture", HTTP_GET, handleCapture);
  controlServer.on("/control", HTTP_GET, handleControl);

  controlServer.on("/relay/on", HTTP_GET, handleRelayOn);
  controlServer.on("/relay/off", HTTP_GET, handleRelayOff);
  controlServer.on("/flash/on", HTTP_GET, handleFlashOn);
  controlServer.on("/flash/off", HTTP_GET, handleFlashOff);
  controlServer.on("/pir/arm", HTTP_GET, handlePirArm);
  controlServer.on("/pir/disarm", HTTP_GET, handlePirDisarm);

  controlServer.on("/access/open", HTTP_GET, handleAccessOpen);
  controlServer.on("/access/close", HTTP_GET, handleAccessClose);
  controlServer.on("/verify/ack", HTTP_GET, handleVerifyAck);

  controlServer.onNotFound(handleNotFound);
  controlServer.begin();

  streamServer.on("/stream", HTTP_GET, handleStream);
  streamServer.begin();

  xTaskCreatePinnedToCore(streamTask, "streamTask", 8192, nullptr, 1, nullptr, 0);

  Serial.println("Servidor listo.");
}

// ================= LOOP =================
void loop() {
  controlServer.handleClient();

  int currentPir = digitalRead(PIR_GPIO_NUM);
  pirState = currentPir;

  if (pirArmed && currentPir != lastPirState) {
    if (currentPir == HIGH) {
      pirRisingCount++;
      lastMotionMillis = millis();
      motionWindowActive = true;
      pirDelayRunning = true;
      pirDelayStart = millis();
      Serial.println("[PIR] Movimiento detectado, esperando 5s...");
    } else {
      pirFallingCount++;
      Serial.println("[PIR] Fin de movimiento");
    }
    lastPirState = currentPir;
  }

  if (pirArmed && currentPir == HIGH) {
    lastMotionMillis = millis();
    motionWindowActive = true;
  }

  if (pirDelayRunning && (millis() - pirDelayStart >= PIR_VERIFY_DELAY_MS)) {
    pirDelayRunning = false;
    verificationRequested = true;
    verificationRequestId++;
    Serial.println("[VERIFY] Solicitud lista para Python");
  }

  if (accessGranted && relayOpenUntil > 0 && millis() >= relayOpenUntil) {
    closeAccessRelay();
  }

  applyOutputs();

  if (millis() - lastWiFiCheck > 15000) {
    lastWiFiCheck = millis();
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("WiFi caido, reconectando...");
      WiFi.disconnect(true);
      delay(500);
      connectWiFi();
    }
  }

  delay(5);
}
