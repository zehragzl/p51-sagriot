cmake_minimum_required(VERSION 3.16)

# Personal settings (Wi-Fi, broker IP) live in sdkconfig.local, which is not
# committed. Copy sdkconfig.local.example to start.
set(SDKCONFIG_DEFAULTS "sdkconfig.defaults")
if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.local")
    list(APPEND SDKCONFIG_DEFAULTS "sdkconfig.local")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(sagriot_node)
