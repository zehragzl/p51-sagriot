/*
 * tinyml.c - Derya's linear model (576 -> 36), quantized weights
 * (soil int16, VPD int8).
 * Float model: cloud/derya_model.py. Weights: tools/export_tinyml.py.
 * See include/tinyml.h for the maths.
 */
#include "tinyml.h"
#include "tinyml_weights.h"

#include <math.h>

#define CTX BASELINE_CONTEXT

_Static_assert(TINYML_OUTPUTS == BASELINE_HORIZON, "model horizon must match the baseline");
_Static_assert(TINYML_WINDOW == BASELINE_CONTEXT, "model window must match the baseline");

/* i-th sample in time order (0 = oldest), as in baseline.c */
static inline int idx(const baseline_t *b, int i)
{
    return (b->head + CTX - b->count + i) % CTX;
}

int tinyml_forecast(const baseline_t *b, const float *site_bias,
                    float out_vwc[BASELINE_HORIZON])
{
    const float fc = b->params.field_capacity;
    if (b->count < CTX || !(fc > 0.0f)) {
        return -1;
    }
    const float vwc_now = b->vwc[idx(b, CTX - 1)];
    const float to_soil = 100.0f / fc;

    /* integer-weight sums per output; float inputs */
    float acc_s[TINYML_OUTPUTS] = { 0 }, acc_v[TINYML_OUTPUTS] = { 0 };
    for (int i = 0; i < CTX; i++) {
        const int k = idx(b, i);
        const float xs = (b->vwc[k] - vwc_now) * to_soil;   /* soil_i - soil_now */
        const float xv = b->vpd[k];
        if (!isfinite(xs) || !isfinite(xv)) {
            return -1;
        }
        const int16_t *qs = g_tinyml_q_soil[i];
        const int8_t  *qv = g_tinyml_q_vpd[i];
        for (int j = 0; j < TINYML_OUTPUTS; j++) {
            acc_s[j] += xs * (float)qs[j];
            acc_v[j] += xv * (float)qv[j];
        }
    }

    const float to_vwc = fc / 100.0f;
    for (int j = 0; j < TINYML_OUTPUTS; j++) {
        float change = acc_s[j] * g_tinyml_scale_soil[j]
                     + acc_v[j] * g_tinyml_scale_vpd[j] + g_tinyml_bias[j];
        out_vwc[j] = vwc_now + change * to_vwc - (site_bias ? site_bias[j] : 0.0f);
    }
    return 0;
}

size_t tinyml_model_bytes(void)
{
    return sizeof(g_tinyml_q_soil) + sizeof(g_tinyml_q_vpd) + sizeof(g_tinyml_scale_soil)
         + sizeof(g_tinyml_scale_vpd) + sizeof(g_tinyml_bias);
}
