#ifndef TINYML_H
#define TINYML_H
/*
 * Derya's linear forecaster on the node: weights quantized (soil int16,
 * VPD int8, 31.5 KB of flash), maths in float (the S3 has a float unit).
 *
 * Same window and output as the baseline: reads the baseline's 24 h ring
 * buffer (288 VWC + 288 VPD samples, so no second copy in RAM) and writes
 * a 36-step (3 h) VWC forecast. The decision is then made with the same
 * rule as the baseline: baseline_decide_forecast().
 *
 *   x = [soil_i - soil_now (i = 0..287), vpd_i (i = 0..287)],  soil = VWC / FC * 100
 *   change_j = ss_j * sum q_soil[i][j] * xs_i + sv_j * sum q_vpd[i][j] * xv_i + bias_j  (% FC)
 *   forecast_j = vwc_now + change_j * FC / 100 - site_bias_j   (VWC)
 *
 * Pure C, no ESP-IDF calls: builds on the PC for the host test.
 */
#include <stddef.h>
#include "baseline.h"

/* site_bias: 36 values subtracted from the forecast (the cloud's learned
 * bias for this site, bias_3h in the twin), or NULL for none.
 * Returns 0, or -1 if the buffer is not full, has a NaN, or FC <= 0. */
int tinyml_forecast(const baseline_t *b, const float *site_bias,
                    float out_vwc[BASELINE_HORIZON]);

/* Flash used by the weights, scales and biases, in bytes. */
size_t tinyml_model_bytes(void);

#endif /* TINYML_H */
