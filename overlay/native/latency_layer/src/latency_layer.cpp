// Vulkan layer entry points and loader dispatch.
#include "latency_layer_internal.h"

namespace pblayer {
VKAPI_ATTR VkResult VKAPI_CALL layer_create_instance(
    const VkInstanceCreateInfo* create_info,
    const VkAllocationCallbacks* allocator,
    VkInstance* instance) {
    send_status_event_once(
        "create-instance-enter",
        g_reported_create_instance_enter);

    if (!create_info || !instance) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    auto* link = find_layer_chain_entry(create_info);
    if (!link || !link->u.pLayerInfo || !link->u.pLayerInfo->pfnNextGetInstanceProcAddr) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    PFN_vkGetInstanceProcAddr next_get_instance_proc_addr =
        link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;

    auto next_create_instance = reinterpret_cast<PFN_vkCreateInstance>(
        next_get_instance_proc_addr(VK_NULL_HANDLE, "vkCreateInstance"));
    if (!next_create_instance) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    VkResult result = next_create_instance(create_info, allocator, instance);
    if (result != VK_SUCCESS) {
        return result;
    }

    InstanceContext context{};
    context.get_instance_proc_addr = next_get_instance_proc_addr;
    context.destroy_instance = reinterpret_cast<PFN_vkDestroyInstance>(
        next_get_instance_proc_addr(*instance, "vkDestroyInstance"));
    context.create_device = reinterpret_cast<PFN_vkCreateDevice>(
        next_get_instance_proc_addr(*instance, "vkCreateDevice"));
    context.enumerate_physical_devices =
        reinterpret_cast<PFN_vkEnumeratePhysicalDevices>(
            next_get_instance_proc_addr(*instance, "vkEnumeratePhysicalDevices"));
    context.enumerate_device_extension_properties =
        reinterpret_cast<PFN_vkEnumerateDeviceExtensionProperties>(
            next_get_instance_proc_addr(
                *instance,
                "vkEnumerateDeviceExtensionProperties"));
    context.get_physical_device_properties =
        reinterpret_cast<PFN_vkGetPhysicalDeviceProperties>(
            next_get_instance_proc_addr(
                *instance,
                "vkGetPhysicalDeviceProperties"));
    context.get_physical_device_queue_family_properties =
        reinterpret_cast<PFN_vkGetPhysicalDeviceQueueFamilyProperties>(
            next_get_instance_proc_addr(
                *instance,
                "vkGetPhysicalDeviceQueueFamilyProperties"));

    if (create_info->pApplicationInfo) {
        if (create_info->pApplicationInfo->pApplicationName) {
            context.application_name = create_info->pApplicationInfo->pApplicationName;
        }
        if (create_info->pApplicationInfo->pEngineName) {
            context.engine_name = create_info->pApplicationInfo->pEngineName;
        }
    }

    {
        std::lock_guard lock(g_mutex);
        g_instances[*instance] = std::move(context);
    }
    send_status_event(
        "create-instance",
        VK_NULL_HANDLE,
        VK_NULL_HANDLE,
        1,
        {});
    return VK_SUCCESS;
}

VKAPI_ATTR void VKAPI_CALL layer_destroy_instance(
    VkInstance instance,
    const VkAllocationCallbacks* allocator) {
    PFN_vkDestroyInstance next_destroy_instance = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_instances.find(instance);
        if (it != g_instances.end()) {
            next_destroy_instance = it->second.destroy_instance;
        }
    }

    if (next_destroy_instance) {
        next_destroy_instance(instance, allocator);
    }

    std::lock_guard lock(g_mutex);
    g_instances.erase(instance);
    for (auto it = g_physical_devices.begin(); it != g_physical_devices.end();) {
        if (it->second == instance) {
            it = g_physical_devices.erase(it);
        } else {
            ++it;
        }
    }
}

VKAPI_ATTR VkResult VKAPI_CALL layer_enumerate_physical_devices(
    VkInstance instance,
    uint32_t* physical_device_count,
    VkPhysicalDevice* physical_devices) {
    PFN_vkEnumeratePhysicalDevices next_enumerate = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_instances.find(instance);
        if (it != g_instances.end()) {
            next_enumerate = it->second.enumerate_physical_devices;
        }
    }

    if (!next_enumerate) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    VkResult result =
        next_enumerate(instance, physical_device_count, physical_devices);
    if ((result == VK_SUCCESS || result == VK_INCOMPLETE) && physical_devices
        && physical_device_count) {
        std::lock_guard lock(g_mutex);
        for (uint32_t i = 0; i < *physical_device_count; ++i) {
            g_physical_devices[physical_devices[i]] = instance;
        }
    }
    return result;
}

