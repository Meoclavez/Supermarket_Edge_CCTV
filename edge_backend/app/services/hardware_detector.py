import os
import shutil
import subprocess
from ..models.schemas import HardwareProfile

def get_ram_info():
    try:
        import psutil
        mem = psutil.virtual_memory()
        return round(mem.total / (1024 ** 3), 2), round(mem.available / (1024 ** 3), 2)
    except Exception:
        total_gb = 16.0
        avail_gb = 12.0
        try:
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
                for line in lines:
                    if line.startswith("MemTotal:"):
                        total_gb = round(int(line.split()[1]) / (1024 ** 2), 2)
                    elif line.startswith("MemAvailable:"):
                        avail_gb = round(int(line.split()[1]) / (1024 ** 2), 2)
        except Exception:
            pass
        return total_gb, avail_gb

class HardwareDetector:
    @staticmethod
    def detect_hardware() -> HardwareProfile:
        total_ram_gb, available_ram_gb = get_ram_info()
        cpu_cores = os.cpu_count() or 4
        
        decoder_type = "cpu"
        inference_backend = "onnx_cpu"
        device_name = "Generic CPU"
        
        # 1. Probe NVIDIA GPU
        if shutil.which("nvidia-smi") or os.path.exists("/dev/nvidia0"):
            try:
                res = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], 
                                     capture_output=True, text=True, timeout=2)
                if res.returncode == 0 and res.stdout.strip():
                    device_name = res.stdout.strip().split("\n")[0]
                    decoder_type = "cuda"
                    inference_backend = "tensorrt"
            except Exception:
                pass
                
        # 2. Probe Intel QuickSync / VA-API or AMD Mesa if no NVIDIA
        if decoder_type == "cpu" and os.path.exists("/dev/dri"):
            render_nodes = [f for f in os.listdir("/dev/dri") if f.startswith("renderD")]
            if render_nodes:
                is_intel = False
                is_amd = False
                if shutil.which("vainfo"):
                    try:
                        res = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=2)
                        output = res.stdout + res.stderr
                        if "iHD" in output or "Intel" in output:
                            is_intel = True
                        elif "radeonsi" in output or "AMD" in output:
                            is_amd = True
                    except Exception:
                        pass
                
                if not (is_intel or is_amd):
                    try:
                        with open("/proc/cpuinfo", "r") as f:
                            cpuinfo = f.read()
                            if "GenuineIntel" in cpuinfo:
                                is_intel = True
                            elif "AuthenticAMD" in cpuinfo:
                                is_amd = True
                    except Exception:
                        is_intel = True
                
                if is_intel:
                    decoder_type = "vaapi_intel"
                    inference_backend = "openvino_gpu"
                    device_name = "Intel QuickSync iGPU (VA-API iHD)"
                elif is_amd:
                    decoder_type = "vaapi_amd"
                    inference_backend = "openvino_cpu"
                    device_name = "AMD Radeon APU/GPU (VA-API Mesa)"

        # 3. Dynamic RAM Sizing
        if total_ram_gb < 6.0:
            ring_buffer_seconds = 3
            max_cams = 6
        elif total_ram_gb <= 16.0:
            ring_buffer_seconds = 5
            max_cams = 12
        else:
            ring_buffer_seconds = 10
            max_cams = 20

        return HardwareProfile(
            decoder_type=decoder_type,
            inference_backend=inference_backend,
            device_name=device_name,
            total_ram_gb=total_ram_gb,
            available_ram_gb=available_ram_gb,
            ring_buffer_seconds=ring_buffer_seconds,
            cpu_cores=cpu_cores,
            max_recommended_cameras=max_cams
        )

hardware_profile = HardwareDetector.detect_hardware()
