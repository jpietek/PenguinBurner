cmake_minimum_required(VERSION 3.20)

project(latency_layer LANGUAGES CXX)

enable_testing()

include(GNUInstallDirs)

find_path(VULKAN_INCLUDE_DIR vulkan/vulkan.h REQUIRED)

add_library(latency_layer SHARED
  src/latency_state.cpp
  src/overlay_text.cpp
  src/overlay_render.cpp
  src/telemetry_events.cpp
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
