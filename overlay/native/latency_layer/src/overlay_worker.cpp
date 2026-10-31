// Builds the HUD readout and its text geometry off the present thread.
#include "latency_layer_internal.h"

namespace pblayer {

namespace {
constexpr auto kOverlayRefresh = std::chrono::milliseconds(250);
}

OverlayWorker::OverlayWorker(bool background) {
    if (background) {
        thread_ = std::thread([this] { run(); });
    }
}

OverlayWorker::~OverlayWorker() {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stop_ = true;
    }
    cv_.notify_all();
    if (thread_.joinable()) {
        thread_.join();
    }
}

void OverlayWorker::set_facts(const OverlayDeviceFacts& facts) {
    std::lock_guard<std::mutex> lock(mutex_);
    facts_ = facts;
}

void OverlayWorker::post(
    const OverlayFrameStats& stats,
    const OverlayGraph& graph,
    VkExtent2D extent,
    bool blended,
    OverlayFont font) {
    std::lock_guard<std::mutex> lock(mutex_);
    stats_ = stats;
    graph_ = graph;
    extent_ = extent;
    blended_ = blended;
    font_ = font;
    if (!valid_) {
        valid_ = true;
        cv_.notify_all();
    }
}

std::shared_ptr<const OverlayPrepared> OverlayWorker::latest() {
    std::lock_guard<std::mutex> lock(mutex_);
    return latest_;
}

std::shared_ptr<const OverlayPrepared> OverlayWorker::build_now() {
    OverlayFrameStats stats;
    OverlayGraph graph;
    OverlayDeviceFacts facts;
    VkExtent2D extent;
    bool blended;
    OverlayFont font;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stats = stats_;
        graph = graph_;
        facts = facts_;
        extent = extent_;
        blended = blended_;
        font = font_;
    }
    auto prepared = std::make_shared<OverlayPrepared>();
    const uint64_t now_us = now_monotonic_us();
    prepared->readout = std::make_shared<const OverlayReadout>(
        build_overlay_readout(stats, facts, read_overlay_gpu_state(), now_us));
    overlay_build_static_geometry(
        prepared->geometry, prepared->readout, graph, extent, blended, font);
    std::lock_guard<std::mutex> lock(mutex_);
    latest_ = prepared;
    return prepared;
}

void OverlayWorker::run() {
    pthread_setname_np(pthread_self(), "pb-overlay-text");
    std::unique_lock<std::mutex> lock(mutex_);
    while (!stop_) {
        if (!valid_) {
            cv_.wait(lock, [this] { return stop_ || valid_; });
            continue;
        }
        lock.unlock();
        build_now();
        lock.lock();
        cv_.wait_for(lock, kOverlayRefresh, [this] { return stop_; });
    }
}

}  // namespace pblayer
