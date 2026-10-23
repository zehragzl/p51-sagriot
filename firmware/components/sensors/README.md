# sensors (Rüya)

Sensor drivers for the node: SHT31 (I²C, air temperature and humidity) and
the RS485 / Modbus RTU soil probe (VWC, soil temperature, bulk EC), plus the
P-MOSFET that switches the probe power.

Not an ESP-IDF component yet. When there is code, add a `CMakeLists.txt`
(see `../baseline/CMakeLists.txt`). `firmware/main/main.c` then calls the
driver in `read_sample()` instead of the replay data.
Fields to fill: see `docs/message_contract.md` (reading payload).
