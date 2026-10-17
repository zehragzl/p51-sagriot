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

/* advise.py: soil_fc = round(vwc / FC * 100, 1) */
static float soil_fc(float vwc, float field_capacity)
{
    return (float)round_to((double)vwc / field_capacity * 100.0, 10.0);
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
 * with a ridge penalty on a only. Sums are kept in double so the result
 * stays close to the float64 Python reference.
 */
bool baseline_fit(baseline_t *b)
{
    if (b->count < BASELINE_CONTEXT) {
        b->fitted = false;   /* the reference always fits on 288 samples */
        return false;
    }

    double sxx = 0.0, sx = 0.0, sxd = 0.0, sd = 0.0;
    const double n = (double)(b->count - 1);

    for (uint16_t i = 0; i + 1 < b->count; i++) {
        double delta = (double)b->vwc[chrono_index(b, i + 1)]
                     - (double)b->vwc[chrono_index(b, i)];
        double x = (double)b->vpd[chrono_index(b, i + 1)];
        sxx += x * x;
        sx  += x;
        sxd += x * delta;
        sd  += delta;
    }

    /* penalty = ridge * trace(gram) / columns, not applied to the intercept */
    double p = RIDGE * (sxx + n) / 2.0;
    double a11 = sxx + p;
    double det = a11 * n - sx * sx;

    if (fabs(det) < DET_EPSILON) {
        b->fitted = false;   /* Python falls back to lstsq here; we report no fit */
        return false;
    }

    b->a = (float)((sxd * n - sx * sd) / det);
    b->c = (float)((a11 * sd - sx * sxd) / det);
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
 * Returns 0 on success, -1 if the model is not fitted.
 */
int baseline_forecast(const baseline_t *b, vpd_mode_t mode,
                      float out_vwc[BASELINE_HORIZON])
{
    if (!b->fitted) {
        return -1;
    }

    double level = (double)b->vwc[chrono_index(b, (uint16_t)(b->count - 1))];

    for (int k = 1; k <= BASELINE_HORIZON; k++) {
        double rate = (double)b->a * future_vpd(b, mode, k) + (double)b->c;
        if (rate > 0.0) {
            rate = 0.0;      /* non_increasing: no water gain without input */
        }
        level += rate;
        out_vwc[k - 1] = (float)level;
    }
    return 0;
}

/*
 * Spec sections 6 and 7. Rule 2 (Irrigation) fires when soil_fc is
 * strictly below the warning line. Rule 1 (Water stress) needs the same
 * soil condition plus high VPD, so it can never fire earlier than Rule 2;
 * the first firing time is therefore the first Rule 2 step.
 */
baseline_decision_t baseline_decide(const baseline_t *b, vpd_mode_t mode)
{
    baseline_decision_t d = { .irrigate_now = false, .warn = false,
                              .when_minutes = -1 };

    if (b->count == 0) {
        return d;
    }

    const float fc = b->params.field_capacity;
    const float warn_low = b->params.fc_warn_low;

    float vwc_now = b->vwc[chrono_index(b, (uint16_t)(b->count - 1))];
    d.irrigate_now = soil_fc(vwc_now, fc) < warn_low;

    float forecast[BASELINE_HORIZON];
    if (baseline_forecast(b, mode, forecast) != 0) {
        return d;            /* no fit yet: only the "now" decision */
    }

    for (int k = 1; k <= BASELINE_HORIZON; k++) {
        if (soil_fc(forecast[k - 1], fc) < warn_low) {
            d.warn = true;
            d.when_minutes = (int16_t)(k * STEP_MINUTES);
            break;
        }
    }
    return d;
}
