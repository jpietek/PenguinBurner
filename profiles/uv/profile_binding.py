"""Bind an unbound (legacy) saved profile to the GPU it is applied on."""

from __future__ import annotations

from drivers.nvidia.daemon_gpu import DaemonGpuClient
from profiles.gpu_identity import normalized_gpu_identity
from profiles.uv.profile_store import bind_auto_uv_profile_gpu_identity


def bind_legacy_profile_to_gpu(profile: dict, gpu_index: int) -> dict:
    """Write the GPU's identity into the profile's saved file, as an explicit
    Apply on an unambiguous one-GPU machine does, and return it. The summary
    dict is updated in place so the caller's copy matches the file."""
    index = int(gpu_index)
    identity = normalized_gpu_identity(
        DaemonGpuClient(index).capabilities().identity,
        index_at_verification=index,
    )
    bind_auto_uv_profile_gpu_identity(
        str(profile.get("path") or profile.get("profile_id") or ""),
        identity,
    )
    profile["gpu_identity"] = identity
    return identity
