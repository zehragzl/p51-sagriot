/*
 * Host test for tinyml.c (runs on the PC, not the ESP32).
 *
 * Reads a CSV with columns vwc,air_temp,air_humidity (one row per 5-min
 * sample), pushes the rows through the baseline's buffer and, for every
 * full 24 h window, prints
 *   index,now,warn,when_minutes,f1..f36
 * (tinyml forecast in VWC, decision with the baseline's rule).
 * tools/compare_tinyml.py checks it against cloud/derya_model.py.
 *
 * Build:  make
 * Run:    ./test_tinyml data.csv [field_capacity]
 */
#include <stdio.h>
#include <stdlib.h>
#include "tinyml.h"

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s data.csv [field_capacity]\n", argv[0]);
        return 1;
    }
    FILE *f = fopen(argv[1], "r");
    if (!f) { perror(argv[1]); return 1; }

    baseline_params_t params = {
        .field_capacity = argc > 2 ? (float)atof(argv[2]) : 64.9f,
        .fc_warn_low = 70.0f, .vpd_warn_high = 0.8f,
    };
    static baseline_t b;
    baseline_init(&b, params);

    char line[256];
    if (!fgets(line, sizeof line, f)) { fclose(f); return 1; }
    printf("index,now,warn,when_minutes");
    for (int j = 1; j <= BASELINE_HORIZON; j++) printf(",f%d", j);
    printf("\n");

    long index = 0;
    float vwc, t, rh, out[BASELINE_HORIZON];
    while (fgets(line, sizeof line, f)) {
        if (sscanf(line, "%f,%f,%f", &vwc, &t, &rh) == 3) {
            baseline_push(&b, vwc, t, rh);
            if (tinyml_forecast(&b, NULL, out) == 0) {
                baseline_decision_t d = baseline_decide_forecast(&b, out);
                printf("%ld,%d,%d,%d", index, d.irrigate_now, d.warn, d.when_minutes);
                for (int j = 0; j < BASELINE_HORIZON; j++) printf(",%.5f", out[j]);
                printf("\n");
            }
        }
        index++;
    }
    fclose(f);
    fprintf(stderr, "model: %zu bytes\n", tinyml_model_bytes());
    return 0;
}