VKAPI_ATTR VkResult VKAPI_CALL layer_create_device(
    VkPhysicalDevice physical_device,
    const VkDeviceCreateInfo* create_info,
    const VkAllocationCallbacks* allocator,
    VkDevice* device) {
    if (!create_info || !device) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    VkInstance instance = VK_NULL_HANDLE;
    InstanceContext instance_context{};
    {
        std::lock_guard lock(g_mutex);
        auto physical_it = g_physical_devices.find(physical_device);
        if (physical_it != g_physical_devices.end()) {
            instance = physical_it->second;
        }
        auto instance_it = g_instances.find(instance);
        if (instance_it != g_instances.end()) {
            instance_context = instance_it->second;
        }
    }

    if (!instance_context.create_device) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    auto* link = find_layer_chain_entry(create_info);
    if (!link || !link->u.pLayerInfo || !link->u.pLayerInfo->pfnNextGetDeviceProcAddr) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    PFN_vkGetDeviceProcAddr next_get_device_proc_addr =
        link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;

    // The overlay allocates its own command buffers, which are dispatchable
    // objects. The loader requires their loader data to be set before a layer
    // above us sees them; the callback arrives in its own chain entry.
    auto* loader_data = find_layer_chain_entry(
        create_info,
        VK_LOADER_DATA_CALLBACK);
    PFN_vkSetDeviceLoaderData set_device_loader_data =
        loader_data ? loader_data->u.pfnSetDeviceLoaderData : nullptr;

    const bool advertised = extension_advertised(
        instance_context,
        physical_device,
        VK_NV_LOW_LATENCY_2_EXTENSION_NAME);
    const bool requested = extension_requested(
        create_info,
        VK_NV_LOW_LATENCY_2_EXTENSION_NAME);
    const bool present_wait_advertised = extension_advertised(
        instance_context,
        physical_device,
        VK_KHR_PRESENT_WAIT_EXTENSION_NAME);
    const bool present_wait_requested = extension_requested(
        create_info,
        VK_KHR_PRESENT_WAIT_EXTENSION_NAME);
    const bool present_id_advertised = extension_advertised(
        instance_context,
        physical_device,
        VK_KHR_PRESENT_ID_EXTENSION_NAME);
    const bool present_id_requested = extension_requested(
        create_info,
        VK_KHR_PRESENT_ID_EXTENSION_NAME);

    VkResult result =
        instance_context.create_device(
            physical_device,
            create_info,
            allocator,
            device);
    if (result != VK_SUCCESS) {
        return result;
    }

    DeviceContext context{};
    context.device = *device;
    context.physical_device = physical_device;
    context.get_device_proc_addr = next_get_device_proc_addr;
    context.set_device_loader_data = set_device_loader_data;
    context.destroy_device = reinterpret_cast<PFN_vkDestroyDevice>(
        next_get_device_proc_addr(*device, "vkDestroyDevice"));
    context.device_wait_idle = reinterpret_cast<PFN_vkDeviceWaitIdle>(
        next_get_device_proc_addr(*device, "vkDeviceWaitIdle"));
    context.get_device_queue = reinterpret_cast<PFN_vkGetDeviceQueue>(
        next_get_device_proc_addr(*device, "vkGetDeviceQueue"));
    context.get_device_queue2 = reinterpret_cast<PFN_vkGetDeviceQueue2>(
        next_get_device_proc_addr(*device, "vkGetDeviceQueue2"));
    context.create_swapchain_khr = reinterpret_cast<PFN_vkCreateSwapchainKHR>(
        next_get_device_proc_addr(*device, "vkCreateSwapchainKHR"));
    context.destroy_swapchain_khr = reinterpret_cast<PFN_vkDestroySwapchainKHR>(
        next_get_device_proc_addr(*device, "vkDestroySwapchainKHR"));
    context.get_swapchain_images_khr =
        reinterpret_cast<PFN_vkGetSwapchainImagesKHR>(
            next_get_device_proc_addr(*device, "vkGetSwapchainImagesKHR"));
    context.queue_present_khr = reinterpret_cast<PFN_vkQueuePresentKHR>(
        next_get_device_proc_addr(*device, "vkQueuePresentKHR"));
    context.queue_submit = reinterpret_cast<PFN_vkQueueSubmit>(
        next_get_device_proc_addr(*device, "vkQueueSubmit"));
    context.create_image_view = reinterpret_cast<PFN_vkCreateImageView>(
        next_get_device_proc_addr(*device, "vkCreateImageView"));
    context.destroy_image_view = reinterpret_cast<PFN_vkDestroyImageView>(
        next_get_device_proc_addr(*device, "vkDestroyImageView"));
    context.create_render_pass = reinterpret_cast<PFN_vkCreateRenderPass>(
        next_get_device_proc_addr(*device, "vkCreateRenderPass"));
    context.destroy_render_pass = reinterpret_cast<PFN_vkDestroyRenderPass>(
        next_get_device_proc_addr(*device, "vkDestroyRenderPass"));
    context.create_framebuffer = reinterpret_cast<PFN_vkCreateFramebuffer>(
        next_get_device_proc_addr(*device, "vkCreateFramebuffer"));
    context.destroy_framebuffer = reinterpret_cast<PFN_vkDestroyFramebuffer>(
        next_get_device_proc_addr(*device, "vkDestroyFramebuffer"));
    context.create_command_pool = reinterpret_cast<PFN_vkCreateCommandPool>(
        next_get_device_proc_addr(*device, "vkCreateCommandPool"));
    context.destroy_command_pool = reinterpret_cast<PFN_vkDestroyCommandPool>(
        next_get_device_proc_addr(*device, "vkDestroyCommandPool"));
    context.allocate_command_buffers =
        reinterpret_cast<PFN_vkAllocateCommandBuffers>(
            next_get_device_proc_addr(*device, "vkAllocateCommandBuffers"));
    context.reset_command_buffer = reinterpret_cast<PFN_vkResetCommandBuffer>(
        next_get_device_proc_addr(*device, "vkResetCommandBuffer"));
    context.begin_command_buffer = reinterpret_cast<PFN_vkBeginCommandBuffer>(
        next_get_device_proc_addr(*device, "vkBeginCommandBuffer"));
    context.end_command_buffer = reinterpret_cast<PFN_vkEndCommandBuffer>(
        next_get_device_proc_addr(*device, "vkEndCommandBuffer"));
    context.cmd_begin_render_pass = reinterpret_cast<PFN_vkCmdBeginRenderPass>(
        next_get_device_proc_addr(*device, "vkCmdBeginRenderPass"));
    context.cmd_end_render_pass = reinterpret_cast<PFN_vkCmdEndRenderPass>(
        next_get_device_proc_addr(*device, "vkCmdEndRenderPass"));
    context.cmd_clear_attachments = reinterpret_cast<PFN_vkCmdClearAttachments>(
        next_get_device_proc_addr(*device, "vkCmdClearAttachments"));
    context.create_semaphore = reinterpret_cast<PFN_vkCreateSemaphore>(
        next_get_device_proc_addr(*device, "vkCreateSemaphore"));
    context.destroy_semaphore = reinterpret_cast<PFN_vkDestroySemaphore>(
        next_get_device_proc_addr(*device, "vkDestroySemaphore"));
    context.create_fence = reinterpret_cast<PFN_vkCreateFence>(
        next_get_device_proc_addr(*device, "vkCreateFence"));
    context.destroy_fence = reinterpret_cast<PFN_vkDestroyFence>(
        next_get_device_proc_addr(*device, "vkDestroyFence"));
    context.get_fence_status = reinterpret_cast<PFN_vkGetFenceStatus>(
        next_get_device_proc_addr(*device, "vkGetFenceStatus"));
    context.reset_fences = reinterpret_cast<PFN_vkResetFences>(
        next_get_device_proc_addr(*device, "vkResetFences"));
    context.wait_for_fences = reinterpret_cast<PFN_vkWaitForFences>(
        next_get_device_proc_addr(*device, "vkWaitForFences"));
    context.set_latency_sleep_mode_nv =
        reinterpret_cast<PFN_vkSetLatencySleepModeNV>(
            next_get_device_proc_addr(*device, "vkSetLatencySleepModeNV"));
    context.latency_sleep_nv = reinterpret_cast<PFN_vkLatencySleepNV>(
        next_get_device_proc_addr(*device, "vkLatencySleepNV"));
    context.set_latency_marker_nv = reinterpret_cast<PFN_vkSetLatencyMarkerNV>(
        next_get_device_proc_addr(*device, "vkSetLatencyMarkerNV"));
    context.get_latency_timings_nv = reinterpret_cast<PFN_vkGetLatencyTimingsNV>(
        next_get_device_proc_addr(*device, "vkGetLatencyTimingsNV"));
    context.queue_notify_out_of_band_nv =
        reinterpret_cast<PFN_vkQueueNotifyOutOfBandNV>(
            next_get_device_proc_addr(*device, "vkQueueNotifyOutOfBandNV"));
    // Only resolve the present-wait entry point when the app enabled the
    // extension; calling it otherwise would be undefined.
    if (present_wait_requested) {
        context.wait_for_present_khr = reinterpret_cast<PFN_vkWaitForPresentKHR>(
            next_get_device_proc_addr(*device, "vkWaitForPresentKHR"));
    }
    if (instance_context.get_physical_device_properties) {
        instance_context.get_physical_device_properties(
            physical_device,
            &context.physical_device_properties);
    }
    if (instance_context.get_physical_device_queue_family_properties) {
        uint32_t queue_family_count = 0;
        instance_context.get_physical_device_queue_family_properties(
            physical_device,
            &queue_family_count,
            nullptr);
        if (queue_family_count) {
            context.queue_family_properties.resize(queue_family_count);
            instance_context.get_physical_device_queue_family_properties(
                physical_device,
                &queue_family_count,
                context.queue_family_properties.data());
            context.queue_family_properties.resize(queue_family_count);
        }
    }
    context.low_latency_extension_advertised = advertised;
    context.low_latency_extension_requested = requested;
    context.low_latency_functions_available =
        context.set_latency_marker_nv && context.get_latency_timings_nv;
    context.present_wait_advertised = present_wait_advertised;
    context.present_wait_requested = present_wait_requested;
    context.present_id_advertised = present_id_advertised;
    context.present_id_requested = present_id_requested;

    DeviceTelemetryFlags flags{
        advertised,
        requested,
        context.low_latency_functions_available,
        0,
    };
    {
        std::lock_guard lock(g_mutex);
        g_devices[*device] = std::move(context);
    }
    send_status_event(
        "create-device",
        *device,
        VK_NULL_HANDLE,
        1,
        flags);
    return VK_SUCCESS;
}

