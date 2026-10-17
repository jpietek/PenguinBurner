// In-present-path Vulkan overlay rendering (bitmap glyphs).
#include "latency_layer_internal.h"

namespace pblayer {
VkClearColorValue overlay_color(float r, float g, float b, float a) {
    VkClearColorValue value{};
    value.float32[0] = r;
    value.float32[1] = g;
    value.float32[2] = b;
    value.float32[3] = a;
    return value;
}

std::array<uint8_t, 7> overlay_glyph_rows(char ch) {
    switch (static_cast<char>(std::toupper(static_cast<unsigned char>(ch)))) {
        case '0': return {0x0e, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0e};
        case '1': return {0x04, 0x0c, 0x04, 0x04, 0x04, 0x04, 0x0e};
        case '2': return {0x0e, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1f};
        case '3': return {0x1e, 0x01, 0x01, 0x0e, 0x01, 0x01, 0x1e};
        case '4': return {0x02, 0x06, 0x0a, 0x12, 0x1f, 0x02, 0x02};
        case '5': return {0x1f, 0x10, 0x1e, 0x01, 0x01, 0x11, 0x0e};
        case '6': return {0x06, 0x08, 0x10, 0x1e, 0x11, 0x11, 0x0e};
        case '7': return {0x1f, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08};
        case '8': return {0x0e, 0x11, 0x11, 0x0e, 0x11, 0x11, 0x0e};
        case '9': return {0x0e, 0x11, 0x11, 0x0f, 0x01, 0x02, 0x0c};
        case 'A': return {0x0e, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11};
        case 'B': return {0x1e, 0x11, 0x11, 0x1e, 0x11, 0x11, 0x1e};
        case 'C': return {0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e};
        case 'D': return {0x1e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1e};
        case 'E': return {0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x1f};
        case 'F': return {0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x10};
        case 'G': return {0x0e, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0f};
        case 'H': return {0x11, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11};
        case 'I': return {0x0e, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0e};
        case 'J': return {0x01, 0x01, 0x01, 0x01, 0x11, 0x11, 0x0e};
        case 'K': return {0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11};
        case 'L': return {0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1f};
        case 'M': return {0x11, 0x1b, 0x15, 0x15, 0x11, 0x11, 0x11};
        case 'N': return {0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11};
        case 'O': return {0x0e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e};
        case 'P': return {0x1e, 0x11, 0x11, 0x1e, 0x10, 0x10, 0x10};
        case 'Q': return {0x0e, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0d};
        case 'R': return {0x1e, 0x11, 0x11, 0x1e, 0x14, 0x12, 0x11};
        case 'S': return {0x0f, 0x10, 0x10, 0x0e, 0x01, 0x01, 0x1e};
        case 'T': return {0x1f, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04};
        case 'U': return {0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e};
        case 'V': return {0x11, 0x11, 0x11, 0x11, 0x11, 0x0a, 0x04};
        case 'W': return {0x11, 0x11, 0x11, 0x15, 0x15, 0x1b, 0x11};
        case 'X': return {0x11, 0x11, 0x0a, 0x04, 0x0a, 0x11, 0x11};
        case 'Y': return {0x11, 0x11, 0x0a, 0x04, 0x04, 0x04, 0x04};
        case 'Z': return {0x1f, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1f};
        case '+': return {0x00, 0x04, 0x04, 0x1f, 0x04, 0x04, 0x00};
        case '-': return {0x00, 0x00, 0x00, 0x1f, 0x00, 0x00, 0x00};
        case '~': return {0x00, 0x00, 0x09, 0x16, 0x00, 0x00, 0x00};
        case '/': return {0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10};
        case '%': return {0x19, 0x19, 0x02, 0x04, 0x08, 0x13, 0x13};
        case '.': return {0x00, 0x00, 0x00, 0x00, 0x00, 0x06, 0x06};
        default: return {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
    }
}

uint32_t overlay_char_width(char ch, uint32_t scale) {
    return ch == ' ' ? 3u * scale : 5u * scale;
}

uint32_t overlay_text_width(const std::string& text, uint32_t scale) {
    uint32_t width = 0;
    bool first = true;
    for (char ch : text) {
        if (!first) {
            width += scale;
        }
        first = false;
        width += overlay_char_width(ch, scale);
    }
    return width;
}

uint32_t overlay_base_resolution_scale(uint32_t height) {
    // Snap the display height to the nearest standard tier (1080p/1440p/4K)
    // rather than assuming a 16:9 width, then map it to the bitmap-font pixel
    // scale. Keying off height keeps ultrawide and other odd resolutions on the
    // right tier (e.g. 3440x1440 stays 1440p, not 4K).
    struct Tier {
        uint32_t height;
        uint32_t scale;
    };
    static constexpr Tier kTiers[] = {
        {1080u, 2u},
        {1440u, 3u},
        {2160u, 4u},
    };
    uint32_t best_scale = kTiers[0].scale;
    uint32_t best_diff = height > kTiers[0].height
        ? height - kTiers[0].height
        : kTiers[0].height - height;
    for (const Tier& tier : kTiers) {
        const uint32_t diff = height > tier.height
            ? height - tier.height
            : tier.height - height;
        if (diff < best_diff) {
            best_diff = diff;
            best_scale = tier.scale;
        }
    }
    return best_scale;
}

uint32_t overlay_resolution_scale(const VkExtent2D& extent) {
    const uint32_t base = overlay_base_resolution_scale(extent.height);
    const double user_scale = g_overlay_user_scale.load(std::memory_order_relaxed);
    // Round to the nearest integer font scale (base values are positive, so a
    // simple +0.5 truncation rounds correctly), then keep at least 1 pixel.
    const double scaled = static_cast<double>(base) * user_scale;
    long rounded = static_cast<long>(scaled + 0.5);
    if (rounded < 1) {
        return 1u;
    }
    if (rounded > 8) {
        return 8u;
    }
    return static_cast<uint32_t>(rounded);
}

void overlay_clear_rect(
    const DeviceContext& device_context,
    VkCommandBuffer command_buffer,
    const VkExtent2D& extent,
    int32_t x,
    int32_t y,
    uint32_t width,
    uint32_t height,
    VkClearColorValue color) {
    if (!device_context.cmd_clear_attachments || !width || !height) {
        return;
    }
    if (x >= static_cast<int32_t>(extent.width)
        || y >= static_cast<int32_t>(extent.height)) {
        return;
    }
    if (x < 0) {
        const uint32_t clipped = static_cast<uint32_t>(-x);
        if (clipped >= width) {
            return;
        }
        width -= clipped;
        x = 0;
    }
    if (y < 0) {
        const uint32_t clipped = static_cast<uint32_t>(-y);
        if (clipped >= height) {
            return;
        }
        height -= clipped;
        y = 0;
    }
    width = std::min<uint32_t>(
        width,
        static_cast<uint32_t>(static_cast<int32_t>(extent.width) - x));
    height = std::min<uint32_t>(
        height,
        static_cast<uint32_t>(static_cast<int32_t>(extent.height) - y));
    VkClearAttachment attachment{};
    attachment.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    attachment.colorAttachment = 0;
    attachment.clearValue.color = color;
    VkClearRect rect{};
    rect.rect.offset = {x, y};
    rect.rect.extent = {width, height};
    rect.baseArrayLayer = 0;
    rect.layerCount = 1;
    device_context.cmd_clear_attachments(command_buffer, 1, &attachment, 1, &rect);
}

void overlay_draw_glyph(
    const DeviceContext& device_context,
    VkCommandBuffer command_buffer,
    const VkExtent2D& extent,
    char ch,
    int32_t x,
    int32_t y,
    uint32_t scale,
    VkClearColorValue color) {
    const auto rows = overlay_glyph_rows(ch);
    for (uint32_t row = 0; row < rows.size(); ++row) {
        uint8_t bits = rows[row];
        uint32_t col = 0;
        while (col < 5) {
            const bool enabled = (bits & (1u << (4u - col))) != 0;
            if (!enabled) {
                ++col;
                continue;
            }
            const uint32_t start_col = col;
            while (col < 5 && (bits & (1u << (4u - col)))) {
                ++col;
            }
            overlay_clear_rect(
                device_context,
                command_buffer,
                extent,
                x + static_cast<int32_t>(start_col * scale),
                y + static_cast<int32_t>(row * scale),
                (col - start_col) * scale,
                scale,
                color);
        }
    }
}

void overlay_draw_text(
    const DeviceContext& device_context,
    VkCommandBuffer command_buffer,
    const VkExtent2D& extent,
    const std::string& text) {
    if (text.empty() || extent.width < 64 || extent.height < 32) {
        return;
    }
    const uint32_t scale = overlay_resolution_scale(extent);
    const uint32_t padding = 2 * scale;
    const uint32_t margin = 5 * scale;
    const uint32_t text_height = 7 * scale;
    uint32_t text_width = overlay_text_width(text, scale);
    if (text_width + padding * 2 + margin * 2 > extent.width) {
        text_width = extent.width > margin * 2 + padding * 2
            ? extent.width - margin * 2 - padding * 2
            : 0;
    }
    if (!text_width) {
        return;
    }
    const int32_t x0 = static_cast<int32_t>(
        extent.width - text_width - padding * 2 - margin);
    const int32_t y0 = static_cast<int32_t>(margin);
    overlay_clear_rect(
        device_context,
        command_buffer,
        extent,
        x0,
        y0,
        text_width + padding * 2,
        text_height + padding * 2,
        overlay_color(0.0f, 0.0f, 0.0f, 1.0f));

    int32_t pen_x = x0 + static_cast<int32_t>(padding);
    const int32_t pen_y = y0 + static_cast<int32_t>(padding);
    const int32_t max_x = x0 + static_cast<int32_t>(padding + text_width);
    const VkClearColorValue text_color = overlay_color(1.0f, 1.0f, 1.0f, 1.0f);
    for (char ch : text) {
        const uint32_t advance = overlay_char_width(ch, scale);
        if (pen_x + static_cast<int32_t>(advance) > max_x) {
            break;
        }
        if (ch != ' ') {
            overlay_draw_glyph(
                device_context,
                command_buffer,
                extent,
                ch,
                pen_x,
                pen_y,
                scale,
                text_color);
        }
        pen_x += static_cast<int32_t>(advance + scale);
    }
}

bool overlay_device_functions_available(const DeviceContext& context) {
    return context.get_swapchain_images_khr && context.queue_submit
        && context.create_image_view && context.destroy_image_view
        && context.create_render_pass && context.destroy_render_pass
        && context.create_framebuffer && context.destroy_framebuffer
        && context.create_command_pool && context.destroy_command_pool
        && context.allocate_command_buffers && context.reset_command_buffer
        && context.begin_command_buffer && context.end_command_buffer
        && context.cmd_begin_render_pass && context.cmd_end_render_pass
        && context.cmd_clear_attachments && context.create_semaphore
        && context.destroy_semaphore && context.create_fence
        && context.destroy_fence && context.get_fence_status
        && context.reset_fences && context.wait_for_fences;
}

void destroy_overlay_resources(
    const DeviceContext& device_context,
    SwapchainContext& swapchain_context) {
    auto& overlay = swapchain_context.overlay;
    if (device_context.wait_for_fences) {
        std::vector<VkFence> pending;
        for (std::size_t i = 0; i < overlay.submit_fences.size(); ++i) {
            if (overlay.submit_fences[i] != VK_NULL_HANDLE
                && i < overlay.submit_pending.size()
                && overlay.submit_pending[i]) {
                pending.push_back(overlay.submit_fences[i]);
            }
        }
        if (!pending.empty()) {
            device_context.wait_for_fences(
                device_context.device,
                static_cast<uint32_t>(pending.size()),
                pending.data(),
                VK_TRUE,
                1000000000ull);
        }
    }
    for (VkFence fence : overlay.submit_fences) {
        if (fence != VK_NULL_HANDLE && device_context.destroy_fence) {
            device_context.destroy_fence(device_context.device, fence, nullptr);
        }
    }
    for (VkSemaphore semaphore : overlay.signal_semaphores) {
        if (semaphore != VK_NULL_HANDLE && device_context.destroy_semaphore) {
            device_context.destroy_semaphore(device_context.device, semaphore, nullptr);
        }
    }
    for (VkFramebuffer framebuffer : overlay.framebuffers) {
        if (framebuffer != VK_NULL_HANDLE && device_context.destroy_framebuffer) {
            device_context.destroy_framebuffer(device_context.device, framebuffer, nullptr);
        }
    }
    for (VkImageView image_view : overlay.image_views) {
        if (image_view != VK_NULL_HANDLE && device_context.destroy_image_view) {
            device_context.destroy_image_view(device_context.device, image_view, nullptr);
        }
    }
    if (overlay.command_pool != VK_NULL_HANDLE
        && device_context.destroy_command_pool) {
        device_context.destroy_command_pool(device_context.device, overlay.command_pool, nullptr);
    }
    if (overlay.render_pass != VK_NULL_HANDLE
        && device_context.destroy_render_pass) {
        device_context.destroy_render_pass(device_context.device, overlay.render_pass, nullptr);
    }
    overlay = {};
}

bool init_overlay_resources(
    const DeviceContext& device_context,
    VkSwapchainKHR swapchain,
    SwapchainContext& swapchain_context,
    uint32_t queue_family_index) {
    auto& overlay = swapchain_context.overlay;
    if (overlay.ready && overlay.queue_family_index == queue_family_index) {
        return true;
    }
    // Resources are bound to the first queue family that presents this
    // swapchain; presents from other families skip the overlay instead of
    // destroying resources that may still be in flight.
    if (overlay.attempted) {
        return false;
    }

    // A disabled overlay must NOT consume the one-shot init: the live toggle
    // can enable it later in this same process, and resources then still need
    // their first (and only) initialization attempt.
    if (!overlay_enabled()) {
        return false;
    }

    overlay.attempted = true;
    overlay.queue_family_index = queue_family_index;
    if (!overlay_device_functions_available(device_context)
        || queue_family_index == UINT32_MAX
        || swapchain_context.image_format == VK_FORMAT_UNDEFINED
        || swapchain_context.image_extent.width == 0
        || swapchain_context.image_extent.height == 0
        || swapchain_context.image_array_layers != 1
        || !(swapchain_context.image_usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT)) {
        return false;
    }

    uint32_t image_count = 0;
    VkResult result = device_context.get_swapchain_images_khr(
        device_context.device,
        swapchain,
        &image_count,
        nullptr);
    if (result != VK_SUCCESS || image_count == 0) {
        return false;
    }
    overlay.images.resize(image_count);
    result = device_context.get_swapchain_images_khr(
        device_context.device,
        swapchain,
        &image_count,
        overlay.images.data());
    if ((result != VK_SUCCESS && result != VK_INCOMPLETE) || image_count == 0) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }
    overlay.images.resize(image_count);

    VkAttachmentDescription attachment{};
    attachment.format = swapchain_context.image_format;
    attachment.samples = VK_SAMPLE_COUNT_1_BIT;
    attachment.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD;
    attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    attachment.initialLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    attachment.finalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;

    VkAttachmentReference color_attachment{};
    color_attachment.attachment = 0;
    color_attachment.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &color_attachment;

    std::array<VkSubpassDependency, 2> dependencies{};
    dependencies[0].srcSubpass = VK_SUBPASS_EXTERNAL;
    dependencies[0].dstSubpass = 0;
    dependencies[0].srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[0].dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[0].dstAccessMask =
        VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    dependencies[0].dependencyFlags = VK_DEPENDENCY_BY_REGION_BIT;
    dependencies[1].srcSubpass = 0;
    dependencies[1].dstSubpass = VK_SUBPASS_EXTERNAL;
    dependencies[1].srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[1].dstStageMask = VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT;
    dependencies[1].srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    dependencies[1].dependencyFlags = VK_DEPENDENCY_BY_REGION_BIT;

    VkRenderPassCreateInfo render_pass_info{};
    render_pass_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    render_pass_info.attachmentCount = 1;
    render_pass_info.pAttachments = &attachment;
    render_pass_info.subpassCount = 1;
    render_pass_info.pSubpasses = &subpass;
    render_pass_info.dependencyCount =
        static_cast<uint32_t>(dependencies.size());
    render_pass_info.pDependencies = dependencies.data();
    result = device_context.create_render_pass(
        device_context.device,
        &render_pass_info,
        nullptr,
        &overlay.render_pass);
    if (result != VK_SUCCESS) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }

