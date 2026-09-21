import pytest

from app.services.hardware_detector import HardwareDetector, get_ram_info
from app.services.inference_backend import person_detector


def test_hardware_detection_profile():
    profile = HardwareDetector.detect_hardware()
    assert profile.total_ram_gb is None or profile.total_ram_gb > 0
    assert profile.decoder_type in ["cuda", "vaapi_intel", "vaapi_amd", "cpu"]
    # decoder_type is a capability probe, and says so under its honest name too.
    assert profile.decoder_capability == profile.decoder_type
    assert profile.ring_buffer_seconds in [3, 5, 10]
    assert profile.cpu_cores is None or profile.cpu_cores >= 1


def test_inference_backend_is_what_the_detector_runs():
    """The profile must not guess 'tensorrt' from nvidia-smi; it reports the live detector."""
    status = person_detector.status()
    profile = HardwareDetector.detect_hardware()
    assert profile.inference_backend == status["backend"]
    assert profile.inference_provider == status["provider"]
    assert profile.inference_available == bool(status["available"])
    assert profile.inference_backend not in ("tensorrt", "openvino_gpu", "openvino_cpu")


def test_ram_info_has_no_fallback_constants():
    total, avail = get_ram_info()
    # Either measured, or honestly unknown -- never the old 16.0 / 12.0 defaults.
    if total is not None:
        assert total > 0
    assert not (total == 16.0 and avail == 12.0), "looks like the old hardcoded fallback"