VKAPI_ATTR void VKAPI_CALL layer_destroy_device(
    VkDevice device,
    const VkAllocationCallbacks* allocator) {
    PFN_vkDestroyDevice next_destroy_device = nullptr;
    DeviceContext device_context{};
    bool has_device_context = false;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_destroy_device = it->second.destroy_device;
            device_context = it->second;
            has_device_context = true;
        }
    }

    // Stop the present-wait thread before tearing the device down so it cannot
    // call into a device that is about to be destroyed.
    display_latency_stop_device(device);

    if (has_device_context && device_context.device_wait_idle) {
        device_context.device_wait_idle(device);
    }

    if (has_device_context) {
        std::lock_guard lock(g_mutex);
        for (auto& entry : g_swapchains) {
            if (entry.second.device == device) {
                destroy_overlay_resources(device_context, entry.second);
            }
        }
    }

    if (next_destroy_device) {
        next_destroy_device(device, allocator);
    }

    std::lock_guard lock(g_mutex);
    g_devices.erase(device);
    for (auto it = g_queues.begin(); it != g_queues.end();) {
        if (it->second.device == device) {
            it = g_queues.erase(it);
        } else {
            ++it;
        }
    }
    for (auto it = g_swapchains.begin(); it != g_swapchains.end();) {
        if (it->second.device == device) {
            it = g_swapchains.erase(it);
        } else {
            ++it;
        }
    }
}

