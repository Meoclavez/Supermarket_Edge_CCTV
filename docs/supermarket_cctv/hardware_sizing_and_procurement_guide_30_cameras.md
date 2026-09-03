# Supermarket Edge AI CCTV System — Hardware Sizing & Procurement Guide (30+ Cameras)

> **Document Version:** 1.0.0  
> **Target Scale:** 30–36 Concurrent CCTV Camera Feeds  
> **Workload Profile:** 24/7 Edge Video Ingest, Multi-Model TensorRT AI Inference, 2D Store Tracking, and Automated Daily Analytics Synthesis  
> **Last Updated:** September 2026  

---

## 1. Executive Summary & Purpose

This specification defines the hardware requirements, component compatibilities, and itemized procurement guide for deploying the **Supermarket Edge AI CCTV Analytics & Decision Platform** across **30 or more cameras**.

The system operates as a hybrid edge appliance:
1. **Continuous Edge Processing:** Ingests 30+ Dahua/ONVIF RTSP sub-streams (720p/D1), decodes them in hardware, decimates frames to 2–5 FPS, and executes a multi-model vision pipeline (YOLOv11 Person/Cart Detection, YOLOv11-Pose Wrist Reach Detection, FastReID Shopper Tracking, and Ephemeral Face Demographic Vectorization).
2. **Local Event Persistence:** Logs all spatial coordinates, dwell durations, and shelf interactions into a local DuckDB time-series event database.
3. **Daily Decision Intelligence:** Synthesizes footfall trajectories, shelf engagement, and POS transactions into daily automated store layout, staffing, and merchandising recommendations (<24-hour turnaround).

---

## 2. Mathematical System Throughput & Workload Analysis

To ensure continuous operation without frame drops, thermal throttling, or Out-of-Memory (OOM) faults, the hardware sizing is computed from empirical throughput equations:

### A. Network & Ingestion Bandwidth (32 Cameras Baseline)
$$\begin{aligned}
\text{Bitrate per Camera (720p @ H.264/H.265)} &= 1.5\text{ Mbps to } 2.5\text{ Mbps} \\
\text{Total Network Ingress} &= 32 \times 2.0\text{ Mbps} = \mathbf{64\text{ Mbps (\approx 8.0\text{ MB/s})}}
\end{aligned}$$
* **Network Requirement:** A single 1 GbE or 2.5 GbE NIC easily absorbs this bandwidth (utilizing less than 7% of a 1 GbE interface, or <3% of a 2.5 GbE interface).

### B. Hardware Video Decoding (NVDEC / QuickSync)
* Continuous decoding of 32 concurrent 720p streams requires dedicated multi-stream hardware video decode engines.
* Software CPU decoding 32 streams would consume $\approx 80\text{–}100\%$ of a 16-core CPU.
* In contrast, an **NVIDIA Ada Lovelace GPU with Dual NVDEC** decodes 32 streams of 720p at $<15\%$ decoder load.

### C. Aggregate AI Inference Frame Rate
$$\text{Aggregate AI FPS} = 32\text{ Cameras} \times 3.0\text{ FPS (Decimated)} = \mathbf{96\text{ FPS}}$$
$$\text{Per-Batch Processing Budget} = \frac{1000\text{ ms}}{96\text{ FPS}} \approx \mathbf{10.4\text{ ms}}$$
* TensorRT 10.x running batched YOLOv11s on an RTX 4070 Ti Super / RTX 4080 executes in **$\approx 1.8\text{ ms}$ to $3.0\text{ ms}$**, leaving over **$70\%$ compute headroom** for secondary keypoint pose estimation and Re-ID feature extraction.

### D. VRAM Allocation Model
$$V_{\text{total}} = V_{\text{frame\_buffers}} + V_{\text{models}} + V_{\text{cuda\_context}} + V_{\text{headroom}}$$

$$\begin{aligned}
V_{\text{frame\_buffers}} &= 32 \times (1280 \times 720 \times 3 \text{ bytes} \times \text{FP16}) \times 4 \text{ batch} \approx \mathbf{1.8\text{ GB}} \\
V_{\text{models (TensorRT FP16)}} &= \underbrace{0.6\text{ GB}}_{\text{YOLOv11s}} + \underbrace{0.85\text{ GB}}_{\text{YOLO-Pose}} + \underbrace{0.9\text{ GB}}_{\text{FastReID}} + \underbrace{0.35\text{ GB}}_{\text{Demographics}} \approx \mathbf{2.7\text{ GB}} \\
V_{\text{cuda\_context \& shm}} &\approx \mathbf{2.5\text{ GB}} \\
V_{\text{local\_llm\_headroom}} &\approx \mathbf{4.5\text{ GB to } 6.0\text{ GB}} \quad (\text{Quantized Qwen 2.5 7B / Llama 3.2 3B}) \\
\hline
\mathbf{Total\ Minimum\ VRAM} &\approx \mathbf{11.5\text{ GB to } 13.0\text{ GB}} \implies \mathbf{16\text{ GB VRAM Minimum Required}}
\end{aligned}$$

