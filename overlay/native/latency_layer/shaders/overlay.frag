#version 450

layout(set = 0, binding = 0) uniform sampler2D font_atlas;

layout(location = 0) in vec4 frag_color;
layout(location = 1) in vec2 frag_uv;

layout(location = 0) out vec4 out_color;

// How far the dark halo reaches past the letterform, in screen pixels. One
// pixel is enough to separate the type from the game without thickening it.
const float kHaloPixels = 1.0;

void main() {
    float coverage = 1.0;
    float halo = 1.0;
    if (frag_uv.x >= 0.0) {
        // The atlas stores a signed distance with the edge at 0.5. Recovering
        // it against the screen-space rate of change is what keeps type sharp
        // at any magnification instead of blurring like a coverage atlas.
        float distance = texture(font_atlas, frag_uv).r;
        // fwidth() is the field's change across a whole pixel, so half of it is
        // the textbook one-pixel transition. Four fifths of that keeps the edge
        // from eating the stroke of a nineteen-pixel face, whose stems are
        // barely two pixels across, and still tracks the scale.
        float softness = max(fwidth(distance) * 0.40, 0.0001);
        coverage = smoothstep(0.5 - softness, 0.5 + softness, distance);
        // The swapchain is not an sRGB format, so the blend that follows
        // happens in encoded space and leaves the partly covered pixels at the
        // edge darker than their coverage deserves. On light-on-dark text -- the
        // only kind this overlay draws -- that reads as a hard, thin edge.
        // Lifting the coverage curve restores the weight the letterform should
        // have without widening the outline itself.
        coverage = pow(coverage, 1.0 / 1.8);
        // A dark halo just outside the letterform, the way a game HUD needs.
        // The panel behind the text is translucent, so on a bright scene the
        // type had only its own colour holding it apart from the game, and at
        // body size a regular-weight stem is barely two pixels of it. The halo
        // gives every glyph its own contrast without making the stroke wider.
        float halo_width = fwidth(distance) * kHaloPixels;
        halo = smoothstep(
            0.5 - halo_width - softness, 0.5 - halo_width + softness, distance);
        halo = pow(halo, 1.0 / 1.8);
    }
    // Colour fades to black across the halo while the alpha covers both, so
    // the blend lays a dark edge under the glyph and the glyph over it.
    out_color = vec4(frag_color.rgb * coverage, frag_color.a * halo);
}
