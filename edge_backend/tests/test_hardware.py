import pytest
from app.services.hardware_detector import HardwareDetector

def test_hardware_detection_profile():
    profile = HardwareDetector.detect_hardware()
    assert profile.total_ram_gb > 0
    assert profile.decoder_type in ["cuda", "vaapi_intel", "vaapi_amd", "cpu"]
    assert profile.inference_backend in ["tensorrt", "openvino_gpu", "openvino_cpu", "onnx_cuda", "onnx_cpu"]
    assert profile.ring_buffer_seconds in [3, 5, 10]
    assert profile.cpu_cores >= 1
