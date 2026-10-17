/*
 * Host test for baseline.c (runs on the PC, not the ESP32).
 *
 * Reads a CSV with columns vwc,air_temp,air_humidity (one row per 5-min
 * sample), pushes the rows through the model and, once 288 samples are
 * in, prints one line per sample:
 *   index,a,c,irrigate_now,warn,when_minutes
 *
 * Build:  make
 * Run:    ./test_baseline data.csv [field_capacity] [persistence|seasonal]
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "baseline.h"

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s data.csv [field_capacity] [persistence|seasonal]\n", argv[0]);
        return 1;
    }
    FILE *f = fopen(argv[1], "r");
    if (!f) { perror(argv[1]); return 1; }

    baseline_params_t params = {
        .field_capacity = argc > 2 ? (float)atof(argv[2]) : 64.9f,
        .fc_warn_low = 70.0f,
        .vpd_warn_high = 0.8f,
    };
    vpd_mode_t mode = (argc > 3 && strcmp(argv[3], "seasonal") == 0)
                      ? VPD_SEASONAL : VPD_PERSISTENCE;
    static baseline_t b;           /* static: 2.3 KB, keep it off the stack */
    baseline_init(&b, params);

    char line[256];
    if (!fgets(line, sizeof line, f)) { fclose(f); return 1; }   /* skip header */
    printf("index,a,c,irrigate_now,warn,when_minutes\n");

    long index = 0;
    float vwc, t, rh;
    while (fgets(line, sizeof line, f)) {
        if (sscanf(line, "%f,%f,%f", &vwc, &t, &rh) != 3) { index++; continue; }
        baseline_push(&b, vwc, t, rh);
        if (baseline_fit(&b)) {
            baseline_decision_t d = baseline_decide(&b, mode);
            printf("%ld,%.6g,%.6g,%d,%d,%d\n", index, b.a, b.c,
                   d.irrigate_now, d.warn, d.when_minutes);
        }
        index++;
    }
    fclose(f);
    return 0;
}