---

## 3. Hardware Architecture Comparison Matrix (30+ Cameras)

| Parameter | **Option A: Custom Edge Workstation** <br> *(Top Recommendation - Best Value & Power)* | **Option B: Enterprise 1U/2U Rack Server** <br> *(Best for Datacenter / Server Rack)* | **Option C: NVIDIA Jetson AGX Orin (64GB)** <br> *(Best for Compact / Fanless Space)* |
| :--- | :--- | :--- | :--- |
| **Supported Cameras** | **30 – 48 Cameras** | **30 – 64+ Cameras** | **25 – 36 Cameras** |
| **GPU Model** | **NVIDIA RTX 4070 Ti Super (16GB)** or **RTX 4080 Super (16GB)** | **NVIDIA RTX 4000 Ada (20GB)** or **NVIDIA L4 (24GB)** | Integrated Ampere GPU (64GB Unified) |
| **GPU Architecture** | Ada Lovelace (AD103, 16GB GDDR6X) | Ada Lovelace Enterprise (Single-Slot, ECC) | Ampere (2048 CUDA Cores, 64 Tensor) |
| **Video Decoders** | **Dual 8th Gen NVDEC** (Decodes >100 720p streams) | **Dual NVDEC + AV1 Decode** (Enterprise 24/7) | Hardware VPU (Up to 30x 1080p30) |
| **AI Performance** | **44 TFLOPS (FP16) / 700+ TOPS** | **300+ TOPS (INT8) / 20GB ECC** | **275 TOPS (INT8)** |
| **Host CPU** | **Intel Core i7-14700** (20 Cores / 28 Threads) | Intel Xeon E-2488 (8C/16T) or AMD EPYC 4004 | 12-core ARM Cortex-A78AE |
| **System RAM** | **32 GB – 64 GB DDR5-5600MHz** | **64 GB DDR5 ECC** | 64 GB LPDDR5 (Unified Memory) |
| **Storage** | 2TB PCIe 4.0 NVMe SSD (7,000+ MB/s) | Dual 1.92TB Enterprise NVMe (RAID-1) | 1TB M.2 PCIe NVMe |
| **Power Consumption** | 65W Idle / 220W–280W Peak | 80W Idle / 180W–240W Peak | 15W Idle / 45W–60W Peak |
| **Form Factor** | Micro-ATX / Compact Mid-Tower | 19" 1U/2U Rackmount Chassis | Industrial Rugged Fanless Box |
| **Est. System Cost** | **$1,750 – $2,050 USD** | **$3,200 – $4,500 USD** | **$2,100 – $2,600 USD** |
| **Recommendation** | ⭐⭐⭐⭐⭐ **(Best ROI, Upgradable, Standard x86)** | ⭐⭐⭐⭐☆ **(Best for Enterprise Server Rooms)** | ⭐⭐⭐⭐☆ **(Best for Fanless Environments)** |

---

## 4. Itemized Bill of Materials (BOM) — Option A (Custom Workstation)

