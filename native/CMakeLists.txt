cmake_minimum_required(VERSION 3.20)
project(TwilightHarnessNative LANGUAGES CXX RC)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

configure_file(resources/twilight.rc.in "${CMAKE_CURRENT_BINARY_DIR}/twilight.rc" @ONLY)
add_executable(twilight-native WIN32 src/main.cpp src/sidecar_client.cpp src/text_util.cpp src/markdown.cpp src/chat_view.cpp "${CMAKE_CURRENT_BINARY_DIR}/twilight.rc")
target_compile_definitions(twilight-native PRIVATE UNICODE _UNICODE)
target_compile_options(twilight-native PRIVATE /utf-8)
target_link_libraries(twilight-native PRIVATE gdiplus user32 gdi32 comctl32 winhttp winmm imm32)

add_custom_command(TARGET twilight-native POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_directory
            "${CMAKE_CURRENT_SOURCE_DIR}/assets"
            "$<TARGET_FILE_DIR:twilight-native>/assets")
