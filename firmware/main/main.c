/*
 * SAgrIoT node - data flow test: ESP32 -> gateway -> cloud -> gateway.
 *
 * Samples come from the recorded log (the same 6020 samples as
 * experiments/baseline_bench, from components/replay) or, with
 * CONFIG_SAGRIOT_SOURCE_SENSORS, from the SHT31 and the soil probe
 * (components/sensors). Only read_sample() differs.
 *
 * Every cycle the node publishes two messages (docs/message_contract.md):
 *   .../reading  : the raw sample; the gateway forwards it to the cloud
 *   .../decision : the decision made on the chip (baseline)
 * The gateway keeps the edge decision, receives the cloud's decision for
 * the same seq and compares the two. The node does not wait for the cloud.
 *
 * Two ways to run (CONFIG_SAGRIOT_DEEP_SLEEP):
 *   off: stay awake and loop every period (replay tests, short period)
 *   on : wake -> read -> decide -> send -> wait for PUBACK -> deep sleep.
 *        The 24 h buffer, seq and counters live in RTC memory, so they
 *        survive the sleep; a power-on or crash starts them over.
 */
#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "baseline.h"
#include "replay_data.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net.h"
#include "sdkconfig.h"
#if CONFIG_SAGRIOT_SOURCE_SENSORS
#include "sensors.h"
#endif

static const char *TAG = "node";

/* Decision parameters: internship pot, tomato (docs/baseline_port_spec.md 6) */
#define FIELD_CAPACITY 64.9f
#define FC_WARN_LOW    70.0f
#define VPD_WARN_HIGH  0.8f

/* Deep sleep: never sleep less than this, even if the cycle ran long */
#define MIN_SLEEP_MS   1000
/* Deep sleep: how long to wait for the broker's PUBACKs before sleeping */
#define FLUSH_TIMEOUT_MS 3000
/* Deep sleep: after a power-on, wait this long for SNTP (once; the clock
 * then survives deep sleep) */
#define TIME_SYNC_TIMEOUT_MS 3000

#if CONFIG_SAGRIOT_SOURCE_SENSORS
#define SOURCE_NAME "sensors"
#else
#define SOURCE_NAME "replay"
#endif

typedef struct {
    float vwc, air_t, rh;
    float soil_t, ec;   /* NAN in replay: the log has no such columns */
    uint32_t err;       /* sensors_reading_t.err bits, 0 in replay */
} sample_t;

/* State kept across deep sleep (RTC slow memory, ~2.3 KB for the model) */
static RTC_DATA_ATTR baseline_t s_model;
static RTC_DATA_ATTR int s_next;           /* replay position in the recorded log */
static RTC_DATA_ATTR uint32_t s_seq, s_sent, s_lost;

/* Stand-in for the sensor drivers. At the end of the log it starts over;
 * the jump there is fine for a pipeline test. */
static sample_t read_sample(void)
{
#if CONFIG_SAGRIOT_SOURCE_SENSORS
    sensors_reading_t r;
    sensors_read(&r);   /* failures are NAN + err bits */
    return (sample_t){ r.vwc, r.air_t, r.rh, r.soil_t, r.ec, r.err };
#else
    sample_t s = { g_vwc[s_next], g_air_temp[s_next], g_rh[s_next], NAN, NAN, 0 };
    s_next = (s_next + 1) % kSamples;
    return s;
#endif
}

/* JSON number, or null when the value is missing (the log has gaps). */
static void fmt_num(char *buf, size_t n, float v, int decimals)
{
    if (isfinite(v)) {
        snprintf(buf, n, "%.*f", decimals, v);
    } else {
        snprintf(buf, n, "null");
    }
}

/* "ts" field: UTC from SNTP, null until the clock is set. */
static void fmt_ts(char *buf, size_t n)
{
    if (!net_time_synced()) {
        snprintf(buf, n, "null");
        return;
    }
    time_t now = time(NULL);
    struct tm t;
    gmtime_r(&now, &t);
    strftime(buf, n, "\"%Y-%m-%dT%H:%M:%SZ\"", &t);
}

