idf_component_register(SRCS "sensors.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_driver_i2c esp_driver_uart esp_driver_gpio)
