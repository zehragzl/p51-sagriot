#ifndef BASELINE_H
#define BASELINE_H

#include <stdbool.h>
#include <stdint.h>

/* Time base (spec section 1) */
#define BASELINE_CONTEXT 288   /* 24 h of 5-min samples */
#define BASELINE_HORIZON 36    /* 3 h of 5-min steps   */

/* Where future VPD comes from (spec section 5) */
typedef enum {
    VPD_PERSISTENCE = 0,   /* future VPD = current VPD          */
    VPD_SEASONAL    = 1    /* future VPD = VPD 24 h earlier     */
} vpd_mode_t;

/* Site parameters (spec section 6) */
typedef struct {
    float field_capacity;  /* VWC at field capacity, %          */
    float fc_warn_low;     /* soil_fc warning line, e.g. 70     */
    float vpd_warn_high;   /* VPD warning line, e.g. 0.8 kPa    */
} baseline_params_t;

/* Model state: ring buffers + fitted coefficients */
typedef struct {
    float    vwc[BASELINE_CONTEXT];
    float    vpd[BASELINE_CONTEXT];
    uint16_t head;         /* next write position               */
    uint16_t count;        /* samples stored so far (max 288)   */
    float    a;            /* drying rate per kPa of VPD        */
    float    c;            /* constant drying rate              */
    bool     fitted;
    baseline_params_t params;
} baseline_t;

/* Decision for one cycle (spec section 7) */
typedef struct {
    bool    irrigate_now;  /* Rule 2 fires on the current sample */
    bool    warn;          /* a rule fires within the 3 h horizon */
    int16_t when_minutes;  /* minutes to first firing, -1 if none */
} baseline_decision_t;

void baseline_init(baseline_t *b, baseline_params_t params);
void baseline_push(baseline_t *b, float vwc, float air_temp, float rh);
bool baseline_fit(baseline_t *b);
int  baseline_forecast(const baseline_t *b, vpd_mode_t mode,
                       float out_vwc[BASELINE_HORIZON]);
baseline_decision_t baseline_decide(const baseline_t *b, vpd_mode_t mode);

#endif /* BASELINE_H */