VKAPI_ATTR void VKAPI_CALL layer_get_device_queue(
    VkDevice device,
    uint32_t queue_family_index,
    uint32_t queue_index,
    VkQueue* queue) {
    PFN_vkGetDeviceQueue next_get_device_queue = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_get_device_queue = it->second.get_device_queue;
        }
    }

    if (next_get_device_queue) {
        next_get_device_queue(device, queue_family_index, queue_index, queue);
        if (queue) {
            store_queue(device, *queue, queue_family_index, queue_index);
        }
    }
}

VKAPI_ATTR void VKAPI_CALL layer_get_device_queue2(
    VkDevice device,
    const VkDeviceQueueInfo2* queue_info,
    VkQueue* queue) {
    PFN_vkGetDeviceQueue2 next_get_device_queue2 = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_get_device_queue2 = it->second.get_device_queue2;
        }
    }

    if (next_get_device_queue2) {
        next_get_device_queue2(device, queue_info, queue);
        if (queue && queue_info) {
            store_queue(
                device,
                *queue,
                queue_info->queueFamilyIndex,
                queue_info->queueIndex);
        }
    }
}

VKAPI_ATTR VkResult VKAPI_CALL layer_create_swapchain_khr(
    VkDevice device,
    const VkSwapchainCreateInfoKHR* create_info,
    const VkAllocationCallbacks* allocator,
    VkSwapchainKHR* swapchain) {
    PFN_vkCreateSwapchainKHR next_create_swapchain = nullptr;
    VkPhysicalDevice physical_device = VK_NULL_HANDLE;
    VkInstance instance = VK_NULL_HANDLE;
    PFN_vkGetInstanceProcAddr get_instance_proc_addr = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_create_swapchain = it->second.create_swapchain_khr;
            physical_device = it->second.physical_device;
            auto physical_it = g_physical_devices.find(physical_device);
            if (physical_it != g_physical_devices.end()) {
                instance = physical_it->second;
                auto instance_it = g_instances.find(instance);
                if (instance_it != g_instances.end()) {
                    get_instance_proc_addr = instance_it->second.get_instance_proc_addr;
                }
            }
        }
    }

    if (!next_create_swapchain) {
        return VK_ERROR_EXTENSION_NOT_PRESENT;
    }

    // DXVK can present storage-only images (notably D3D9). Our HUD needs
    // color attachments, including when enabled later through the live toggle.
    // Request that usage only when the surface supports it; keep the caller's
    // structure and extension chain intact. Shared-present modes have different
    // capability rules and image layouts, so leave those untouched.
    VkSwapchainCreateInfoKHR overlay_info{};
    if (create_info && get_instance_proc_addr
        && !(create_info->imageUsage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT)
        && create_info->presentMode != VK_PRESENT_MODE_SHARED_DEMAND_REFRESH_KHR
        && create_info->presentMode != VK_PRESENT_MODE_SHARED_CONTINUOUS_REFRESH_KHR) {
        auto get_capabilities = reinterpret_cast<PFN_vkGetPhysicalDeviceSurfaceCapabilitiesKHR>(
            get_instance_proc_addr(instance, "vkGetPhysicalDeviceSurfaceCapabilitiesKHR"));
        VkSurfaceCapabilitiesKHR capabilities{};
        if (get_capabilities
            && get_capabilities(physical_device, create_info->surface, &capabilities) == VK_SUCCESS
            && (capabilities.supportedUsageFlags & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT)) {
            overlay_info = *create_info;
            overlay_info.imageUsage |= VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
            create_info = &overlay_info;
        }
    }
    VkResult result =
        next_create_swapchain(device, create_info, allocator, swapchain);
    const bool latency_mode_enabled = swapchain_latency_mode_enabled(create_info);
    uint64_t live_swapchain_count = 0;
    if (result == VK_SUCCESS && swapchain) {
        std::lock_guard lock(g_mutex);
        SwapchainContext context{};
        context.device = device;
        context.swapchain_latency_mode = latency_mode_enabled;
        if (create_info) {
            context.image_format = create_info->imageFormat;
            context.image_extent = create_info->imageExtent;
            context.image_array_layers = create_info->imageArrayLayers;
            context.image_usage = create_info->imageUsage;
        }
        g_swapchains[*swapchain] = context;
        live_swapchain_count = live_swapchain_count_locked(device);
    }
    if (result == VK_SUCCESS && swapchain) {
        send_swapchain_event(
            "create-swapchain",
            device,
            *swapchain,
            create_info,
            live_swapchain_count,
            latency_mode_enabled,
            device_telemetry_flags(device));
        reapply_latency_sleep_mode(
            "latency-sleep-mode-reapplied-create",
            device,
            *swapchain,
            0,
            0);
    }
    return result;
}

