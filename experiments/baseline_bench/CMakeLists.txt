cmake_minimum_required(VERSION 3.16)
# Uses the model and the recorded log from the node firmware.
set(EXTRA_COMPONENT_DIRS
    ${CMAKE_CURRENT_LIST_DIR}/../../firmware/components/baseline
    ${CMAKE_CURRENT_LIST_DIR}/../../firmware/components/replay)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(baseline_bench)
