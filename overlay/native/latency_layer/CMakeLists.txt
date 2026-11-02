cmake_minimum_required(VERSION 3.20)

project(latency_layer LANGUAGES CXX)

enable_testing()

include(GNUInstallDirs)

# Keep release features independent of the build distribution's header age.
# The pinned C headers are included in source distributions for offline builds.
set(VULKAN_INCLUDE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/vendor/vulkan-headers/include"
    CACHE PATH "Vulkan headers (must support VK_EXT_present_timing)")
include(CheckCXXSourceCompiles)
set(CMAKE_REQUIRED_INCLUDES "${VULKAN_INCLUDE_DIR}")
unset(PB_HAS_PRESENT_TIMING CACHE)
check_cxx_source_compiles("
#include <vulkan/vulkan.h>
#ifndef VK_EXT_present_timing
#error VK_EXT_present_timing is required
#endif
int main() { VkPresentTimingSurfaceCapabilitiesEXT caps{}; return int(caps.presentTimingSupported); }
" PB_HAS_PRESENT_TIMING)
unset(CMAKE_REQUIRED_INCLUDES)
if(NOT PB_HAS_PRESENT_TIMING)
  message(FATAL_ERROR "Vulkan headers must support VK_EXT_present_timing; use the bundled headers or Vulkan-Headers v1.4.341 or newer")
endif()

add_library(latency_layer SHARED
  src/base_timing.cpp
  src/latency_state.cpp
  src/overlay_text.cpp
  src/overlay_render.cpp
  src/overlay_worker.cpp
  src/telemetry_events.cpp
  src/display_timing.cpp
  src/frame_timing.cpp
  src/marker_timing.cpp
  src/latency_layer.cpp
)

target_include_directories(latency_layer PRIVATE
  "${VULKAN_INCLUDE_DIR}"
)

target_compile_features(latency_layer PRIVATE cxx_std_17)

if(CMAKE_SIZEOF_VOID_P EQUAL 8)
  set(PB_LAYER_LIBRARY_ARCH "64")
else()
  set(PB_LAYER_LIBRARY_ARCH "32")
endif()

# The 32-bit build ships alongside the 64-bit one in the same directory, so its
# library needs a distinct file name. The Vulkan loader reads library_arch and
# loads whichever manifest matches the target application, so both keep the same
# layer name. Empty for the default (64-bit) build.
set(PB_LAYER_NAME_SUFFIX "" CACHE STRING
  "Suffix for the layer library file name, e.g. _i386 for the 32-bit build")

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(latency_layer PRIVATE
    -Wall
    -Wextra
    -Wpedantic
    -fvisibility=hidden
  )
endif()

# Output name, manifest, and layer name stay on the public contract referenced by
# the Python overlay package, packaging, and tests; only the C++ sources were
# de-prefixed and split.
set_target_properties(latency_layer PROPERTIES
  OUTPUT_NAME "VkLayer_penguinburner_latency${PB_LAYER_NAME_SUFFIX}"
)

set(PB_LAYER_LIBRARY_PATH
  "./libVkLayer_penguinburner_latency${PB_LAYER_NAME_SUFFIX}.so")
configure_file(
  VkLayer_PENGUINBURNER_latency.json.in
  "${CMAKE_CURRENT_BINARY_DIR}/VkLayer_PENGUINBURNER_latency.json"
  @ONLY
)

set(PB_LAYER_LIBRARY_PATH
  "${CMAKE_INSTALL_FULL_LIBDIR}/libVkLayer_penguinburner_latency${PB_LAYER_NAME_SUFFIX}.so"
)
configure_file(
  VkLayer_PENGUINBURNER_latency.json.in
  "${CMAKE_CURRENT_BINARY_DIR}/install/VkLayer_PENGUINBURNER_latency.json"
  @ONLY
)

install(TARGETS latency_layer
  LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}"
)

install(
  FILES "${CMAKE_CURRENT_BINARY_DIR}/install/VkLayer_PENGUINBURNER_latency.json"
  DESTINATION "${CMAKE_INSTALL_DATADIR}/vulkan/implicit_layer.d"
)
