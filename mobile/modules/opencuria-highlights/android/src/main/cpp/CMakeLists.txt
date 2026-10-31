cmake_minimum_required(VERSION 3.22.1)

project(opencuriahighlights LANGUAGES C)

set(OPENCURIA_HIGHLIGHTS_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../..")

add_library(
  opencuriahighlights SHARED
  opencuria_highlights_jni.c
  "${OPENCURIA_HIGHLIGHTS_ROOT}/cpp/opencuria_highlights.c"
  "${OPENCURIA_HIGHLIGHTS_ROOT}/Vendor/highlights/highlights.c"
  "${OPENCURIA_HIGHLIGHTS_ROOT}/Vendor/wasm2c/wasm-rt-impl.c"
  "${OPENCURIA_HIGHLIGHTS_ROOT}/Vendor/wasm2c/wasm-rt-mem-impl.c"
)

target_include_directories(
  opencuriahighlights
  PRIVATE "${OPENCURIA_HIGHLIGHTS_ROOT}/cpp" "${OPENCURIA_HIGHLIGHTS_ROOT}/Vendor"
          "${OPENCURIA_HIGHLIGHTS_ROOT}/Vendor/highlights" "${OPENCURIA_HIGHLIGHTS_ROOT}/Vendor/wasm2c"
)

# Plain malloc memory with explicit bounds checks: no guard-page signal
# handlers competing with ART's.
target_compile_definitions(
  opencuriahighlights PRIVATE WASM_RT_USE_MMAP=0 WASM_RT_MEMCHECK_BOUNDS_CHECK=1
                       WASM_RT_SKIP_SIGNAL_RECOVERY=1
)

# The lexers are generated code; keep them fast in debug builds too.
target_compile_options(opencuriahighlights PRIVATE -O2)

target_link_options(
  opencuriahighlights PRIVATE "-Wl,-z,common-page-size=16384" "-Wl,-z,max-page-size=16384"
)