    overlay.image_views.resize(image_count, VK_NULL_HANDLE);
    overlay.framebuffers.resize(image_count, VK_NULL_HANDLE);
    for (uint32_t i = 0; i < image_count; ++i) {
        VkImageViewCreateInfo view_info{};
        view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        view_info.image = overlay.images[i];
        view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
        view_info.format = swapchain_context.image_format;
        view_info.components = {
            VK_COMPONENT_SWIZZLE_IDENTITY,
            VK_COMPONENT_SWIZZLE_IDENTITY,
            VK_COMPONENT_SWIZZLE_IDENTITY,
            VK_COMPONENT_SWIZZLE_IDENTITY,
        };
        view_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        view_info.subresourceRange.baseMipLevel = 0;
        view_info.subresourceRange.levelCount = 1;
        view_info.subresourceRange.baseArrayLayer = 0;
        view_info.subresourceRange.layerCount = 1;
        result = device_context.create_image_view(
            device_context.device,
            &view_info,
            nullptr,
            &overlay.image_views[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }

        VkFramebufferCreateInfo framebuffer_info{};
        framebuffer_info.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
        framebuffer_info.renderPass = overlay.render_pass;
        framebuffer_info.attachmentCount = 1;
        framebuffer_info.pAttachments = &overlay.image_views[i];
        framebuffer_info.width = swapchain_context.image_extent.width;
        framebuffer_info.height = swapchain_context.image_extent.height;
        framebuffer_info.layers = 1;
        result = device_context.create_framebuffer(
            device_context.device,
            &framebuffer_info,
            nullptr,
            &overlay.framebuffers[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }
    }

    VkCommandPoolCreateInfo pool_info{};
    pool_info.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    pool_info.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pool_info.queueFamilyIndex = queue_family_index;
    result = device_context.create_command_pool(
        device_context.device,
        &pool_info,
        nullptr,
        &overlay.command_pool);
    if (result != VK_SUCCESS) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }

    overlay.command_buffers.resize(image_count, VK_NULL_HANDLE);
    VkCommandBufferAllocateInfo allocate_info{};
    allocate_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocate_info.commandPool = overlay.command_pool;
    allocate_info.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocate_info.commandBufferCount = image_count;
    result = device_context.allocate_command_buffers(
        device_context.device,
        &allocate_info,
        overlay.command_buffers.data());
    if (result != VK_SUCCESS) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }
    // A command buffer is dispatchable, and the one the layer below us
    // returned carries its dispatch table, not the one a layer above us reads.
    // The loader's callback restores that before the handle travels back up.
    if (device_context.set_device_loader_data) {
        for (VkCommandBuffer command_buffer : overlay.command_buffers) {
            result = device_context.set_device_loader_data(
                device_context.device,
                command_buffer);
            if (result != VK_SUCCESS) {
                destroy_overlay_resources(device_context, swapchain_context);
                return false;
            }
        }
    }

