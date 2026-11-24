#pragma once
#include <stdint.h>
/* Float model's answers per replay sample (tools/compare_tinyml.py). */
extern const uint8_t g_tm_now[];     /* 255: window not compared */
extern const int16_t g_tm_when[];
extern const float   g_tm_pred_3h[]; /* forecast 3 h ahead, VWC */
