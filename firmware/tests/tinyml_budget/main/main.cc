// TinyML budget check on the ESP32-S3 (answers: are 50 KB / 100 KB / 100 ms realistic?).
//
// Loads a ~47 KB int8 1D-CNN of the target shape, then reports:
//   1. model size (flash)
//   2. tensor arena actually used (RAM)
//   3. time per inference, over 100 runs (min / mean / max)
#include <cinttypes>
#include <cstdio>

#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"

#include "model_data.h"

namespace {
constexpr int kArenaSize = 150 * 1024;   // generous; we report what is really used
constexpr int kRuns = 100;
alignas(16) uint8_t tensor_arena[kArenaSize];
}

extern "C" void app_main(void)
{
    vTaskDelay(pdMS_TO_TICKS(2000));     // give the serial monitor time to attach
    printf("\n=== TinyML budget check (ESP32-S3) ===\n");

    size_t heap_before = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);

    const tflite::Model *model = tflite::GetModel(g_model);
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        printf("model schema %" PRIu32 " != supported %d\n", model->version(), TFLITE_SCHEMA_VERSION);
        return;
    }

    static tflite::MicroMutableOpResolver<3> resolver;
    resolver.AddConv2D();
    resolver.AddFullyConnected();
    resolver.AddReshape();

    static tflite::MicroInterpreter interpreter(model, resolver, tensor_arena, kArenaSize);
    if (interpreter.AllocateTensors() != kTfLiteOk) {
        printf("AllocateTensors failed (arena too small?)\n");
        return;
    }

    TfLiteTensor *input = interpreter.input(0);
    for (size_t i = 0; i < input->bytes; i++) {
        input->data.int8[i] = (int8_t)((i * 37) % 255 - 127);   // any fixed pattern
    }

    int64_t min_us = INT64_MAX, max_us = 0, sum_us = 0;
    for (int run = 0; run < kRuns; run++) {
        int64_t t0 = esp_timer_get_time();
        if (interpreter.Invoke() != kTfLiteOk) {
            printf("Invoke failed\n");
            return;
        }
        int64_t dt = esp_timer_get_time() - t0;
        if (dt < min_us) min_us = dt;
        if (dt > max_us) max_us = dt;
        sum_us += dt;
    }

    size_t heap_after = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);

    printf("model size (flash):    %u bytes  (target <= 51200)\n", g_model_len);
    printf("tensor arena used:     %u bytes  (target <= 102400)\n",
           (unsigned)interpreter.arena_used_bytes());
    printf("inference time:        min %.2f ms, mean %.2f ms, max %.2f ms over %d runs  (target <= 100 ms)\n",
           min_us / 1000.0, sum_us / 1000.0 / kRuns, max_us / 1000.0, kRuns);
    printf("free internal heap:    %u bytes before, %u after setup\n",
           (unsigned)heap_before, (unsigned)heap_after);
    printf("input %u bytes, output %u bytes\n",
           (unsigned)input->bytes, (unsigned)interpreter.output(0)->bytes);
    printf("=== done ===\n");
}
