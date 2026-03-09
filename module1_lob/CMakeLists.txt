cmake_minimum_required(VERSION 3.16)
project(lob_server)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_BUILD_TYPE Release)

find_package(PkgConfig REQUIRED)
pkg_check_modules(RDKAFKA REQUIRED rdkafka++)

include(FetchContent)

# Fetch Crow
FetchContent_Declare(Crow
  GIT_REPOSITORY https://github.com/CrowCpp/Crow.git
  GIT_TAG v1.2.0
)
FetchContent_MakeAvailable(Crow)

# Fetch prometheus-cpp
FetchContent_Declare(prometheus-cpp
  GIT_REPOSITORY https://github.com/jupp0r/prometheus-cpp.git
  GIT_TAG v1.1.0
)
set(ENABLE_TESTING OFF CACHE BOOL "" FORCE)
set(ENABLE_PUSH OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(prometheus-cpp)

add_subdirectory(engine/cpp engine)

add_executable(lob_server lob_server.cpp)
target_include_directories(lob_server PRIVATE 
    ${RDKAFKA_INCLUDE_DIRS}
    ${CMAKE_CURRENT_SOURCE_DIR}/engine/cpp/include
)
target_link_libraries(lob_server PRIVATE 
    Crow::Crow
    ${RDKAFKA_LIBRARIES}
    prometheus-cpp::core
    prometheus-cpp::pull
    ashuwhy_lob_core
    pthread
)
