cmake_minimum_required(VERSION 3.16)
# Use the baseline component from the main firmware tree.
set(EXTRA_COMPONENT_DIRS ${CMAKE_CURRENT_LIST_DIR}/../../components/baseline)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(baseline_bench)
