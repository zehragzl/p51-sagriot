/*
 * sensors.c - SHT31 over I2C, soil probe over RS485 / Modbus RTU.
 *
 * Probe register map, as measured on 20 Nov 2026 (the plan in
 * docs/integration_plan.md had the first two the other way round, x100):
 *   0x0000 VWC x10, %         (jumps at once when the probe is held)
 *   0x0001 soil temperature x10, °C, signed (rises slowly, follows SHT31)
 *   0x0002 bulk EC, µS/cm     (0 so far, also when held)
 * Every answer is checked: address, function, byte count and CRC; then
 * values outside 0..100 % or -40..80 °C are rejected as well.
 * Check against the probe's datasheet and the old Pi log.
 */
#include "sensors.h"

#include <math.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "soc/soc_caps.h"

static const char *TAG = "sensors";

#define SHT31_ADDR        0x44
#define I2C_TIMEOUT_MS    200
#define TRIES             3

#define PROBE_UART        UART_NUM_1
#define PROBE_REG_START   0x0000
#define PROBE_REG_COUNT   3
#define MODBUS_TIMEOUT_MS 300

#define PWR_PIN CONFIG_SAGRIOT_PROBE_PWR_GPIO
#define DE_PIN  CONFIG_SAGRIOT_RS485_DE_GPIO

#if CONFIG_SAGRIOT_PROBE_PWR_ACTIVE_LOW
#define PWR_ON_LEVEL  0
#else
#define PWR_ON_LEVEL  1
#endif
#define PWR_OFF_LEVEL (!PWR_ON_LEVEL)

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_sht;
static bool s_uart_ok;
static esp_err_t s_sht_err = ESP_ERR_INVALID_STATE;
static esp_err_t s_probe_err = ESP_ERR_INVALID_STATE;

/* ---------- probe power ---------- */

static void probe_power(bool on)
{
    if (PWR_PIN >= 0) {
        gpio_set_level(PWR_PIN, on ? PWR_ON_LEVEL : PWR_OFF_LEVEL);
    }
}

/* A pin held before deep sleep stays held after waking. Drive it to the
 * same idle level first, then release it, so it does not glitch. */
static void output_pin_init(int pin, int idle_level)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_set_level(pin, idle_level);
    gpio_config(&io);
    gpio_set_level(pin, idle_level);
    gpio_hold_dis(pin);
}

/* ---------- SHT31 ---------- */

static uint8_t sht31_crc(const uint8_t *data, size_t len)
{
    uint8_t crc = 0xFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int j = 0; j < 8; j++) {
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
        }
    }
    return crc;
}

static esp_err_t sht31_read(float *t, float *rh)
{
    if (!s_sht) {
        return ESP_ERR_INVALID_STATE;
    }
    const uint8_t cmd[2] = { 0x24, 0x00 };   /* single shot, high repeatability */
    esp_err_t err = i2c_master_transmit(s_sht, cmd, sizeof cmd, I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(20));           /* max 15.5 ms */

    uint8_t d[6];
    err = i2c_master_receive(s_sht, d, sizeof d, I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        return err;
    }
    if (sht31_crc(d, 2) != d[2] || sht31_crc(d + 3, 2) != d[5]) {
        return ESP_ERR_INVALID_CRC;
    }
    uint16_t raw_t = ((uint16_t)d[0] << 8) | d[1];
    uint16_t raw_h = ((uint16_t)d[3] << 8) | d[4];
    *t = -45.0f + 175.0f * raw_t / 65535.0f;
    *rh = 100.0f * raw_h / 65535.0f;
    return ESP_OK;
}

/* ---------- Modbus RTU ---------- */

static uint16_t modbus_crc(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int j = 0; j < 8; j++) {
            crc = (crc & 1) ? (crc >> 1) ^ 0xA001 : crc >> 1;
        }
    }
    return crc;
}

