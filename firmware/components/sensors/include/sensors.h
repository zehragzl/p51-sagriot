#pragma once
/*
 * sensors - SHT31 (I2C, air) + RS485 / Modbus RTU soil probe, with the
 * probe power switched by a P-MOSFET (docs/integration_plan.md, section 3).
 *
 * Pins and probe settings come from Kconfig ("SAgrIoT sensors" menu).
 */
#include <stdint.h>
#include "esp_err.h"

#define SENSORS_ERR_SHT31 (1u << 0)
#define SENSORS_ERR_PROBE (1u << 1)

typedef struct {
    float vwc;      /* %     (probe, register 0x0000 / 10)             */
    float soil_t;   /* °C    (probe, register 0x0001 / 10, signed)     */
    float ec;       /* µS/cm (probe, register 0x0002)                  */
    float air_t;    /* °C    (SHT31)                                   */
    float rh;       /* %     (SHT31)                                   */
    uint32_t err;   /* 0 = all read; SENSORS_ERR_* bits otherwise      */
} sensors_reading_t;

/* Sets up I2C, UART and the probe power pin. Call once per boot
 * (after deep sleep too). Returns the first error, but a failed bus
 * only shows up as NAN values and err bits in sensors_read(). */
esp_err_t sensors_init(void);

/* Turns the probe on, waits for it to warm up, reads it (3 tries),
 * turns it off; reads the SHT31 (3 tries). Values that could not be
 * read are NAN. Returns ESP_OK when everything was read. */
esp_err_t sensors_read(sensors_reading_t *out);

/* Why the last sensors_read() failed, per sensor (ESP_OK if it read).
 * For the "error" status message, so the cause shows up at the gateway. */
void sensors_last_errors(esp_err_t *sht31, esp_err_t *probe);

/* Before deep sleep: probe off, power and DE pins held at their idle
 * level so they do not float while the chip sleeps. */
void sensors_prepare_sleep(void);