/* reading: the sample just pushed into the buffer. */
static void build_reading(char *msg, size_t n, uint32_t seq, const char *ts, sample_t s)
{
    float vpd_now = s_model.vpd[(s_model.head + BASELINE_CONTEXT - 1) % BASELINE_CONTEXT];
    char vwc[16], vpd[16], air_t[16], rh[16], soil_t[16], ec[16];
    fmt_num(vwc, sizeof vwc, s.vwc, 2);
    fmt_num(vpd, sizeof vpd, vpd_now, 3);
    fmt_num(air_t, sizeof air_t, s.air_t, 2);
    fmt_num(rh, sizeof rh, s.rh, 2);
    fmt_num(soil_t, sizeof soil_t, s.soil_t, 2);
    fmt_num(ec, sizeof ec, s.ec / 1000.0f, 3);   /* probe µS/cm -> contract dS/m */

    snprintf(msg, n,
             "{\"v\":1,\"node\":\"%s\",\"seq\":%" PRIu32 ",\"ts\":%s,"
             "\"vwc\":%s,\"soil_t\":%s,\"ec\":%s,\"air_t\":%s,\"rh\":%s,\"vpd\":%s,"
             "\"batt_mv\":null,\"err\":%" PRIu32 ",\"t_send_ms\":%" PRId64 "}",
             CONFIG_SAGRIOT_NODE_ID, seq, ts, vwc, soil_t, ec, air_t, rh, vpd,
             s.err, esp_timer_get_time() / 1000);
}

/* decision: baseline run on the chip. */
static void build_decision(char *msg, size_t n, uint32_t seq, const char *ts)
{
    int64_t t0 = esp_timer_get_time();
    baseline_fit(&s_model);
    baseline_decision_t d = baseline_decide(&s_model, VPD_PERSISTENCE);
    int64_t infer_us = esp_timer_get_time() - t0;

    float forecast[BASELINE_HORIZON];
    float pred_3h = NAN;
    if (baseline_forecast(&s_model, VPD_PERSISTENCE, forecast) == 0) {
        pred_3h = forecast[BASELINE_HORIZON - 1];
    }
    char pred[16];
    fmt_num(pred, sizeof pred, pred_3h, 2);

    snprintf(msg, n,
             "{\"v\":1,\"node\":\"%s\",\"seq\":%" PRIu32 ",\"ts\":%s,\"model\":\"baseline\","
             "\"now\":%s,\"warn\":%s,\"t_cross_min\":%d,\"vwc_pred_3h\":%s,"
             "\"infer_us\":%" PRId64 ",\"t_send_ms\":%" PRId64 "}",
             CONFIG_SAGRIOT_NODE_ID, seq, ts, d.irrigate_now ? "true" : "false",
             d.warn ? "true" : "false", d.when_minutes, pred, infer_us,
             esp_timer_get_time() / 1000);
}

/* The node does not need the cloud's answer; config messages are logged
 * until the node acts on them. */
static void on_message(const char *kind, const char *data, int len)
{
    ESP_LOGI(TAG, "received %s (%d bytes)", kind, len);
}

/* status on errors (docs/message_contract.md): which sensor failed and why,
 * so the cause is visible at the gateway without a serial cable. */
static void publish_error_status(uint32_t seq, uint32_t err)
{
#if CONFIG_SAGRIOT_SOURCE_SENSORS
    esp_err_t sht, probe;
    sensors_last_errors(&sht, &probe);
    char msg[256];
    snprintf(msg, sizeof msg,
             "{\"v\":1,\"node\":\"%s\",\"state\":\"error\",\"source\":\"%s\",\"seq\":%" PRIu32 ","
             "\"err\":%" PRIu32 ",\"sht31\":\"%s\",\"probe\":\"%s\"}",
             CONFIG_SAGRIOT_NODE_ID, SOURCE_NAME, seq, err,
             esp_err_to_name(sht), esp_err_to_name(probe));
    ESP_LOGW(TAG, "%s", msg);
    net_publish("status", msg, 1, true);
#endif
}

/* Fresh model state: on power-on, reset or crash (not after deep sleep). */
static void cold_start(void)
{
    s_seq = s_sent = s_lost = 0;
    s_next = 0;
    baseline_init(&s_model, (baseline_params_t){
        .field_capacity = FIELD_CAPACITY,
        .fc_warn_low = FC_WARN_LOW,
        .vpd_warn_high = VPD_WARN_HIGH,
    });
#if !CONFIG_SAGRIOT_SOURCE_SENSORS
    /* Warm start: fill 24 h minus one sample, so the first cycle can already
     * fit the model instead of waiting 287 cycles. With real sensors the
     * buffer fills from real readings (first forecast after 24 h). */
    for (int i = 0; i < BASELINE_CONTEXT - 1; i++) {
        sample_t s = read_sample();
        baseline_push(&s_model, s.vwc, s.air_t, s.rh);
    }
#endif
}

