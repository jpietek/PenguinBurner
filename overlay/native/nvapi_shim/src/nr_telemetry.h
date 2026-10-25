#pragma once

// Same bounded, asynchronous output path as Reflex markers.
using NrEmit = void (*)(const char*, int);
void start_nr_telemetry(NrEmit emit);