/* Function 0x03, read holding registers. */
static esp_err_t modbus_read(uint8_t slave, uint16_t start, uint16_t n, uint16_t *out)
{
    if (!s_uart_ok) {
        return ESP_ERR_INVALID_STATE;
    }
    uint8_t req[8] = { slave, 0x03, start >> 8, start & 0xFF, n >> 8, n & 0xFF };
    uint16_t crc = modbus_crc(req, 6);
    req[6] = crc & 0xFF;          /* CRC low byte first */
    req[7] = crc >> 8;

    uart_flush_input(PROBE_UART);
    uart_write_bytes(PROBE_UART, req, sizeof req);
    uart_wait_tx_done(PROBE_UART, pdMS_TO_TICKS(50));

    /* id + func + byte count + 2n data + CRC */
    uint8_t resp[5 + 2 * PROBE_REG_COUNT];
    const int expected = 5 + 2 * n;
    if (expected > (int)sizeof resp) {
        return ESP_ERR_INVALID_SIZE;
    }
    int got = uart_read_bytes(PROBE_UART, resp, expected, pdMS_TO_TICKS(MODBUS_TIMEOUT_MS));
    if (got != expected) {
        ESP_LOGW(TAG, "probe: %d/%d bytes", got, expected);
        return ESP_ERR_TIMEOUT;
    }
    if (resp[0] != slave || resp[1] != 0x03 || resp[2] != 2 * n) {
        ESP_LOGW(TAG, "probe: unexpected answer %02x %02x %02x", resp[0], resp[1], resp[2]);
        return ESP_ERR_INVALID_RESPONSE;
    }
    uint16_t rx_crc = resp[expected - 2] | ((uint16_t)resp[expected - 1] << 8);
    if (modbus_crc(resp, expected - 2) != rx_crc) {
        return ESP_ERR_INVALID_CRC;
    }
    for (int i = 0; i < n; i++) {
        out[i] = ((uint16_t)resp[3 + 2 * i] << 8) | resp[4 + 2 * i];
    }
    return ESP_OK;
}

/* ---------- public ---------- */

