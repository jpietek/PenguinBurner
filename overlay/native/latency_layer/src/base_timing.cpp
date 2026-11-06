// Bounded shared-memory transport from the original NVAPI marker bridge: the
// single source of base (pre-FG) frames. The bridge writes the file directly;
// the layer only reads it. Opening/mapping happens on the text worker, which
// also re-validates the file's identity each build so a restarted bridge is
// picked up. Drawing only reads memory and retries a concurrent write at most
// twice; it never waits for the producer.
#include "latency_layer_internal.h"
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>

namespace pblayer {
constexpr uint32_t kBaseTimingMagic = 0x50424231; // PBB1, fixed ABI across 32/64 bit.
struct alignas(8) BaseTimingBuffer {
    uint32_t sequence, magic, pid, index, count, reserved;
    uint64_t frame_id, last_us;
    uint64_t samples[512], times_us[512];
};
static_assert(offsetof(BaseTimingBuffer, frame_id) == 24);
static_assert(sizeof(BaseTimingBuffer) == 8232);

// Reference writer for the ABI above; the shipped bridge writes the same
// layout itself, so these stay internal and only feed the layer's own tests.
size_t pb_base_timing_size() {
    return sizeof(BaseTimingBuffer);
}
void pb_base_timing_publish(void* memory, uint64_t frame_id, uint64_t time_us, uint32_t pid) {
    if (!memory || !frame_id || !time_us || !pid) return;
    auto& state = *static_cast<BaseTimingBuffer*>(memory);
    const bool reset = state.magic != kBaseTimingMagic || (time_us > state.last_us && time_us - state.last_us > 2000000);
    if (!reset && (state.pid != pid || frame_id <= state.frame_id || time_us <= state.last_us)) return;
    const uint32_t odd = (__atomic_load_n(&state.sequence, __ATOMIC_RELAXED) + 1) | 1;
    __atomic_store_n(&state.sequence, odd, __ATOMIC_SEQ_CST);
    // The payload stores below must not become visible before the odd marker.
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    if (reset) {
        state.magic = kBaseTimingMagic; state.pid = pid;
        state.count = state.index = 0; state.last_us = 0;
    }
    if (state.last_us) {
        state.samples[state.index] = time_us - state.last_us;
        state.times_us[state.index] = time_us;
        state.index = (state.index + 1) % 512;
        state.count = std::min<uint32_t>(state.count + 1, 512);
    }
    state.frame_id = frame_id; state.last_us = time_us;
    __atomic_store_n(&state.sequence, odd + 1, __ATOMIC_RELEASE);
}

class BaseFrameReader {
public:
    BaseFrameReader(void* memory, std::string path, dev_t dev, ino_t ino)
        : memory_(static_cast<const BaseTimingBuffer*>(memory)), path_(std::move(path)), dev_(dev), ino_(ino) {}
    ~BaseFrameReader() { munmap(const_cast<BaseTimingBuffer*>(memory_), sizeof(BaseTimingBuffer)); }
    const std::string& path() const { return path_; }
    // The mapping follows the inode, not the path: a bridge that replaced or
    // unlinked the file leaves this reader attached to a dead buffer.
    bool same_file(const struct stat& status) const {
        return status.st_dev == dev_ && status.st_ino == ino_;
    }
    // Replaces the graph's top plot with base frames when the buffer holds at
    // least one sample inside the window; otherwise leaves whatever fallback
    // the caller placed there. base_available marks the source as existing
    // even when its samples are stale, so a gap reads "waiting".
    bool read(OverlayGraph& graph) {
        BaseTimingBuffer snapshot{};
        bool stable = false;
        for (unsigned attempt = 0; attempt < 2; ++attempt) {
            const auto before = __atomic_load_n(&memory_->sequence, __ATOMIC_ACQUIRE);
            if (before & 1) continue;
            std::memcpy(&snapshot, memory_, sizeof(snapshot));
            __atomic_thread_fence(__ATOMIC_ACQUIRE);
            if (before == __atomic_load_n(&memory_->sequence, __ATOMIC_ACQUIRE)) { stable = true; break; }
        }
        if (stable && snapshot.magic == kBaseTimingMagic && snapshot.count <= 512 && snapshot.index < 512) {
            cached_ = snapshot;
            cached_valid_ = true;
        } else if (cached_valid_) {
            snapshot = cached_;
        } else return false;
        graph.base_available = true;
        // Only a stale stream gives up the plot. The newest marker may postdate
        // end_time_us by the CPU's run-ahead plus the bridge's publish delay;
        // in_window() drops those samples individually.
        if (!snapshot.last_us
            || (graph.end_time_us > snapshot.last_us && graph.end_time_us - snapshot.last_us >= graph.window_us))
            return false;
        const auto in_window = [&](uint64_t t) {
            return t && t <= graph.end_time_us && graph.end_time_us - t < graph.window_us;
        };
        const auto first = snapshot.count == 512 ? snapshot.index : 0;
        uint32_t fresh = 0;
        for (uint32_t i = 0; i < snapshot.count; ++i)
            if (in_window(snapshot.times_us[(first + i) % 512])) ++fresh;
        if (!fresh) return false;
        graph.count = 0;
        graph.source = OverlayGraphSource::BaseFrames;
        for (uint32_t i = 0; i < snapshot.count; ++i) {
            const auto slot = (first + i) % 512;
            if (!in_window(snapshot.times_us[slot])) continue;
            graph.sample_times_us[graph.count] = snapshot.times_us[slot];
            graph.samples[graph.count++] = snapshot.samples[slot];
        }
        auto sorted = graph.samples;
        std::sort(sorted.begin(), sorted.begin() + graph.count);
        graph.median_us = graph.count ? sorted[graph.count / 2] : 0;
        return true;
    }
private:
    const BaseTimingBuffer* memory_;
    std::string path_;
    dev_t dev_;
    ino_t ino_;
    BaseTimingBuffer cached_{};
    bool cached_valid_ = false;
};

std::shared_ptr<BaseFrameReader> open_base_frame_reader() {
    const std::string session = telemetry_session_id();
    if (session.empty()) return {};
    const auto socket = overlay_runtime_path(kSocketEnv, "latency.sock");
    const auto slash = socket.find_last_of('/');
    if (slash == std::string::npos) return {};
    const auto path = socket.substr(0, slash + 1) + "base-timing." + session + ".bin";
    const int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return {};
    struct stat status{};
    void* memory = MAP_FAILED;
    if (fstat(fd, &status) == 0 && S_ISREG(status.st_mode) && status.st_uid == getuid()
        && status.st_size == static_cast<off_t>(sizeof(BaseTimingBuffer)))
        memory = mmap(nullptr, sizeof(BaseTimingBuffer), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (memory == MAP_FAILED) return nullptr;
    return std::make_shared<BaseFrameReader>(memory, path, status.st_dev, status.st_ino);
}

bool base_frame_reader_stale(const std::shared_ptr<BaseFrameReader>& reader) {
    if (!reader) return false;
    struct stat status{};
    return stat(reader->path().c_str(), &status) != 0 || !reader->same_file(status);
}

bool read_base_frame_reader(const std::shared_ptr<BaseFrameReader>& reader, OverlayGraph& graph) {
    return reader && reader->read(graph);
}
} // namespace pblayer