VKAPI_ATTR void VKAPI_CALL layer_destroy_swapchain_khr(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkAllocationCallbacks* allocator) {
    PFN_vkDestroySwapchainKHR next_destroy_swapchain = nullptr;
    DeviceContext device_context{};
    bool has_device_context = false;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_destroy_swapchain = it->second.destroy_swapchain_khr;
            device_context = it->second;
            has_device_context = true;
        }
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end() && has_device_context) {
            destroy_overlay_resources(device_context, swapchain_it->second);
        }
    }

    // Quiesce the present-wait thread before the driver destroys the swapchain,
    // so no wait is ever in flight across the destroy.
    display_latency_stop_device(device);

    if (next_destroy_swapchain) {
        next_destroy_swapchain(device, swapchain, allocator);
    }

    bool latency_mode_enabled = false;
    uint64_t live_swapchain_count = 0;
    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            latency_mode_enabled = swapchain_it->second.swapchain_latency_mode;
            g_swapchains.erase(swapchain_it);
        }
        live_swapchain_count = live_swapchain_count_locked(device);
    }
    send_swapchain_event(
        "destroy-swapchain",
        device,
        swapchain,
        nullptr,
        live_swapchain_count,
        latency_mode_enabled,
        device_telemetry_flags(device));
}

VKAPI_ATTR VkResult VKAPI_CALL layer_queue_present_khr(
    VkQueue queue,
    const VkPresentInfoKHR* present_info) {
    VkDevice device = VK_NULL_HANDLE;
    PFN_vkQueuePresentKHR next_queue_present = nullptr;
    DeviceContext device_context{};
    QueueContext queue_context{};
    bool has_device_context = false;
    bool has_queue_context = false;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            queue_context = queue_it->second;
            has_queue_context = true;
            device = queue_it->second.device;
            auto device_it = g_devices.find(device);
            if (device_it != g_devices.end()) {
                next_queue_present = device_it->second.queue_present_khr;
                device_context = device_it->second;
                has_device_context = true;
            }
        }
    }

    if (!next_queue_present) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    struct PresentObservation {
        VkSwapchainKHR swapchain = VK_NULL_HANDLE;
        uint64_t present_count = 0;
        uint64_t present_frametime_us = 0;
        uint64_t overlay_fps = 0;
        uint64_t vulkan_present_id = 0;
        uint64_t injected_present_id = 0;
    };
    std::vector<PresentObservation> observations;
    VkSemaphore overlay_signal = VK_NULL_HANDLE;
    const uint64_t present_time_us = now_monotonic_us();
    // Whether to stamp our own present IDs this present. The app must enable
    // present_id + present_wait, both opt-in env flags must be set, and we must
    // be able to place our struct without duplicating one: either the app chained
    // none, or it chained one at the head (vkd3d's zero-ID struct) which we
    // substitute. A present-id struct buried deeper in the chain we leave alone.
    const bool chain_has_present_id =
        present_info_has_present_id_struct(present_info);
    const auto* present_chain_head = present_info
        ? reinterpret_cast<const VkBaseInStructure*>(present_info->pNext)
        : nullptr;
    const bool present_id_at_head = present_chain_head
        && present_chain_head->sType == VK_STRUCTURE_TYPE_PRESENT_ID_KHR;
    const bool want_inject = inject_present_id_enabled()
        && display_latency_enabled()
        && has_device_context
        && device_context.present_id_requested
        && device_context.present_wait_requested
        && device_context.wait_for_present_khr
        && (!chain_has_present_id || present_id_at_head);
    if (present_info && present_info->pSwapchains) {
        observations.reserve(present_info->swapchainCount);
        std::lock_guard lock(g_mutex);
        for (uint32_t i = 0; i < present_info->swapchainCount; ++i) {
            VkSwapchainKHR swapchain = present_info->pSwapchains[i];
            const uint64_t vulkan_present_id =
                present_id_for_swapchain(present_info, i);
            PresentObservation observation{};
            observation.swapchain = swapchain;
            observation.vulkan_present_id = vulkan_present_id;
            auto swapchain_it = g_swapchains.find(swapchain);
            if (swapchain_it != g_swapchains.end()) {
                auto& swapchain_context = swapchain_it->second;
                observation.present_count = ++swapchain_context.present_count;
                if (vulkan_present_id) {
                    swapchain_context.last_vulkan_present_id =
                        vulkan_present_id;
                } else if (want_inject) {
                    // Strictly increasing per swapchain, as the spec requires.
                    observation.injected_present_id =
                        ++swapchain_context.last_injected_present_id;
                }
                if (swapchain_context.last_present_us
                    && present_time_us > swapchain_context.last_present_us) {
                    observation.present_frametime_us =
                        present_time_us - swapchain_context.last_present_us;
                }
                swapchain_context.last_present_us = present_time_us;
                if (overlay_enabled() && observation.present_frametime_us) {
                    observation.overlay_fps =
                        median_fps_from_present_frametime_locked(
                            swapchain_context,
                            observation.present_frametime_us);
                }
                if (overlay_enabled() && has_device_context && has_queue_context
                    && present_info->swapchainCount == 1
                    && present_info->pImageIndices) {
                    const std::string overlay_text =
                        cached_overlay_text(
                            observation.overlay_fps,
                            present_time_us);
                    if (!overlay_text.empty()) {
                        overlay_signal = submit_overlay_locked(
                            device_context,
                            swapchain,
                            swapchain_context,
                            queue_context,
                            queue,
                            present_info->pImageIndices[i],
                            present_info,
                            overlay_text);
                    }
                }
            }
            observations.push_back(observation);
        }
    }

    // Present-id array must outlive the present call (the driver reads it).
    std::vector<uint64_t> injected_present_ids;
    bool any_injected = false;
    if (want_inject && present_info) {
        injected_present_ids.assign(present_info->swapchainCount, 0);
        for (size_t i = 0;
             i < observations.size() && i < injected_present_ids.size();
             ++i) {
            injected_present_ids[i] = observations[i].injected_present_id;
            if (injected_present_ids[i]) {
                any_injected = true;
            }
        }
    }

    VkPresentInfoKHR modified_present_info{};
    VkPresentIdKHR present_id_info{};
    const VkPresentInfoKHR* next_present_info = present_info;
    if (present_info && (overlay_signal != VK_NULL_HANDLE || any_injected)) {
        modified_present_info = *present_info;
        if (overlay_signal != VK_NULL_HANDLE) {
            modified_present_info.waitSemaphoreCount = 1;
            modified_present_info.pWaitSemaphores = &overlay_signal;
        }
        if (any_injected) {
            present_id_info.sType = VK_STRUCTURE_TYPE_PRESENT_ID_KHR;
            // Replace the app's head present-id struct (skip past it), else
            // prepend, preserving the rest of the pNext chain behind ours.
            present_id_info.pNext = present_id_at_head
                ? present_chain_head->pNext
                : present_info->pNext;
            present_id_info.swapchainCount = present_info->swapchainCount;
            present_id_info.pPresentIds = injected_present_ids.data();
            modified_present_info.pNext = &present_id_info;
        }
        next_present_info = &modified_present_info;
    }

    VkResult result = next_queue_present(queue, next_present_info);
    for (const PresentObservation& observation : observations) {
        VkSwapchainKHR swapchain = observation.swapchain;
        const uint64_t present_count = observation.present_count;
        if (should_report_counter(present_count)) {
            send_status_event(
                "present",
                device,
                swapchain,
                present_count,
                device_telemetry_flags(device));
        }
        if (debug_flow_enabled() && should_report_counter(present_count)) {
            send_flow_state_event(
                "present-flow",
                device,
                swapchain,
                queue,
                present_count,
                result,
                -1,
                0,
                flow_snapshot(device, swapchain));
        }
        // Present-pacing FPS works without Reflex; emit it for every present.
        if (observation.present_frametime_us) {
            send_present_pacing_sample(
                device,
                swapchain,
                observation.present_frametime_us,
                present_count);
        }
        // Opt-in present->scanout tail. Waits on the app's own present ID when it
        // supplied one, else on the ID we injected this present (inject path).
        const uint64_t effective_present_id =
            observation.injected_present_id
                ? observation.injected_present_id
                : observation.vulkan_present_id;
        if (display_latency_enabled() && has_device_context
            && device_context.present_wait_requested
            && device_context.present_id_requested
            && device_context.wait_for_present_khr
            && effective_present_id) {
            display_latency_enqueue(
                device,
                device_context.wait_for_present_khr,
                swapchain,
                effective_present_id,
                present_time_us);
        }
        if (inject_present_id_enabled()
            && should_report_counter(present_count)) {
            send_inject_debug_event(
                device,
                swapchain,
                present_count,
                chain_has_present_id,
                present_id_at_head,
                want_inject,
                observation.injected_present_id);
        }
        if (timing_queries_enabled()) {
            query_latency_timing(device, swapchain);
        } else if (should_report_counter(present_count)) {
            send_status_event(
                "latency-timing-query-disabled",
                device,
                swapchain,
                present_count,
                device_telemetry_flags(device));
        }
    }
    return result;
}

