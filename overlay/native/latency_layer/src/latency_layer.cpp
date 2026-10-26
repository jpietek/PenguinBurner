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
    for (uint32_t i = 0; i < create_info->enabledExtensionCount; ++i)
        if (std::strcmp(create_info->ppEnabledExtensionNames[i], VK_KHR_GET_SURFACE_CAPABILITIES_2_EXTENSION_NAME) == 0)
            context.surface_capabilities2_enabled = true;
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
    context.get_physical_device_memory_properties =
        reinterpret_cast<PFN_vkGetPhysicalDeviceMemoryProperties>(
            next_get_instance_proc_addr(
                *instance, "vkGetPhysicalDeviceMemoryProperties"));
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

#ifdef VK_EXT_present_timing
    DisplayTimingDeviceSetup display_setup;
    prepare_display_timing_device(instance, instance_context, physical_device, create_info, display_setup);
    if (display_setup.enabled) create_info = &display_setup.info;
#endif
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
#ifdef VK_EXT_present_timing
    context.owns_display_timing = display_setup.enabled;
    if (extension_requested(create_info, VK_EXT_PRESENT_TIMING_EXTENSION_NAME)) {
        context.set_timing_queue = reinterpret_cast<PFN_vkSetSwapchainPresentTimingQueueSizeEXT>(next_get_device_proc_addr(*device, "vkSetSwapchainPresentTimingQueueSizeEXT"));
        context.get_timing_domains = reinterpret_cast<PFN_vkGetSwapchainTimeDomainPropertiesEXT>(next_get_device_proc_addr(*device, "vkGetSwapchainTimeDomainPropertiesEXT"));
        context.get_past_timing = reinterpret_cast<PFN_vkGetPastPresentationTimingEXT>(next_get_device_proc_addr(*device, "vkGetPastPresentationTimingEXT"));
    }
    display_debug_log("create-device %p timing_req=%d id2_req=%d wait2_req=%d owns=%d set_queue=%p domains=%p past=%p",
        reinterpret_cast<void*>(*device),
        extension_requested(create_info, VK_EXT_PRESENT_TIMING_EXTENSION_NAME) ? 1 : 0,
        extension_requested(create_info, VK_KHR_PRESENT_ID_2_EXTENSION_NAME) ? 1 : 0,
        extension_requested(create_info, VK_KHR_PRESENT_WAIT_2_EXTENSION_NAME) ? 1 : 0,
        context.owns_display_timing ? 1 : 0,
        reinterpret_cast<void*>(context.set_timing_queue), reinterpret_cast<void*>(context.get_timing_domains),
        reinterpret_cast<void*>(context.get_past_timing));
