/*
 * Baseline on the ESP32-S3: success criterion 1, measured on the hardware.
 *
 * Replays the 21-day internship log (6,020 five-minute samples) through
 * firmware/components/baseline and, for every 24 h window:
 *   - compares irrigate_now and when_minutes with the Python reference
 *     (expected values embedded in baseline_data.c),
 *   - times baseline_fit() and baseline_decide() with esp_timer.
 * Memory: static model state (sizeof(baseline_t)) and the peak stack used
 * by fit + decide, from the high-water mark of a dedicated task.
 *
 * Targets: >= 99 % same decisions, <= 1 ms per decision, <= 4 KB RAM.
 */
#include <inttypes.h>
#include <stdio.h>

#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "baseline.h"
#include "baseline_data.h"

#define BENCH_STACK_BYTES 4096

static baseline_t s_model;                 /* the state the node keeps (RTC memory later) */

static struct {
    int compared, agree, warns;
    int first_mismatch;
    int64_t fit_sum, fit_max, dec_sum, dec_max;
    int timed;
    uint32_t stack_free_min;
} s_res;

static void bench_task(void *arg)
{
    baseline_params_t params = { .field_capacity = 64.9f, .fc_warn_low = 70.0f,
                                 .vpd_warn_high = 0.8f };
    baseline_init(&s_model, params);
    s_res.first_mismatch = -1;

    for (int i = 0; i < kSamples; i++) {
        baseline_push(&s_model, g_vwc[i], g_air_temp[i], g_rh[i]);

        int64_t t0 = esp_timer_get_time();
        bool ok = baseline_fit(&s_model);
        int64_t t1 = esp_timer_get_time();
        baseline_decision_t d = baseline_decide(&s_model, VPD_PERSISTENCE);
        int64_t t2 = esp_timer_get_time();

        if ((i & 0xFF) == 0) {
            vTaskDelay(1);                 /* let IDLE run, so the task watchdog stays quiet */
        }
        if (!ok || g_expected_now[i] == 255) {
            continue;                      /* not a complete, valid window */
        }
        s_res.timed++;
        s_res.fit_sum += t1 - t0;
        s_res.dec_sum += t2 - t1;
        if (t1 - t0 > s_res.fit_max) s_res.fit_max = t1 - t0;
        if (t2 - t1 > s_res.dec_max) s_res.dec_max = t2 - t1;

        s_res.compared++;
        s_res.warns += d.warn;
        if ((uint8_t)d.irrigate_now == g_expected_now[i] &&
            d.when_minutes == g_expected_when[i]) {
            s_res.agree++;
        } else if (s_res.first_mismatch < 0) {
            s_res.first_mismatch = i;
        }
    }

    s_res.stack_free_min = uxTaskGetStackHighWaterMark(NULL);
    xTaskNotifyGive((TaskHandle_t)arg);
    vTaskDelete(NULL);
}

void app_main(void)
{
    vTaskDelay(pdMS_TO_TICKS(2000));       /* let the serial monitor attach */
    printf("\n=== Baseline on ESP32-S3 (criterion 1) ===\n");

    int64_t start = esp_timer_get_time();
    xTaskCreate(bench_task, "bench", BENCH_STACK_BYTES, xTaskGetCurrentTaskHandle(), 5, NULL);
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    int64_t total = esp_timer_get_time() - start;

    double agree_pct = s_res.compared ? 100.0 * s_res.agree / s_res.compared : 0.0;
    printf("windows compared:      %d (with a warning: %d)\n", s_res.compared, s_res.warns);
    printf("same decision as Python: %d/%d = %.2f %%  (target >= 99 %%)\n",
           s_res.agree, s_res.compared, agree_pct);
    if (s_res.first_mismatch >= 0) {
        int i = s_res.first_mismatch;
        printf("first mismatch at sample %d: expected now=%d when=%d\n",
               i, g_expected_now[i], g_expected_when[i]);
    }
    printf("fit time:              mean %.1f us, max %" PRId64 " us\n",
           (double)s_res.fit_sum / s_res.timed, s_res.fit_max);
    printf("decide time:           mean %.1f us, max %" PRId64 " us\n",
           (double)s_res.dec_sum / s_res.timed, s_res.dec_max);
    printf("fit + decide:          mean %.3f ms per decision  (target <= 1 ms)\n",
           (double)(s_res.fit_sum + s_res.dec_sum) / s_res.timed / 1000.0);
    printf("model state:           %u bytes (sizeof(baseline_t))\n", (unsigned)sizeof(baseline_t));
    printf("peak stack (task):     %u bytes of %d\n",
           (unsigned)(BENCH_STACK_BYTES - s_res.stack_free_min), BENCH_STACK_BYTES);
    printf("total RAM estimate:    %u bytes  (target <= 4096)\n",
           (unsigned)(sizeof(baseline_t) + BENCH_STACK_BYTES - s_res.stack_free_min));
    printf("whole replay:          %.2f s for %d samples\n", total / 1e6, kSamples);
    printf("=== done ===\n");
}
