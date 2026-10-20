/*
 * baseline.c - two-coefficient "driven drying" model for the ESP32-S3.
 *
 * Port of DrivenDrying(("vpd",)) from zehragzl/sagriot-dss
 * (sagriot/forecasters.py). See docs/baseline_port_spec.md for every
 * constant and its source.
 *
 * Pure C, no ESP-IDF calls: the same file builds on the PC (gcc) for
 * testing and on the ESP32 as a component.
 */
#include "baseline.h"

#include <math.h>
#include <string.h>

#define RIDGE         1e-3   /* forecasters.py: ridge=1e-3            */
#define STEP_MINUTES  5      /* advise.py: STEP_MINUTES = 5           */
#define DET_EPSILON   1e-12  /* below this the 2x2 system is singular */

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */

/* Python's round() rounds halves to even; nearbyint() does the same in
 * the default rounding mode. */
static double round_to(double x, double scale)
{
    return nearbyint(x * scale) / scale;
}

/* features.py: compute_vpd, FAO-56 / Tetens, rounded to 3 decimals. */
static float compute_vpd(float air_temp, float rh)
{
    double es = 0.6108 * exp(17.27 * air_temp / (air_temp + 237.3));
    double ea = es * rh / 100.0;
    return (float)round_to(es - ea, 1000.0);
}

/* i-th sample in time order: 0 = oldest, count-1 = newest. */
static uint16_t chrono_index(const baseline_t *b, uint16_t i)
{
    return (uint16_t)((b->head + BASELINE_CONTEXT - b->count + i)
                      % BASELINE_CONTEXT);
}

/* ------------------------------------------------------------------ */
/* Public API                                                         */
/* ------------------------------------------------------------------ */

void baseline_init(baseline_t *b, baseline_params_t params)
{
    memset(b, 0, sizeof(*b));
    b->params = params;
}

void baseline_push(baseline_t *b, float vwc, float air_temp, float rh)
{
    b->vwc[b->head] = vwc;
    b->vpd[b->head] = compute_vpd(air_temp, rh);
    b->head = (uint16_t)((b->head + 1) % BASELINE_CONTEXT);
    if (b->count < BASELINE_CONTEXT) {
        b->count++;
    }
    b->fitted = false;   /* new data: coefficients are stale */
}

/*
 * Spec section 4.1. Fits delta = a * vpd + c over the full 24 h window,
 * with a ridge penalty on a only.
 *
 * The ESP32-S3 has a single-precision FPU; double is emulated in software
 * and made this fit take ~0.7 ms on the chip. So the sums are in float,
 * and centred on the means first, which keeps float accurate:
 *
 *   a = Sxy / (Sxx_c + p)        Sxx_c = sum (x - mean_x)^2
 *   c = mean_d - a * mean_x      Sxy   = sum (x - mean_x)(d - mean_d)
 *
 * This is the same solution as the 2x2 system in the spec (eliminate c
 * from its second row). The ridge penalty keeps the reference definition,
 * p = ridge * trace(gram) / 2 with trace = Sxx_c + n*mean_x^2 + n.
 */
bool baseline_fit(baseline_t *b)
{
    if (b->count < BASELINE_CONTEXT) {
        b->fitted = false;   /* the reference always fits on 288 samples */
        return false;
    }

    const int n = b->count - 1;

    /* pass 1: means */
    float sum_x = 0.0f, sum_d = 0.0f;
    for (int i = 0; i < n; i++) {
        sum_x += b->vpd[chrono_index(b, (uint16_t)(i + 1))];
        sum_d += b->vwc[chrono_index(b, (uint16_t)(i + 1))]
               - b->vwc[chrono_index(b, (uint16_t)i)];
    }
    const float mean_x = sum_x / (float)n;
    const float mean_d = sum_d / (float)n;

    /* pass 2: centred sums */
    float sxx_c = 0.0f, sxy = 0.0f;
    for (int i = 0; i < n; i++) {
        float dx = b->vpd[chrono_index(b, (uint16_t)(i + 1))] - mean_x;
        float dd = (b->vwc[chrono_index(b, (uint16_t)(i + 1))]
                  - b->vwc[chrono_index(b, (uint16_t)i)]) - mean_d;
        sxx_c += dx * dx;
        sxy   += dx * dd;
    }

    const float trace = sxx_c + (float)n * mean_x * mean_x + (float)n;
    const float p = (float)RIDGE * trace / 2.0f;
    const float denom = sxx_c + p;

    if (!(denom > (float)DET_EPSILON)) {
        b->fitted = false;   /* Python falls back to lstsq here; we report no fit */
        return false;
    }

    b->a = sxy / denom;
    b->c = mean_d - b->a * mean_x;
    b->fitted = true;
    return true;
}

/* Spec section 5: where the future VPD for step k (1..HORIZON) comes from. */
static float future_vpd(const baseline_t *b, vpd_mode_t mode, int k)
{
    if (mode == VPD_SEASONAL) {
        /* newest sample is time t at chronological index count-1 = 287;
         * t + k steps - 24 h sits at index k - 1 */
        return b->vpd[chrono_index(b, (uint16_t)(k - 1))];
    }
    return b->vpd[chrono_index(b, (uint16_t)(b->count - 1))];   /* persistence */
}

/*
 * Spec section 4.2. Writes the 36-step VWC forecast into out_vwc.
 * Returns 0 on success, -1 if the model is not fitted. Float throughout.
 */
int baseline_forecast(const baseline_t *b, vpd_mode_t mode,
                      float out_vwc[BASELINE_HORIZON])
{
    if (!b->fitted) {
        return -1;
    }

    float level = b->vwc[chrono_index(b, (uint16_t)(b->count - 1))];

    for (int k = 1; k <= BASELINE_HORIZON; k++) {
        float rate = b->a * future_vpd(b, mode, k) + b->c;
        if (rate > 0.0f) {
            rate = 0.0f;     /* non_increasing: no water gain without input */
        }
        level += rate;
        out_vwc[k - 1] = level;
    }
    return 0;
}

/*
 * Spec sections 6 and 7. Rule 2 (Irrigation) fires when soil_fc is
 * strictly below the warning line. Rule 1 (Water stress) needs the same
 * soil condition plus high VPD, so it can never fire earlier than Rule 2;
 * the first firing time is therefore the first Rule 2 step.
 *
 * round(vwc / FC * 100, 1) < warn_low  <=>  vwc / FC * 100 < warn_low - 0.05
 * (a value exactly on x.x5 rounds half to even and lands on warn_low, which
 * is not below it). So the line is turned into a VWC threshold once, and
 * each step is a single float comparison instead of a divide and a round.
 */
baseline_decision_t baseline_decide(const baseline_t *b, vpd_mode_t mode)
{
    baseline_decision_t d = { .irrigate_now = false, .warn = false,
                              .when_minutes = -1 };

    if (b->count == 0) {
        return d;
    }

    const float vwc_line = (b->params.fc_warn_low - 0.05f) / 100.0f
                           * b->params.field_capacity;

    float vwc_now = b->vwc[chrono_index(b, (uint16_t)(b->count - 1))];
    d.irrigate_now = vwc_now < vwc_line;

    float forecast[BASELINE_HORIZON];
    if (baseline_forecast(b, mode, forecast) != 0) {
        return d;            /* no fit yet: only the "now" decision */
    }

    for (int k = 1; k <= BASELINE_HORIZON; k++) {
        if (forecast[k - 1] < vwc_line) {
            d.warn = true;
            d.when_minutes = (int16_t)(k * STEP_MINUTES);
            break;
        }
    }
    return d;
}
