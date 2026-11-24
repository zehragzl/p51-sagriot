idf_component_register(SRCS "tinyml.c" "tinyml_weights.c"
                       INCLUDE_DIRS "include"
                       PRIV_INCLUDE_DIRS "."
                       REQUIRES baseline)