VKAPI_ATTR VkResult VKAPI_CALL layer_set_latency_sleep_mode_nv(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkLatencySleepModeInfoNV* sleep_mode_info) {
    PFN_vkSetLatencySleepModeNV next_set_latency_sleep_mode = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_set_latency_sleep_mode = it->second.set_latency_sleep_mode_nv;
        }
    }

    VkLatencySleepModeInfoNV saved_info{};
    bool has_sleep_mode_info = false;
    if (sleep_mode_info) {
        saved_info = *sleep_mode_info;
        saved_info.pNext = nullptr;
        has_sleep_mode_info = true;
    }

    VkResult result = VK_ERROR_EXTENSION_NOT_PRESENT;
    if (next_set_latency_sleep_mode) {
        result = next_set_latency_sleep_mode(device, swapchain, sleep_mode_info);
    }

    uint64_t count = 0;
    DeviceTelemetryFlags flags{};
    if (result == VK_SUCCESS) {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            auto& context = it->second;
            context.has_latency_sleep_mode_info = has_sleep_mode_info;
            context.latency_sleep_mode_info = saved_info;
            count = ++context.latency_sleep_mode_set_count;
            flags = {
                context.low_latency_extension_advertised,
                context.low_latency_extension_requested,
                context.low_latency_functions_available,
                context.marker_count,
            };
        }
    } else {
        flags = device_telemetry_flags(device);
    }

    send_latency_sleep_mode_event(
        "latency-sleep-mode-set",
        device,
        swapchain,
        count,
        result,
        has_sleep_mode_info,
        saved_info,
        0,
        0,
        flags);
    return result;
}

