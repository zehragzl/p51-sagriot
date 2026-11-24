# sensors (Rüya)

Sensor drivers for the node: SHT31 (I²C, air temperature and humidity) and
the RS485 / Modbus RTU soil probe (VWC, soil temperature, bulk EC), plus the
P-MOSFET that switches the probe power. Interface: `include/sensors.h`
(as in `docs/integration_plan.md`, section 3).

Used when `CONFIG_SAGRIOT_SOURCE_SENSORS=y`; `main.c` calls it from
`read_sample()`. Otherwise the node replays the recorded log.

## Wiring (Kconfig defaults, "SAgrIoT sensors" menu)

| Signal | ESP32-S3 pin | Setting |
|---|---|---|
| SHT31 SDA | GPIO 8 | `SAGRIOT_I2C_SDA_GPIO` |
| SHT31 SCL | GPIO 9 | `SAGRIOT_I2C_SCL_GPIO` |
| RS485 DI (ESP TX) | GPIO 17 | `SAGRIOT_RS485_TX_GPIO` |
| RS485 RO (ESP RX) | GPIO 18 | `SAGRIOT_RS485_RX_GPIO` |
| RS485 DE+RE | -1 (auto-direction module) | `SAGRIOT_RS485_DE_GPIO` |
| Probe MOSFET gate | -1 (probe always on) | `SAGRIOT_PROBE_PWR_GPIO`, `SAGRIOT_PROBE_PWR_ACTIVE_LOW` |

12 V adapter, ESP and RS485 module share GND. Internal I²C pull-ups are on;
add 4.7 kΩ external ones for long wires. Put a pull-up on the P-MOSFET gate
so the probe stays off while the ESP resets.

## Probe registers

Function 0x03, address `SAGRIOT_PROBE_SLAVE_ID` (1), 9600 8N1, three
registers from 0x0000, as measured on 20 Nov 2026:

| Register | Value | How we know |
|---|---|---|
| 0x0000 | VWC ×10, % | jumps at once when the probe is held (13.6 → 82.9 → 18.1) |
| 0x0001 | soil temperature ×10, °C, signed | rises slowly in the hand (27.7 → 34.2), close to the SHT31 |
| 0x0002 | bulk EC, µS/cm | stays 0, also when held |

The plan had 0x0000 and 0x0001 the other way round and ×100. All
temperatures are °C; nothing is in Fahrenheit. Every answer is checked
(address, function, byte count, CRC) and values outside 0–100 % or
−40–80 °C are rejected (`err` bit 1).
The reading payload carries EC in dS/m (`main.c` divides by 1000).
**Still to check** against the datasheet and the old Pi log.

## Errors

A value that cannot be read (3 tries) is `NAN` and goes out as `null`;
`err` bit 0 = SHT31, bit 1 = probe.

## Deep sleep

`sensors_prepare_sleep()` turns the probe off and holds the MOSFET gate and
DE pins at their idle level during deep sleep; `sensors_init()` releases
them after waking.