/* One cycle: read, decide, send. connect_ms > 0: wait that long for the
 * broker first (deep sleep, where every cycle connects anew). */
static void run_cycle(int connect_ms)
{
    sample_t s = read_sample();
    baseline_push(&s_model, s.vwc, s.air_t, s.rh);

    if (connect_ms > 0) {
        if (!net_wait_ready(connect_ms)) {
            ESP_LOGW(TAG, "no Wi-Fi + MQTT after %d ms", connect_ms);
        } else if (!net_wait_time_synced(TIME_SYNC_TIMEOUT_MS)) {
            /* otherwise the node sleeps before SNTP answers and ts stays null */
            ESP_LOGW(TAG, "clock not set yet, ts is null this cycle");
        }
    }

    char ts[32];
    fmt_ts(ts, sizeof ts);
    char reading[320], decision[320];
    build_reading(reading, sizeof reading, s_seq, ts, s);
    build_decision(decision, sizeof decision, s_seq, ts);

    /* not connected: the cycle is lost and shows up as a seq gap */
    bool ok = net_publish("reading", reading, 1, false) >= 0;
    ok = (net_publish("decision", decision, 1, false) >= 0) && ok;
    if (s.err != 0) {
        publish_error_status(s_seq, s.err);
    }
    if (ok) {
        s_sent++;
    } else {
        s_lost++;
    }
    if (connect_ms > 0 || s_seq % 10 == 0) {
        ESP_LOGI(TAG, "seq %" PRIu32 ": sent %" PRIu32 ", lost %" PRIu32 " | %s",
                 s_seq, s_sent, s_lost, decision);
    }
    s_seq++;
}

void app_main(void)
{
    bool woke = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER;

    char online[160];
    snprintf(online, sizeof online,
             "{\"v\":1,\"node\":\"%s\",\"state\":\"online\",\"source\":\"%s\"}",
             CONFIG_SAGRIOT_NODE_ID, SOURCE_NAME);
    net_set_rx_handler(on_message);
    net_start(online);   /* Wi-Fi connects in the background meanwhile */

#if CONFIG_SAGRIOT_SOURCE_SENSORS
    sensors_init();
#endif
    if (!woke) {
        cold_start();
    }

#if CONFIG_SAGRIOT_DEEP_SLEEP
    run_cycle(CONFIG_SAGRIOT_CONNECT_TIMEOUT_MS);

    /* publish only queued the messages: wait for the broker's PUBACK */
    if (!net_flush(FLUSH_TIMEOUT_MS)) {
        ESP_LOGW(TAG, "not every message was acknowledged before sleep");
    }
    net_stop();
#if CONFIG_SAGRIOT_SOURCE_SENSORS
    sensors_prepare_sleep();
#endif

    /* sleep the rest of the period, so cycles stay CONFIG_SAGRIOT_PERIOD_MS apart */
    int64_t awake_ms = esp_timer_get_time() / 1000;
    int64_t sleep_ms = CONFIG_SAGRIOT_PERIOD_MS - awake_ms;
    if (sleep_ms < MIN_SLEEP_MS) {
        sleep_ms = MIN_SLEEP_MS;
    }
    ESP_LOGI(TAG, "awake %" PRId64 " ms, deep sleep %" PRId64 " ms", awake_ms, sleep_ms);
    esp_sleep_enable_timer_wakeup((uint64_t)sleep_ms * 1000);
    esp_deep_sleep_start();
#else
    ESP_LOGI(TAG, "period %d ms, waiting for Wi-Fi + MQTT", CONFIG_SAGRIOT_PERIOD_MS);
    while (!net_wait_ready(10000)) {
        ESP_LOGW(TAG, "still not connected");
    }

    TickType_t last_wake = xTaskGetTickCount();
    for (;;) {
        run_cycle(0);
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(CONFIG_SAGRIOT_PERIOD_MS));
    }
#endif
}
