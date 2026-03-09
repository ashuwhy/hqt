cmake_minimum_required(VERSION 3.16)
project(lob_server)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_BUILD_TYPE Release)

find_package(PkgConfig REQUIRED)
pkg_check_modules(RDKAFKA REQUIRED rdkafka rdkafka++)

include(FetchContent)

# simdjson — replaces nlohmann, ~20x faster
FetchContent_Declare(simdjson
    GIT_REPOSITORY https://github.com/simdjson/simdjson.git
    GIT_TAG v3.9.2
    GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(simdjson)

# prometheus-cpp
FetchContent_Declare(prometheus-cpp
    GIT_REPOSITORY https://github.com/jupp0r/prometheus-cpp.git
    GIT_TAG v1.1.0
    GIT_SHALLOW TRUE
)
set(ENABLE_TESTING OFF CACHE BOOL "" FORCE)
set(ENABLE_PUSH    OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(prometheus-cpp)

# Drogon — replaces Crow
FetchContent_Declare(drogon
    GIT_REPOSITORY https://github.com/drogonframework/drogon.git
    GIT_TAG v1.9.5
    GIT_SHALLOW TRUE
)
set(BUILD_DROGON_SHARED_LIBS OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(drogon)

add_subdirectory(engine/cpp engine)

add_executable(lob_server lob_server.cpp)
target_include_directories(lob_server PRIVATE
    ${RDKAFKA_INCLUDE_DIRS}
    ${CMAKE_CURRENT_SOURCE_DIR}/engine/cpp/include
)
target_link_libraries(lob_server PRIVATE
    drogon trantor
    ${RDKAFKA_LIBRARIES}
    prometheus-cpp::core
    prometheus-cpp::pull
    lob_core lob_util
    simdjson pthread
)
