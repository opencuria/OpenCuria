cmake_minimum_required(VERSION 3.22.1)

project(opencuriaterminal LANGUAGES CXX)

add_library(ghostty-vt SHARED IMPORTED)
set_target_properties(
  ghostty-vt
  PROPERTIES IMPORTED_LOCATION
             "${CMAKE_CURRENT_SOURCE_DIR}/../jniLibs/${ANDROID_ABI}/libghostty-vt.so"
)

add_library(opencuriaterminal SHARED opencuria_terminal_jni.cpp)

# Vendored libghostty-vt C headers (module root: android/src/main/cpp/../../../..).
target_include_directories(
  opencuriaterminal
  PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../../../../Vendor/libghostty-vt/include"
)

target_link_options(
  opencuriaterminal PRIVATE "-Wl,-z,common-page-size=16384" "-Wl,-z,max-page-size=16384"
)

target_link_libraries(opencuriaterminal PRIVATE ghostty-vt log)