This build uses standard, high-reliability commercial components optimized for 24/7 edge surveillance workloads:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                      ITEMIZED HARDWARE BILL OF MATERIALS (30+ CAMERAS)                          │
├──────────────────────┬───────────────────────────────────────────────┬──────────────────────────┤
│ Component            │ Recommended Part Specification                │ Estimated Price (USD)    │
├──────────────────────┼───────────────────────────────────────────────┼──────────────────────────┤
│ 1. Dedicated GPU     │ NVIDIA GeForce RTX 4070 Ti Super (16GB VRAM)  │ $780 – $830              │
│                      │ Models: ASUS Dual, MSI Ventus 3X, Gigabyte OC │                          │
├──────────────────────┼───────────────────────────────────────────────┼──────────────────────────┤
│ 2. Processor (CPU)   │ Intel Core i7-14700 (20 Cores / 28 Threads)   │ $370 – $395              │
│                      │ (Integrated Intel UHD 770 QuickSync fallback) │                          │
├──────────────────────┼───────────────────────────────────────────────┼──────────────────────────┤
│ 3. Motherboard       │ ASUS TUF GAMING B760M-PLUS WIFI (Micro-ATX)   │ $160 – $180              │
│                      │ (PCIe 5.0 x16, Dual PCIe 4.0 M.2, 2.5GbE LAN) │                          │
├──────────────────────┼───────────────────────────────────────────────┼──────────────────────────┤
│ 4. System Memory     │ 32GB (2x16GB) or 64GB Corsair Vengeance DDR5  │ $115 – $190              │
│                      │ 5600MHz / 6000MHz CL30                        │                          │
├──────────────────────┼───────────────────────────────────────────────┼──────────────────────────┤
│ 5. Primary Storage   │ 2TB Samsung 990 Pro or WD Black SN850X NVMe   │ $160 – $180              │
│                      │ (PCIe 4.0, 7,450 MB/s Read, 1,200 TBW write)  │                          │
├──────────────────────┼───────────────────────────────────────────────┼──────────────────────────┤
│ 6. Power Supply (PSU)│ Corsair RM750e (750W, 80+ Gold, ATX 3.0 / PCIe5│ $99 – $115              │
├──────────────────────┼───────────────────────────────────────────────┼──────────────────────────┤
│ 7. Case & Enclosure  │ ASUS Prime AP201 (Mesh) or Fractal Pop Mini Air│ $80 – $95               │
│                      │ (Compact Micro-ATX, High airflow, dust filters)│                          │
├──────────────────────┼───────────────────────────────────────────────┼──────────────────────────┤
│ 8. CPU Cooling       │ Thermalright Peerless Assassin 120 SE Air      │ $35 – $40                │
├──────────────────────┴───────────────────────────────────────────────┼──────────────────────────┤
│ **TOTAL ESTIMATED HARDWARE INVESTMENT:**                             │ **$1,799 – $2,025 USD**  │
└──────────────────────────────────────────────────────────────────────┴──────────────────────────┘
```

---

## 5. Architectural & Deployment Validation

### 1. Dual NVDEC Hardware Decoding
* Standard GeForce RTX 40-series cards feature unrestricted hardware decoding sessions (NVDEC).
* The **RTX 4070 Ti Super** contains **Dual 8th Gen NVDEC engines**, allowing parallel decode of 30+ 720p/1080p H.264/H.265 streams with negligible CPU overhead (<10% CPU usage).

### 2. Supermarket Network Topology & Isolation
```
 [ 30+ Supermarket Dahua / ONVIF Cameras ]
                    │
                    ▼ (PoE Cat6 Cabling)
     [ Managed 32-Port PoE Gigabit Switch ]
     (Isolated Surveillance VLAN: 192.168.20.0/24)
                    │
                    ▼ (Uplink 1GbE / 2.5GbE)
    [ Edge AI Workstation (2.5GbE Onboard NIC) ]
                    │
                    ▼ (Secondary NIC / Store LAN)
    [ Internet Gateway / POS Cash Registers ]
```
* **Surveillance VLAN Isolation:** Isolate all CCTV cameras onto a dedicated subnet to eliminate packet contention with supermarket POS systems.
* **Ingress Throughput:** 32 cameras $\times$ 2 Mbps $\approx 64$ Mbps, leaving $>90\%$ bandwidth free on the uplink cable.

### 3. Power Protection & 24/7 Operational Resilience
* **Uninterruptible Power Supply (UPS):** Pair the system with an **APC Smart-UPS 1000VA / 1500VA LCD** (approx. $300–$450).
  * System continuous draw: 180W–240W.
  * Runtime on battery: 35–50 minutes during supermarket brownouts/power transitions.
* **Auto-Power-On on AC Restore:** Enable `Restore on AC Power Loss: Power ON` in the motherboard BIOS so the edge node recovers automatically after prolonged outages.

### 4. Software Stack & Toolchain Compatibility
* **Operating System:** Ubuntu 24.04 LTS (x86_64) or Arch Linux (Kernel 6.6+ LTS).
* **NVIDIA Driver:** Version 550.x or 560.x.
* **Inference Runtime:** NVIDIA TensorRT 10.x, CUDA 12.4, cuDNN 9.x.
* **Container Runtime:** Docker Engine + NVIDIA Container Toolkit (`nvidia-ctk`).

---

## 6. Commercial Pre-Built Workstation Alternatives

If internal assembly is not preferred, the following tier-1 enterprise OEM workstations match these specifications directly:

1. **Dell Precision 3680 Tower Workstation:**
   * Config: Intel Core i7-14700, 32GB DDR5, NVIDIA RTX 4070 Ti Super or RTX 4000 Ada (20GB), 2TB NVMe, 3-Year ProSupport.
   * Approx. Price: $2,400 – $2,800 USD.
2. **Lenovo ThinkStation P3 Tower:**
   * Config: Intel Core i7-14700K, 32GB DDR5, NVIDIA RTX 4080 (16GB) or RTX 4000 Ada, 2TB NVMe.
   * Approx. Price: $2,500 – $2,900 USD.
3. **HP Z2 G9 Tower Workstation:**
   * Config: Intel Core i7-14700, 32GB DDR5, NVIDIA RTX 4070 Ti / 4080, 2TB Z Turbo Drive.
   * Approx. Price: $2,450 – $2,850 USD.
