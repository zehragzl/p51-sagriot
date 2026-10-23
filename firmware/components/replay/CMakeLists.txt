# Recorded 21-day internship log (6020 samples at 5 min) and the decisions the
# Python reference gives on it. Stands in for the sensors until the drivers
# exist, and lets experiments/baseline_bench check the port on the chip.
idf_component_register(SRCS "replay_data.c"
                       INCLUDE_DIRS "include")