VKAPI_ATTR VkResult VKAPI_CALL layer_latency_sleep_nv(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkLatencySleepInfoNV* sleep_info) {
    PFN_vkLatencySleepNV next_latency_sleep = nullptr;
    uint64_t count = 0;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_latency_sleep = it->second.latency_sleep_nv;
            count = ++it->second.latency_sleep_count;
        }
    }

    VkResult result = VK_ERROR_EXTENSION_NOT_PRESENT;
    if (next_latency_sleep) {
        result = next_latency_sleep(device, swapchain, sleep_info);
    }

    if (debug_flow_enabled() || should_report_counter(count)) {
        send_flow_state_event(
            "latency-sleep",
            device,
            swapchain,
            VK_NULL_HANDLE,
            count,
            result,
            -1,
            sleep_info ? sleep_info->value : 0,
            flow_snapshot(device, swapchain));
    }

    return result;
}

VKAPI_ATTR void VKAPI_CALL layer_set_latency_marker_nv(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkSetLatencyMarkerInfoNV* marker_info) {
    PFN_vkSetLatencyMarkerNV next_set_latency_marker = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_set_latency_marker = it->second.set_latency_marker_nv;
        }
    }

    if (next_set_latency_marker) {
        next_set_latency_marker(device, swapchain, marker_info);
    }
    record_marker(device, swapchain, marker_info);
    observe_latency_marker(device, swapchain, marker_info);
}

VKAPI_ATTR void VKAPI_CALL layer_queue_notify_out_of_band_nv(
    VkQueue queue,
    const VkOutOfBandQueueTypeInfoNV* queue_type_info) {
    VkDevice device = VK_NULL_HANDLE;
    PFN_vkQueueNotifyOutOfBandNV next_queue_notify = nullptr;
    uint64_t count = 0;
    int queue_type = queue_type_info
        ? static_cast<int>(queue_type_info->queueType)
        : -1;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            auto& queue_context = queue_it->second;
            device = queue_context.device;
            count = ++queue_context.out_of_band_notify_count;
            auto device_it = g_devices.find(device);
            if (device_it != g_devices.end()) {
                next_queue_notify = device_it->second.queue_notify_out_of_band_nv;
            }
        }
    }

    if (next_queue_notify) {
        next_queue_notify(queue, queue_type_info);
    }

    if (debug_flow_enabled() || should_report_counter(count)) {
        send_flow_state_event(
            "latency-queue-out-of-band",
            device,
            VK_NULL_HANDLE,
            queue,
            count,
            next_queue_notify ? VK_SUCCESS : VK_ERROR_EXTENSION_NOT_PRESENT,
            queue_type,
            0,
            flow_snapshot(device, VK_NULL_HANDLE));
    }
}

VKAPI_ATTR void VKAPI_CALL layer_get_latency_timings_nv(
    VkDevice device,
    VkSwapchainKHR swapchain,
    VkGetLatencyMarkerInfoNV* latency_marker_info) {
    PFN_vkGetLatencyTimingsNV next_get_latency_timings = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_get_latency_timings = it->second.get_latency_timings_nv;
        }
    }

    if (next_get_latency_timings) {
        next_get_latency_timings(device, swapchain, latency_marker_info);
    } else if (latency_marker_info) {
        latency_marker_info->timingCount = 0;
    }
}

}  // namespace pblayer

