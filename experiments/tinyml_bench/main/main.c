/*
 * Derya's model on the ESP32-S3 (success criteria 2 and 4, on the hardware).
 *
 * Replays the 21-day internship log (6,020 five-minute samples, the same
 * as baseline_bench) and, for every complete 24 h window:
 *   - runs tinyml_forecast() and the baseline's decision rule on it,
 *   - compares now / when_minutes and the 3 h forecast with Derya's float
 *     model in Python (cloud/derya_model.py, embedded in tinyml_expected.c),
 *   - times the forecast and the decision with esp_timer.
 * Also prints the model's flash size and the peak stack of the task.
 * For reference it times the baseline (fit + decide) on the same windows.
 *
 * Targets: same decision >= 99 %, model <= 50 KB, inference <= 10 ms.
 */
#include <inttypes.h>
#include <math.h>
#include <stdio.h>

#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "baseline.h"
#include "replay_data.h"
#include "tinyml.h"
#include "tinyml_expected.h"

#define BENCH_STACK_BYTES 4096

static baseline_t s_buf;      /* the 24 h buffer both models read */

static struct {
    int compared, agree, warns, first_mismatch;
    float pred_diff_max, pred_diff_sum;
    int64_t tm_sum, tm_max, base_sum, base_max;
    uint32_t stack_free_min;
} s_res;

static void bench_task(void *arg)
{
    baseline_init(&s_buf, (baseline_params_t){ .field_capacity = 64.9f,
                                               .fc_warn_low = 70.0f, .vpd_warn_high = 0.8f });
    s_res.first_mismatch = -1;
    float fc[BASELINE_HORIZON];

    for (int i = 0; i < kSamples; i++) {
        baseline_push(&s_buf, g_vwc[i], g_air_temp[i], g_rh[i]);
        if ((i & 0xFF) == 0) {
            vTaskDelay(1);                 /* keep the task watchdog quiet */
        }
        if (g_tm_now[i] == 255) {
            continue;                      /* not a complete, valid window */
        }

        int64_t t0 = esp_timer_get_time();
        int rc = tinyml_forecast(&s_buf, NULL, fc);
        baseline_decision_t d = baseline_decide_forecast(&s_buf, rc == 0 ? fc : NULL);
        int64_t t1 = esp_timer_get_time();
        baseline_fit(&s_buf);
        (void)baseline_decide(&s_buf, VPD_PERSISTENCE);
        int64_t t2 = esp_timer_get_time();

        s_res.compared++;
        s_res.tm_sum += t1 - t0;
        s_res.base_sum += t2 - t1;
        if (t1 - t0 > s_res.tm_max) s_res.tm_max = t1 - t0;
        if (t2 - t1 > s_res.base_max) s_res.base_max = t2 - t1;
        if (rc != 0) {
            if (s_res.first_mismatch < 0) s_res.first_mismatch = i;
            continue;
        }
        s_res.warns += d.warn;
        float diff = fabsf(fc[BASELINE_HORIZON - 1] - g_tm_pred_3h[i]);
        s_res.pred_diff_sum += diff;
        if (diff > s_res.pred_diff_max) s_res.pred_diff_max = diff;
        if ((uint8_t)d.irrigate_now == g_tm_now[i] && d.when_minutes == g_tm_when[i]) {
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
    printf("\n=== Derya's model on ESP32-S3 ===\n");

    int64_t start = esp_timer_get_time();
    xTaskCreate(bench_task, "bench", BENCH_STACK_BYTES, xTaskGetCurrentTaskHandle(), 5, NULL);
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    int64_t total = esp_timer_get_time() - start;

    int n = s_res.compared;
    printf("windows compared:        %d (with a warning: %d)\n", n, s_res.warns);
    printf("same decision as Python: %d/%d = %.2f %%  (target >= 99 %%)\n",
           s_res.agree, n, n ? 100.0 * s_res.agree / n : 0.0);
    if (s_res.first_mismatch >= 0) {
        int i = s_res.first_mismatch;
        printf("first mismatch at sample %d: expected now=%d when=%d\n",
               i, g_tm_now[i], g_tm_when[i]);
    }
    printf("3 h forecast vs Python:  mean %.4f, max %.4f VWC points\n",
           n ? (double)(s_res.pred_diff_sum / n) : 0.0, (double)s_res.pred_diff_max);
    printf("tinyml forecast+decide:  mean %.1f us, max %" PRId64 " us\n",
           n ? (double)s_res.tm_sum / n : 0.0, s_res.tm_max);
    printf("baseline fit+decide:     mean %.1f us, max %" PRId64 " us  (same windows)\n",
           n ? (double)s_res.base_sum / n : 0.0, s_res.base_max);
    printf("model in flash:          %u bytes  (target <= 51200)\n",
           (unsigned)tinyml_model_bytes());
    printf("24 h buffer (shared):    %u bytes (sizeof(baseline_t))\n", (unsigned)sizeof(baseline_t));
    printf("peak stack (task):       %u bytes of %d\n",
           (unsigned)(BENCH_STACK_BYTES - s_res.stack_free_min), BENCH_STACK_BYTES);
    printf("whole replay:            %.2f s for %d samples\n", total / 1e6, kSamples);
    printf("=== done ===\n");
}