#endif
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
    // Use the application's enabled clock-calibration extension. This adds no
    // device features or graphics initialization to the game's startup path.
    const char* calibration_suffix = extension_requested(create_info, "VK_KHR_calibrated_timestamps")
        ? "KHR" : extension_requested(create_info, VK_EXT_CALIBRATED_TIMESTAMPS_EXTENSION_NAME)
        ? "EXT" : nullptr;
    if (calibration_suffix && instance_context.get_instance_proc_addr) {
        const std::string domains_name = std::string("vkGetPhysicalDeviceCalibrateableTimeDomains") + calibration_suffix;
        auto domains_fn = reinterpret_cast<PFN_vkGetPhysicalDeviceCalibrateableTimeDomainsEXT>(
            instance_context.get_instance_proc_addr(instance, domains_name.c_str()));
        uint32_t count = 0;
        if (domains_fn && domains_fn(physical_device, &count, nullptr) == VK_SUCCESS && count) {
            std::vector<VkTimeDomainEXT> domains(count);
            if (domains_fn(physical_device, &count, domains.data()) == VK_SUCCESS
                && std::find(domains.begin(), domains.end(), VK_TIME_DOMAIN_CLOCK_MONOTONIC_EXT) != domains.end()
                && std::find(domains.begin(), domains.end(), VK_TIME_DOMAIN_DEVICE_EXT) != domains.end()) {
                const std::string name = std::string("vkGetCalibratedTimestamps") + calibration_suffix;
                context.get_calibrated_timestamps = reinterpret_cast<PFN_vkGetCalibratedTimestampsEXT>(
                    next_get_device_proc_addr(*device, name.c_str()));
            }
        }
    }
    context.create_query_pool = reinterpret_cast<PFN_vkCreateQueryPool>(next_get_device_proc_addr(*device, "vkCreateQueryPool"));
    context.destroy_query_pool = reinterpret_cast<PFN_vkDestroyQueryPool>(next_get_device_proc_addr(*device, "vkDestroyQueryPool"));
    context.get_query_pool_results = reinterpret_cast<PFN_vkGetQueryPoolResults>(next_get_device_proc_addr(*device, "vkGetQueryPoolResults"));
    context.cmd_reset_query_pool = reinterpret_cast<PFN_vkCmdResetQueryPool>(next_get_device_proc_addr(*device, "vkCmdResetQueryPool"));
    context.cmd_write_timestamp = reinterpret_cast<PFN_vkCmdWriteTimestamp>(next_get_device_proc_addr(*device, "vkCmdWriteTimestamp"));
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
    context.create_shader_module = reinterpret_cast<PFN_vkCreateShaderModule>(
        next_get_device_proc_addr(*device, "vkCreateShaderModule"));
    context.destroy_shader_module = reinterpret_cast<PFN_vkDestroyShaderModule>(
        next_get_device_proc_addr(*device, "vkDestroyShaderModule"));
    context.create_pipeline_layout = reinterpret_cast<PFN_vkCreatePipelineLayout>(
        next_get_device_proc_addr(*device, "vkCreatePipelineLayout"));
    context.destroy_pipeline_layout = reinterpret_cast<PFN_vkDestroyPipelineLayout>(
        next_get_device_proc_addr(*device, "vkDestroyPipelineLayout"));
    context.create_graphics_pipelines = reinterpret_cast<PFN_vkCreateGraphicsPipelines>(
        next_get_device_proc_addr(*device, "vkCreateGraphicsPipelines"));
    context.create_pipeline_cache = reinterpret_cast<PFN_vkCreatePipelineCache>(
        next_get_device_proc_addr(*device, "vkCreatePipelineCache"));
    context.destroy_pipeline_cache = reinterpret_cast<PFN_vkDestroyPipelineCache>(
        next_get_device_proc_addr(*device, "vkDestroyPipelineCache"));
    context.overlay_pipeline_cache = std::make_shared<OverlayPipelineCache>();
    context.destroy_pipeline = reinterpret_cast<PFN_vkDestroyPipeline>(
        next_get_device_proc_addr(*device, "vkDestroyPipeline"));
    context.create_buffer = reinterpret_cast<PFN_vkCreateBuffer>(
        next_get_device_proc_addr(*device, "vkCreateBuffer"));
    context.destroy_buffer = reinterpret_cast<PFN_vkDestroyBuffer>(
        next_get_device_proc_addr(*device, "vkDestroyBuffer"));
    context.get_buffer_memory_requirements = reinterpret_cast<PFN_vkGetBufferMemoryRequirements>(
        next_get_device_proc_addr(*device, "vkGetBufferMemoryRequirements"));
    context.allocate_memory = reinterpret_cast<PFN_vkAllocateMemory>(
        next_get_device_proc_addr(*device, "vkAllocateMemory"));
    context.free_memory = reinterpret_cast<PFN_vkFreeMemory>(
        next_get_device_proc_addr(*device, "vkFreeMemory"));
    context.bind_buffer_memory = reinterpret_cast<PFN_vkBindBufferMemory>(
        next_get_device_proc_addr(*device, "vkBindBufferMemory"));
    context.map_memory = reinterpret_cast<PFN_vkMapMemory>(
        next_get_device_proc_addr(*device, "vkMapMemory"));
    context.unmap_memory = reinterpret_cast<PFN_vkUnmapMemory>(
        next_get_device_proc_addr(*device, "vkUnmapMemory"));
    context.cmd_bind_pipeline = reinterpret_cast<PFN_vkCmdBindPipeline>(
        next_get_device_proc_addr(*device, "vkCmdBindPipeline"));
    context.cmd_bind_vertex_buffers = reinterpret_cast<PFN_vkCmdBindVertexBuffers>(
        next_get_device_proc_addr(*device, "vkCmdBindVertexBuffers"));
    context.cmd_set_viewport = reinterpret_cast<PFN_vkCmdSetViewport>(
        next_get_device_proc_addr(*device, "vkCmdSetViewport"));
    context.cmd_set_scissor = reinterpret_cast<PFN_vkCmdSetScissor>(
        next_get_device_proc_addr(*device, "vkCmdSetScissor"));
    context.cmd_draw = reinterpret_cast<PFN_vkCmdDraw>(
        next_get_device_proc_addr(*device, "vkCmdDraw"));
    context.create_image = reinterpret_cast<PFN_vkCreateImage>(
        next_get_device_proc_addr(*device, "vkCreateImage"));
    context.destroy_image = reinterpret_cast<PFN_vkDestroyImage>(
        next_get_device_proc_addr(*device, "vkDestroyImage"));
    context.get_image_memory_requirements = reinterpret_cast<PFN_vkGetImageMemoryRequirements>(
        next_get_device_proc_addr(*device, "vkGetImageMemoryRequirements"));
    context.bind_image_memory = reinterpret_cast<PFN_vkBindImageMemory>(
        next_get_device_proc_addr(*device, "vkBindImageMemory"));
    context.create_sampler = reinterpret_cast<PFN_vkCreateSampler>(
        next_get_device_proc_addr(*device, "vkCreateSampler"));
    context.destroy_sampler = reinterpret_cast<PFN_vkDestroySampler>(
        next_get_device_proc_addr(*device, "vkDestroySampler"));
    context.create_descriptor_set_layout = reinterpret_cast<PFN_vkCreateDescriptorSetLayout>(
        next_get_device_proc_addr(*device, "vkCreateDescriptorSetLayout"));
    context.destroy_descriptor_set_layout = reinterpret_cast<PFN_vkDestroyDescriptorSetLayout>(
        next_get_device_proc_addr(*device, "vkDestroyDescriptorSetLayout"));
    context.create_descriptor_pool = reinterpret_cast<PFN_vkCreateDescriptorPool>(
        next_get_device_proc_addr(*device, "vkCreateDescriptorPool"));
    context.destroy_descriptor_pool = reinterpret_cast<PFN_vkDestroyDescriptorPool>(
        next_get_device_proc_addr(*device, "vkDestroyDescriptorPool"));
    context.allocate_descriptor_sets = reinterpret_cast<PFN_vkAllocateDescriptorSets>(
        next_get_device_proc_addr(*device, "vkAllocateDescriptorSets"));
    context.update_descriptor_sets = reinterpret_cast<PFN_vkUpdateDescriptorSets>(
        next_get_device_proc_addr(*device, "vkUpdateDescriptorSets"));
    context.cmd_bind_descriptor_sets = reinterpret_cast<PFN_vkCmdBindDescriptorSets>(
        next_get_device_proc_addr(*device, "vkCmdBindDescriptorSets"));
    context.cmd_copy_buffer_to_image = reinterpret_cast<PFN_vkCmdCopyBufferToImage>(
        next_get_device_proc_addr(*device, "vkCmdCopyBufferToImage"));
    context.cmd_pipeline_barrier = reinterpret_cast<PFN_vkCmdPipelineBarrier>(
        next_get_device_proc_addr(*device, "vkCmdPipelineBarrier"));
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
    if (instance_context.get_physical_device_memory_properties) {
        instance_context.get_physical_device_memory_properties(
            physical_device,
            &context.physical_device_memory_properties);
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

    // All overlay workers have joined before their shared device cache dies.
    if (device_context.overlay_pipeline_cache && device_context.destroy_pipeline_cache
        && device_context.overlay_pipeline_cache->handle != VK_NULL_HANDLE) {
        device_context.destroy_pipeline_cache(
            device, device_context.overlay_pipeline_cache->handle, nullptr);
        device_context.overlay_pipeline_cache->handle = VK_NULL_HANDLE;
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
    bool owns_display_timing = false;
    bool has_timing_procs = false;
    VkPhysicalDevice physical_device = VK_NULL_HANDLE;
    VkInstance instance = VK_NULL_HANDLE;
    PFN_vkGetInstanceProcAddr get_instance_proc_addr = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_create_swapchain = it->second.create_swapchain_khr;
            owns_display_timing = it->second.owns_display_timing;
#ifdef VK_EXT_present_timing
            has_timing_procs = it->second.get_past_timing && it->second.get_timing_domains;
#endif
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
    uint32_t display_stage = 0;
    bool shared_timing = false;
#ifdef VK_EXT_present_timing
    if (!owns_display_timing && has_timing_procs && create_info
        && (create_info->flags & VK_SWAPCHAIN_CREATE_PRESENT_TIMING_BIT_EXT)
        && (create_info->flags & VK_SWAPCHAIN_CREATE_PRESENT_ID_2_BIT_KHR)) {
        display_stage = display_timing_surface_stage(instance, get_instance_proc_addr, physical_device, create_info->surface);
        shared_timing = display_stage != 0;
    }
    // Existing timing consumers retain ownership of their own queue and IDs.
    if (owns_display_timing && create_info
        && !(create_info->flags & VK_SWAPCHAIN_CREATE_PRESENT_TIMING_BIT_EXT)) {
        display_stage = display_timing_surface_stage(instance, get_instance_proc_addr, physical_device, create_info->surface);
        if (display_stage) {
            overlay_info = *create_info;
            overlay_info.flags |= VK_SWAPCHAIN_CREATE_PRESENT_TIMING_BIT_EXT | VK_SWAPCHAIN_CREATE_PRESENT_ID_2_BIT_KHR;
            create_info = &overlay_info;
        }
    }
#else
    (void)owns_display_timing;
    (void)has_timing_procs;
#endif
    if (create_info) display_debug_log("create-swapchain flags=0x%x mode=%d images=%u owns=%d shared=%d stage=0x%x",
        create_info->flags, static_cast<int>(create_info->presentMode), create_info->minImageCount,
        owns_display_timing ? 1 : 0, shared_timing ? 1 : 0, display_stage);
    VkResult result =
        next_create_swapchain(device, create_info, allocator, swapchain);
    display_debug_log("create-swapchain result=%d handle=%p", static_cast<int>(result), swapchain ? reinterpret_cast<void*>(*swapchain) : nullptr);
    const bool latency_mode_enabled = swapchain_latency_mode_enabled(create_info);
    uint64_t live_swapchain_count = 0;
    if (result == VK_SUCCESS && swapchain) {
        std::lock_guard lock(g_mutex);
        SwapchainContext context{};
        context.device = device;
        context.display_timing.stage = display_stage;
        context.display_timing.shared = shared_timing;
        context.swapchain_latency_mode = latency_mode_enabled;
        if (create_info) {
            context.image_format = create_info->imageFormat;
            context.image_extent = create_info->imageExtent;
            context.image_array_layers = create_info->imageArrayLayers;
            context.image_usage = create_info->imageUsage;
            context.present_mode = create_info->presentMode;
            context.device_facts.resolution =
                overlay_resolution_text(create_info->imageExtent);
            context.device_facts.present_mode =
                overlay_present_mode_text(create_info->presentMode);
        }
        // The driver already told us the card and its version at device
        // creation; no daemon round trip and no state file are involved.
        const auto device_entry = g_devices.find(device);
        if (device_entry != g_devices.end()) {
#ifdef VK_EXT_present_timing
            init_display_timing(device_entry->second, *swapchain, context.display_timing);
#endif
            const VkPhysicalDeviceProperties& properties =
                device_entry->second.physical_device_properties;
            context.device_facts.gpu_name = properties.deviceName;
            context.device_facts.driver_version = overlay_driver_version_text(
                properties.driverVersion, properties.vendorID);
        }
        // Text and geometry are built off the present thread from here on.
        context.overlay_worker = std::make_unique<OverlayWorker>();
        context.overlay_worker->set_facts(context.device_facts);
        g_swapchains[*swapchain] = std::move(context);
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

struct PresentHookStats {
    std::mutex mutex;
    uint64_t count = 0, pre_sum = 0, pre_max = 0, drv_sum = 0, drv_max = 0, post_sum = 0, post_max = 0;
};
PresentHookStats g_present_hook_stats;

VKAPI_ATTR VkResult VKAPI_CALL layer_queue_present_khr(
    VkQueue queue,
    const VkPresentInfoKHR* present_info) {
    // Hook cost accounting, only under the debug-flow trace.
    const bool trace = display_debug_enabled();
    const uint64_t hook_entry_us = trace ? now_monotonic_us() : 0;
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
#ifdef VK_EXT_present_timing
                collect_display_timing(device_context, swapchain, swapchain_context.display_timing);
#endif
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
                swapchain_context.frame_cycle_start_us = swapchain_context.last_present_us;
                swapchain_context.last_present_us = present_time_us;
                // vkd3d switches modes per present (VkSwapchainPresentModeInfoKHR)
                // without recreating the swapchain; keep the footer on the live mode.
                for (auto* entry = reinterpret_cast<const VkBaseInStructure*>(present_info->pNext);
                     entry; entry = entry->pNext) {
                    if (entry->sType != VK_STRUCTURE_TYPE_SWAPCHAIN_PRESENT_MODE_INFO_KHR) continue;
                    const auto* modes = reinterpret_cast<const VkSwapchainPresentModeInfoKHR*>(entry);
                    if (modes->pPresentModes && i < modes->swapchainCount
                        && modes->pPresentModes[i] != swapchain_context.present_mode) {
                        swapchain_context.present_mode = modes->pPresentModes[i];
                        swapchain_context.device_facts.present_mode =
                            overlay_present_mode_text(modes->pPresentModes[i]);
                        if (swapchain_context.overlay_worker) {
                            swapchain_context.overlay_worker->set_facts(swapchain_context.device_facts);
                        }
                    }
                    break;
                }
                OverlayFrameStats overlay_stats{};
                OverlayGraph overlay_graph{};
                if (overlay_enabled() && observation.present_frametime_us) {
                    overlay_stats = overlay_frame_stats_locked(
                        swapchain_context,
                        observation.present_frametime_us);
                    observation.overlay_fps = overlay_stats.median_fps;
                    if (overlay_graph_enabled()) {
                        overlay_graph = overlay_graph_locked(swapchain_context, overlay_stats.median_us);
                    }
                }
                if (overlay_enabled() && has_device_context && has_queue_context
                    && present_info->swapchainCount == 1
                    && present_info->pImageIndices) {
                    overlay_signal = submit_overlay_locked(
                        device_context,
                        swapchain,
                        swapchain_context,
                        queue_context,
                        queue,
                        present_info->pImageIndices[i],
                        present_info,
                        overlay_stats,
                        overlay_graph);
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

#ifdef VK_EXT_present_timing
    // Query one swapchain only. Never rewrite an application's timing/ID2
    // chain or consume its feedback. Backpressure stops queries before full.
    VkPresentInfoKHR timed_present{};
    VkPresentTimingInfoEXT timing_info{};
    timing_info.sType = VK_STRUCTURE_TYPE_PRESENT_TIMING_INFO_EXT;
    VkPresentTimingsInfoEXT timing_infos{};
    timing_infos.sType = VK_STRUCTURE_TYPE_PRESENT_TIMINGS_INFO_EXT;
    VkPresentId2KHR timing_id{};
    timing_id.sType = VK_STRUCTURE_TYPE_PRESENT_ID_2_KHR;
    uint64_t timing_id_value = 0;
    bool submitted_timing = false;
    bool foreign_timing = false;
    for (auto* entry = reinterpret_cast<const VkBaseInStructure*>(next_present_info->pNext);
         entry; entry = entry->pNext) {
        if (entry->sType == VK_STRUCTURE_TYPE_PRESENT_TIMINGS_INFO_EXT
            || entry->sType == VK_STRUCTURE_TYPE_PRESENT_ID_2_KHR) foreign_timing = true;
    }
    if (trace) {
        static std::atomic<uint64_t> presents{0};
        const uint64_t n = ++presents;
        if (n <= 8 || n % 600 == 0) {
            char chain[160]{};
            size_t used = 0;
            for (auto* entry = reinterpret_cast<const VkBaseInStructure*>(next_present_info->pNext);
                 entry && used < sizeof(chain) - 16; entry = entry->pNext)
                used += std::snprintf(chain + used, sizeof(chain) - used, "%d,", static_cast<int>(entry->sType));
            display_debug_log("present n=%" PRIu64 " foreign=%d swapchains=%u chain=[%s]", n, foreign_timing ? 1 : 0,
                next_present_info->swapchainCount, chain);
        }
    }
    if (!foreign_timing && next_present_info->swapchainCount == 1) {
        std::lock_guard lock(g_mutex);
        auto it = g_swapchains.find(next_present_info->pSwapchains[0]);
        if (it != g_swapchains.end()) {
            auto& state = it->second.display_timing;
            const uint32_t pending_cap = state.shared ? 8u : 32u;
            if (state.available && (state.shared || !state.passive) && state.pending < pending_cap) {
                // One ID per present: reuse a v1 ID already in the chain so v1
                // and v2 never disagree; mint our own only when there is none.
                const uint64_t chain_v1_id = observations.empty() ? 0 : observations[0].vulkan_present_id;
                const uint64_t injected_v1 = observations.empty() ? 0 : observations[0].injected_present_id;
                timing_id_value = chain_v1_id ? chain_v1_id : injected_v1 ? injected_v1 : ++state.next_id;
                timing_info.timeDomainId = state.domain_id;
                timing_info.presentStageQueries = state.stage;
                timing_info.targetTimeDomainPresentStage = state.stage;
                timing_infos.swapchainCount = 1;
                timing_infos.pTimingInfos = &timing_info;
                timing_infos.pNext = next_present_info->pNext;
                timing_id.swapchainCount = 1;
                timing_id.pPresentIds = &timing_id_value;
                timing_id.pNext = &timing_infos;
                timed_present = *next_present_info;
                timed_present.pNext = &timing_id;
                next_present_info = &timed_present;
                ++state.pending;
                submitted_timing = true;
            }
        }
    }
#endif
    const uint64_t driver_begin_us = trace ? now_monotonic_us() : 0;
    VkResult result = next_queue_present(queue, next_present_info);
    const uint64_t driver_end_us = trace ? now_monotonic_us() : 0;
#ifdef VK_EXT_present_timing
    if (submitted_timing && result != VK_SUCCESS && result != VK_SUBOPTIMAL_KHR) {
        std::lock_guard lock(g_mutex);
        auto it = g_swapchains.find(next_present_info->pSwapchains[0]);
        if (it != g_swapchains.end()) {
            auto& state = it->second.display_timing;
            state.available = false;
            state.count = state.index = 0;
        }
    }
#endif
    if (overlay_signal != VK_NULL_HANDLE && present_info && present_info->swapchainCount == 1) {
        std::lock_guard lock(g_mutex);
        auto it = g_swapchains.find(present_info->pSwapchains[0]);
        if (it != g_swapchains.end()) {
            auto& samples = it->second.overlay.frame_samples;
            const auto index = present_info->pImageIndices[0];
            const auto per_swapchain = present_info->pResults ? present_info->pResults[0] : result;
            if (index < samples.size()) {
                samples[index].presented = (result == VK_SUCCESS || result == VK_SUBOPTIMAL_KHR)
                    && (per_swapchain == VK_SUCCESS || per_swapchain == VK_SUBOPTIMAL_KHR);
            }
        }
    }
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
    if (trace) {
        const uint64_t done_us = now_monotonic_us();
        const uint64_t pre = driver_begin_us - hook_entry_us;
        const uint64_t drv = driver_end_us - driver_begin_us;
        const uint64_t post = done_us - driver_end_us;
        auto& st = g_present_hook_stats;
        std::lock_guard<std::mutex> lock(st.mutex);
        ++st.count; st.pre_sum += pre; st.drv_sum += drv; st.post_sum += post;
        st.pre_max = std::max(st.pre_max, pre); st.drv_max = std::max(st.drv_max, drv); st.post_max = std::max(st.post_max, post);
        if (st.count % 600 == 0) {
            display_debug_log("present-hook n=%" PRIu64 " layer-pre avg=%.1fus max=%" PRIu64 "us | driver avg=%.1fus max=%" PRIu64 "us | layer-post avg=%.1fus max=%" PRIu64 "us",
                st.count, st.pre_sum / 600.0, st.pre_max, st.drv_sum / 600.0, st.drv_max, st.post_sum / 600.0, st.post_max);
            st.pre_sum = st.drv_sum = st.post_sum = 0; st.pre_max = st.drv_max = st.post_max = 0;
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
#ifdef VK_EXT_present_timing
    if (std::strcmp(name, "vkGetPastPresentationTimingEXT") == 0) {
        display_debug_log("proc-addr query for vkGetPastPresentationTimingEXT -> hook");
        return reinterpret_cast<PFN_vkVoidFunction>(layer_get_past_presentation_timing_ext);
    }
#endif
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

    PFN_vkGetInstanceProcAddr next_get_instance_proc_addr = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_instances.find(instance);
        if (it != g_instances.end()) {
            next_get_instance_proc_addr = it->second.get_instance_proc_addr;
        }
    }
    // Never call down the chain with g_mutex held: the loader calls into this
    // layer under its own lock (vkEnumeratePhysicalDevices), so that deadlocks.
    return next_get_instance_proc_addr
        ? next_get_instance_proc_addr(instance, name)
        : nullptr;
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
#ifdef VK_EXT_present_timing
    if (std::strcmp(name, "vkGetPastPresentationTimingEXT") == 0) {
        display_debug_log("proc-addr query for vkGetPastPresentationTimingEXT -> hook");
        return reinterpret_cast<PFN_vkVoidFunction>(layer_get_past_presentation_timing_ext);
    }
#endif
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

    PFN_vkGetDeviceProcAddr next_get_device_proc_addr = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_get_device_proc_addr = it->second.get_device_proc_addr;
        }
    }
    // Same lock order as vkGetInstanceProcAddr: never hold g_mutex down-chain.
    return next_get_device_proc_addr
        ? next_get_device_proc_addr(device, name)
        : nullptr;
}
