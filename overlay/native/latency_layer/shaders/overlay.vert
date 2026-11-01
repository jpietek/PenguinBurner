#version 450

// Positions arrive in clip space: the CPU already knows the swapchain extent
// when it builds the quads, so there is nothing for a uniform to carry.
layout(location = 0) in vec2 in_position;
layout(location = 1) in vec4 in_color;
// Atlas coordinates for a glyph. A negative u marks a solid quad -- the panel,
// a graph bar, the median rule -- so both kinds share one pipeline.
layout(location = 2) in vec2 in_uv;

layout(location = 0) out vec4 frag_color;
layout(location = 1) out vec2 frag_uv;

void main() {
    gl_Position = vec4(in_position, 0.0, 1.0);
    frag_color = in_color;
    frag_uv = in_uv;
}