using namespace pblayer;

extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(
    VkInstance instance,
    const char* name);

extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(
    VkDevice device,
    const char* name);

extern "C" __attribute__((visibility("default"))) VKAPI_ATTR VkResult VKAPI_CALL
vkNegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface* version) {
    send_status_event_once("negotiate", g_reported_negotiate);

    if (!version || version->sType != LAYER_NEGOTIATE_INTERFACE_STRUCT) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    if (version->loaderLayerInterfaceVersion < 2) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    version->loaderLayerInterfaceVersion =
        std::min<uint32_t>(version->loaderLayerInterfaceVersion, 2);
    version->pfnGetInstanceProcAddr = vkGetInstanceProcAddr;
    version->pfnGetDeviceProcAddr = vkGetDeviceProcAddr;
    version->pfnGetPhysicalDeviceProcAddr = nullptr;
    return VK_SUCCESS;
}

extern "C" __attribute__((visibility("default"))) VKAPI_ATTR PFN_vkVoidFunction
VKAPI_CALL vkGetInstanceProcAddr(VkInstance instance, const char* name) {
    send_status_event_once(
        "get-instance-proc-addr",
        g_reported_get_instance_proc_addr);

    if (!name) {
        return nullptr;
    }

    if (std::strcmp(name, "vkGetInstanceProcAddr") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(vkGetInstanceProcAddr);
    }
    if (std::strcmp(name, "vkGetDeviceProcAddr") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(vkGetDeviceProcAddr);
    }
    if (std::strcmp(name, "vkCreateInstance") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_create_instance);
    }
    if (std::strcmp(name, "vkDestroyInstance") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_destroy_instance);
    }
    if (std::strcmp(name, "vkEnumeratePhysicalDevices") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_enumerate_physical_devices);
    }
    if (std::strcmp(name, "vkCreateDevice") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_create_device);
    }
    if (std::strcmp(name, "vkDestroyDevice") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_destroy_device);
    }
    if (std::strcmp(name, "vkGetDeviceQueue") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_get_device_queue);
    }
    if (std::strcmp(name, "vkGetDeviceQueue2") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_get_device_queue2);
    }
    if (std::strcmp(name, "vkCreateSwapchainKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_create_swapchain_khr);
    }
    if (std::strcmp(name, "vkDestroySwapchainKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_destroy_swapchain_khr);
    }
    if (std::strcmp(name, "vkQueuePresentKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_queue_present_khr);
    }
    if (std::strcmp(name, "vkSetLatencySleepModeNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_set_latency_sleep_mode_nv);
    }
    if (std::strcmp(name, "vkLatencySleepNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_latency_sleep_nv);
    }
    if (std::strcmp(name, "vkSetLatencyMarkerNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_set_latency_marker_nv);
    }
    if (std::strcmp(name, "vkGetLatencyTimingsNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_get_latency_timings_nv);
    }
    if (std::strcmp(name, "vkQueueNotifyOutOfBandNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_queue_notify_out_of_band_nv);
    }

    std::lock_guard lock(g_mutex);
    auto it = g_instances.find(instance);
    if (it != g_instances.end() && it->second.get_instance_proc_addr) {
        return it->second.get_instance_proc_addr(instance, name);
    }
    return nullptr;
}

extern "C" __attribute__((visibility("default"))) VKAPI_ATTR PFN_vkVoidFunction
VKAPI_CALL vkGetDeviceProcAddr(VkDevice device, const char* name) {
    send_status_event_once(
        "get-device-proc-addr",
        g_reported_get_device_proc_addr);

    if (!name) {
        return nullptr;
    }

    if (std::strcmp(name, "vkGetDeviceProcAddr") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(vkGetDeviceProcAddr);
    }
    if (std::strcmp(name, "vkDestroyDevice") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_destroy_device);
    }
    if (std::strcmp(name, "vkGetDeviceQueue") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_get_device_queue);
    }
    if (std::strcmp(name, "vkGetDeviceQueue2") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_get_device_queue2);
    }
    if (std::strcmp(name, "vkCreateSwapchainKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_create_swapchain_khr);
    }
    if (std::strcmp(name, "vkDestroySwapchainKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_destroy_swapchain_khr);
    }
    if (std::strcmp(name, "vkQueuePresentKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_queue_present_khr);
    }
    if (std::strcmp(name, "vkSetLatencySleepModeNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_set_latency_sleep_mode_nv);
    }
    if (std::strcmp(name, "vkLatencySleepNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_latency_sleep_nv);
    }
    if (std::strcmp(name, "vkSetLatencyMarkerNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_set_latency_marker_nv);
    }
    if (std::strcmp(name, "vkGetLatencyTimingsNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_get_latency_timings_nv);
    }
    if (std::strcmp(name, "vkQueueNotifyOutOfBandNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_queue_notify_out_of_band_nv);
    }

    std::lock_guard lock(g_mutex);
    auto it = g_devices.find(device);
    if (it != g_devices.end() && it->second.get_device_proc_addr) {
        return it->second.get_device_proc_addr(device, name);
    }
    return nullptr;
}