    overlay.signal_semaphores.resize(image_count, VK_NULL_HANDLE);
    VkSemaphoreCreateInfo semaphore_info{};
    semaphore_info.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;
    for (uint32_t i = 0; i < image_count; ++i) {
        result = device_context.create_semaphore(
            device_context.device,
            &semaphore_info,
            nullptr,
            &overlay.signal_semaphores[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }
    }

    overlay.submit_fences.resize(image_count, VK_NULL_HANDLE);
    overlay.submit_pending.assign(image_count, 0);
    VkFenceCreateInfo fence_info{};
    fence_info.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    for (uint32_t i = 0; i < image_count; ++i) {
        result = device_context.create_fence(
            device_context.device,
            &fence_info,
            nullptr,
            &overlay.submit_fences[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }
    }

    overlay.ready = true;
    return true;
}

VkSemaphore submit_overlay_locked(
    const DeviceContext& device_context,
    VkSwapchainKHR swapchain,
    SwapchainContext& swapchain_context,
    const QueueContext& queue_context,
    VkQueue queue,
    uint32_t image_index,
    const VkPresentInfoKHR* present_info,
    const std::string& text) {
    if (text.empty() || !overlay_enabled()
        || !(queue_context.queue_flags & VK_QUEUE_GRAPHICS_BIT)
        || !present_info) {
        return VK_NULL_HANDLE;
    }
    if (!init_overlay_resources(
            device_context,
            swapchain,
            swapchain_context,
            queue_context.queue_family_index)) {
        return VK_NULL_HANDLE;
    }
    auto& overlay = swapchain_context.overlay;
    if (image_index >= overlay.command_buffers.size()
        || image_index >= overlay.framebuffers.size()
        || image_index >= overlay.signal_semaphores.size()
        || image_index >= overlay.submit_fences.size()
        || image_index >= overlay.submit_pending.size()) {
        return VK_NULL_HANDLE;
    }

    // The previous submit that used this image's command buffer must have
    // retired before the buffer is reset; skip the overlay for this frame
    // rather than stall the present path.
    VkFence fence = overlay.submit_fences[image_index];
    if (overlay.submit_pending[image_index]) {
        if (device_context.get_fence_status(device_context.device, fence)
            != VK_SUCCESS) {
            return VK_NULL_HANDLE;
        }
        overlay.submit_pending[image_index] = 0;
    }
    if (device_context.reset_fences(device_context.device, 1, &fence)
        != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    VkCommandBuffer command_buffer = overlay.command_buffers[image_index];
    VkResult result = device_context.reset_command_buffer(command_buffer, 0);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    VkCommandBufferBeginInfo begin_info{};
    begin_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    begin_info.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    result = device_context.begin_command_buffer(command_buffer, &begin_info);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    VkRenderPassBeginInfo render_pass_info{};
    render_pass_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    render_pass_info.renderPass = overlay.render_pass;
    render_pass_info.framebuffer = overlay.framebuffers[image_index];
    render_pass_info.renderArea.offset = {0, 0};
    render_pass_info.renderArea.extent = swapchain_context.image_extent;
    device_context.cmd_begin_render_pass(
        command_buffer,
        &render_pass_info,
        VK_SUBPASS_CONTENTS_INLINE);
    overlay_draw_text(
        device_context,
        command_buffer,
        swapchain_context.image_extent,
        text);
    device_context.cmd_end_render_pass(command_buffer);

    result = device_context.end_command_buffer(command_buffer);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    std::vector<VkPipelineStageFlags> wait_stages(
        present_info->waitSemaphoreCount,
        VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT);
    VkSemaphore signal = overlay.signal_semaphores[image_index];
    VkSubmitInfo submit_info{};
    submit_info.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submit_info.waitSemaphoreCount = present_info->waitSemaphoreCount;
    submit_info.pWaitSemaphores = present_info->pWaitSemaphores;
    submit_info.pWaitDstStageMask =
        wait_stages.empty() ? nullptr : wait_stages.data();
    submit_info.commandBufferCount = 1;
    submit_info.pCommandBuffers = &command_buffer;
    submit_info.signalSemaphoreCount = 1;
    submit_info.pSignalSemaphores = &signal;
    result = device_context.queue_submit(queue, 1, &submit_info, fence);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }
    overlay.submit_pending[image_index] = 1;
    return signal;
}

}  // namespace pblayer