esp_err_t sensors_init(void)
{
    esp_err_t first = ESP_OK;

    if (PWR_PIN >= 0) {
        output_pin_init(PWR_PIN, PWR_OFF_LEVEL);
    }
    if (DE_PIN >= 0) {
        output_pin_init(DE_PIN, 0);   /* receive; the UART takes it over below */
    }

    /* I2C, SHT31 */
    i2c_master_bus_config_t bus = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = CONFIG_SAGRIOT_I2C_SDA_GPIO,
        .scl_io_num = CONFIG_SAGRIOT_I2C_SCL_GPIO,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t err = i2c_new_master_bus(&bus, &s_bus);
    if (err == ESP_OK) {
        /* 0x44 (ADDR low) on most boards, 0x45 when ADDR is tied high */
        uint16_t addr = SHT31_ADDR;
        if (i2c_master_probe(s_bus, SHT31_ADDR, I2C_TIMEOUT_MS) != ESP_OK) {
            if (i2c_master_probe(s_bus, SHT31_ADDR + 1, I2C_TIMEOUT_MS) == ESP_OK) {
                addr = SHT31_ADDR + 1;
            } else {
                ESP_LOGW(TAG, "no SHT31 at 0x44 or 0x45 (SDA %d, SCL %d): check wiring and 3.3 V",
                         CONFIG_SAGRIOT_I2C_SDA_GPIO, CONFIG_SAGRIOT_I2C_SCL_GPIO);
            }
        }
        ESP_LOGI(TAG, "SHT31 at 0x%02x", addr);
        i2c_device_config_t dev = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = addr,
            .scl_speed_hz = 100000,
        };
        err = i2c_master_bus_add_device(s_bus, &dev, &s_sht);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2C init failed: %s", esp_err_to_name(err));
        s_sht = NULL;
        first = err;
    }

    /* UART, RS485 probe */
    uart_config_t uc = {
        .baud_rate = CONFIG_SAGRIOT_PROBE_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    err = uart_driver_install(PROBE_UART, 256, 0, 0, NULL, 0);
    if (err == ESP_OK) {
        err = uart_param_config(PROBE_UART, &uc);
    }
    if (err == ESP_OK) {
        /* With a DE pin the UART drives it (RTS) in RS485 half-duplex mode;
         * without one the module switches direction by itself. */
        err = uart_set_pin(PROBE_UART, CONFIG_SAGRIOT_RS485_TX_GPIO, CONFIG_SAGRIOT_RS485_RX_GPIO,
                           DE_PIN >= 0 ? DE_PIN : UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    }
    if (err == ESP_OK && DE_PIN >= 0) {
        err = uart_set_mode(PROBE_UART, UART_MODE_RS485_HALF_DUPLEX);
    }
    s_uart_ok = (err == ESP_OK);
    if (!s_uart_ok) {
        ESP_LOGE(TAG, "UART init failed: %s", esp_err_to_name(err));
        if (first == ESP_OK) {
            first = err;
        }
    }
    return first;
}

esp_err_t sensors_read(sensors_reading_t *out)
{
    *out = (sensors_reading_t){ NAN, NAN, NAN, NAN, NAN, 0 };

    /* probe */
    probe_power(true);
    if (PWR_PIN >= 0) {
        vTaskDelay(pdMS_TO_TICKS(CONFIG_SAGRIOT_PROBE_WARMUP_MS));
    }
    uint16_t regs[PROBE_REG_COUNT];
    esp_err_t err = ESP_FAIL;
    for (int i = 0; i < TRIES && err != ESP_OK; i++) {
        err = modbus_read(CONFIG_SAGRIOT_PROBE_SLAVE_ID, PROBE_REG_START, PROBE_REG_COUNT, regs);
        if (err != ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
    probe_power(false);
    s_probe_err = err;
    if (err == ESP_OK) {
        /* raw values (hex and decimal), to check the register map */
        ESP_LOGI(TAG, "probe raw: 0x0000=0x%04X (%u) 0x0001=0x%04X (%u) 0x0002=0x%04X (%u)",
                 regs[0], regs[0], regs[1], regs[1], regs[2], regs[2]);
        /* Measured on 20 Nov (see the file header): 0x0000 jumps at once
         * when the probe is held (moisture), 0x0001 rises slowly and
         * follows the SHT31 (temperature, °C). Both x10. */
        float vwc = regs[0] / 10.0f;
        float soil_t = (int16_t)regs[1] / 10.0f;
        if (vwc < 0.0f || vwc > 100.0f || soil_t < -40.0f || soil_t > 80.0f) {
            /* CRC was fine, but the value cannot be real */
            ESP_LOGW(TAG, "probe value out of range: vwc %.1f %%, soil_t %.1f C", vwc, soil_t);
            err = ESP_ERR_INVALID_RESPONSE;
            s_probe_err = err;
            out->err |= SENSORS_ERR_PROBE;
        } else {
            out->vwc = vwc;
            out->soil_t = soil_t;
            out->ec = (float)regs[2];
        }
    } else {
        ESP_LOGW(TAG, "probe read failed: %s", esp_err_to_name(err));
        out->err |= SENSORS_ERR_PROBE;
    }

    /* SHT31 */
    err = ESP_FAIL;
    for (int i = 0; i < TRIES && err != ESP_OK; i++) {
        err = sht31_read(&out->air_t, &out->rh);
        if (err != ESP_OK && s_bus) {
            /* after a timeout the bus stays busy (INVALID_STATE): free it */
            i2c_master_bus_reset(s_bus);
            vTaskDelay(pdMS_TO_TICKS(20));
        }
    }
    s_sht_err = err;
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SHT31 read failed: %s", esp_err_to_name(err));
        out->air_t = NAN;
        out->rh = NAN;
        out->err |= SENSORS_ERR_SHT31;
    }

    return out->err == 0 ? ESP_OK : ESP_FAIL;
}

void sensors_last_errors(esp_err_t *sht31, esp_err_t *probe)
{
    *sht31 = s_sht_err;
    *probe = s_probe_err;
}

void sensors_prepare_sleep(void)
{
    bool held = false;
    if (PWR_PIN >= 0) {
        probe_power(false);
        gpio_hold_en(PWR_PIN);
        held = true;
    }
    if (DE_PIN >= 0) {
        /* take DE back from the UART and keep the transceiver receiving */
        output_pin_init(DE_PIN, 0);
        gpio_hold_en(DE_PIN);
        held = true;
    }
#if !SOC_GPIO_SUPPORT_HOLD_SINGLE_IO_IN_DSLP
    if (held) {
        gpio_deep_sleep_hold_en();   /* ESP32-S3: needed for digital pads */
    }
#else
    (void)held;
#endif
}
