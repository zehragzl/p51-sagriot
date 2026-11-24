idf_component_register(SRCS "main.c" "tinyml_expected.c"
                       INCLUDE_DIRS "."
                       REQUIRES baseline tinyml replay esp_timer)
