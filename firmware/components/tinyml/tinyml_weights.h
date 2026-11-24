#pragma once
#include <stdint.h>

/* Generated data: tools/export_tinyml.py. Window and horizon as the baseline. */
#define TINYML_WINDOW  288   /* soil inputs, then the same number of VPD inputs */
#define TINYML_OUTPUTS 36

extern const int16_t g_tinyml_q_soil[TINYML_WINDOW][TINYML_OUTPUTS];
extern const int8_t  g_tinyml_q_vpd[TINYML_WINDOW][TINYML_OUTPUTS];
extern const float   g_tinyml_scale_soil[TINYML_OUTPUTS];
extern const float   g_tinyml_scale_vpd[TINYML_OUTPUTS];
extern const float   g_tinyml_bias[TINYML_OUTPUTS];
