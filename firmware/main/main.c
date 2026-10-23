/*
 * SAgrIoT node - success criterion 2, step 1: ESP32 -> MQTT -> gateway.
 *
 * No sensors yet: the node replays the recorded log (the same 6020 samples
 * as experiments/baseline_bench, from components/replay) as if it were reading them, one sample per cycle.
 *
 *   edge mode : runs the baseline on the chip, publishes .../decision
 *   cloud mode: publishes the raw sample as .../reading
 *
 * In cloud mode the node also listens for .../cloud_decision and logs the
 * round trip (cloud/fake_cloud.py answers until the real cloud exists).
 *
 * Payloads follow docs/message_contract.md. When the sensor drivers arrive,
 * only read_sample() changes.
 */
#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "baseline.h"
#include "replay_data.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net.h"
#include "sdkconfig.h"

static const char *TAG = "node";

/* Decision parameters: internship pot, tomato (docs/baseline_port_spec.md 6) */
#define FIELD_CAPACITY 64.9f
#define FC_WARN_LOW    70.0f
#define VPD_WARN_HIGH  0.8f

#ifdef CONFIG_SAGRIOT_MODE_EDGE
#define MODE_NAME "edge"
#else
#define MODE_NAME "cloud"
#endif

typedef struct {
    float vwc, air_t, rh;
} sample_t;

static baseline_t s_model;   /* 2.3 KB, kept off the task stack */
static int s_next;           /* replay position in the recorded log */

/* Stand-in for the sensor drivers. At the end of the log it starts over;
 * the jump there is fine for a pipeline test. */
static sample_t read_sample(void)
{
    sample_t s = { g_vwc[s_next], g_air_temp[s_next], g_rh[s_next] };
    s_next = (s_next + 1) % kSamples;
    return s;
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

#ifdef CONFIG_SAGRIOT_MODE_EDGE
/* Edge mode: decide on the chip, send only the decision. */
static void build_message(char *msg, size_t n, uint32_t seq, const char *ts)
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
#define MESSAGE_KIND "decision"

#else
/* Cloud mode: send the sample, the cloud decides. */
static void build_message(char *msg, size_t n, uint32_t seq, const char *ts)
{
    /* newest entry of the buffer = VPD of the sample just pushed */
    float vpd_now = s_model.vpd[(s_model.head + BASELINE_CONTEXT - 1) % BASELINE_CONTEXT];
    float vwc_now = s_model.vwc[(s_model.head + BASELINE_CONTEXT - 1) % BASELINE_CONTEXT];
    char vwc[16], vpd[16];
    fmt_num(vwc, sizeof vwc, vwc_now, 2);
    fmt_num(vpd, sizeof vpd, vpd_now, 3);
    /* air_t / rh are in the buffer only as VPD, so take them from the log */
    int i = (s_next + kSamples - 1) % kSamples;
    char air_t[16], rh[16];
    fmt_num(air_t, sizeof air_t, g_air_temp[i], 2);
    fmt_num(rh, sizeof rh, g_rh[i], 2);

    snprintf(msg, n,
             "{\"v\":1,\"node\":\"%s\",\"seq\":%" PRIu32 ",\"ts\":%s,"
             "\"vwc\":%s,\"soil_t\":null,\"ec\":null,\"air_t\":%s,\"rh\":%s,\"vpd\":%s,"
             "\"batt_mv\":null,\"err\":0,\"t_send_ms\":%" PRId64 "}",
             CONFIG_SAGRIOT_NODE_ID, seq, ts, vwc, air_t, rh, vpd,
             esp_timer_get_time() / 1000);
}
#define MESSAGE_KIND "reading"
#endif

/* Value of a number field in a flat JSON message ("key":123). */
static bool json_number(const char *json, const char *key, long long *out)
{
    char pattern[32];
    snprintf(pattern, sizeof pattern, "\"%s\":", key);
    const char *p = strstr(json, pattern);
    if (p == NULL) {
        return false;
    }
    *out = strtoll(p + strlen(pattern), NULL, 10);
    return true;
}

/* Messages coming back from the gateway/cloud. A cloud_decision echoes the
 * node's t_send_ms, so now - t_send_ms is the round trip node -> cloud ->
 * node, measured on the node's own clock. */
static void on_message(const char *kind, const char *data, int len)
{
    char buf[320];
    int n = len < (int)sizeof buf - 1 ? len : (int)sizeof buf - 1;
    memcpy(buf, data, n);
    buf[n] = '\0';

    long long seq, t_send;
    if (strcmp(kind, "cloud_decision") == 0
        && json_number(buf, "seq", &seq) && json_number(buf, "t_send_ms", &t_send)) {
        long long rtt = esp_timer_get_time() / 1000 - t_send;
        ESP_LOGI(TAG, "cloud_decision seq %lld, round trip %lld ms | %s", seq, rtt, buf);
    } else {
        ESP_LOGI(TAG, "received %s: %s", kind, buf);
    }
}

void app_main(void)
{
    char online[160];
    snprintf(online, sizeof online,
             "{\"v\":1,\"node\":\"%s\",\"state\":\"online\",\"mode\":\"%s\",\"source\":\"replay\"}",
             CONFIG_SAGRIOT_NODE_ID, MODE_NAME);
    net_set_rx_handler(on_message);
    net_start(online);

    /* Warm start: fill 24 h minus one sample, so the first cycle can already
     * fit the model instead of waiting 287 cycles. */
    baseline_init(&s_model, (baseline_params_t){
        .field_capacity = FIELD_CAPACITY,
        .fc_warn_low = FC_WARN_LOW,
        .vpd_warn_high = VPD_WARN_HIGH,
    });
    for (int i = 0; i < BASELINE_CONTEXT - 1; i++) {
        sample_t s = read_sample();
        baseline_push(&s_model, s.vwc, s.air_t, s.rh);
    }

    ESP_LOGI(TAG, "mode %s, period %d ms, waiting for Wi-Fi + MQTT",
             MODE_NAME, CONFIG_SAGRIOT_PERIOD_MS);
    while (!net_wait_ready(10000)) {
        ESP_LOGW(TAG, "still not connected");
    }

    uint32_t seq = 0, sent = 0, lost = 0;
    TickType_t last_wake = xTaskGetTickCount();
    for (;;) {
        sample_t s = read_sample();
        baseline_push(&s_model, s.vwc, s.air_t, s.rh);

        char ts[32];
        fmt_ts(ts, sizeof ts);
        char msg[320];
        build_message(msg, sizeof msg, seq, ts);

        /* not connected: the cycle is lost and shows up as a seq gap */
        if (net_publish(MESSAGE_KIND, msg, 1, false) >= 0) {
            sent++;
        } else {
            lost++;
        }
        if (seq % 10 == 0) {
            ESP_LOGI(TAG, "seq %" PRIu32 ": sent %" PRIu32 ", lost %" PRIu32 " | %s",
                     seq, sent, lost, msg);
        }
        seq++;
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(CONFIG_SAGRIOT_PERIOD_MS));
    }
}
