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

### B. Hardware Video Decoding (Single 5th-Gen NVDEC vs. Dual NVENC)
* Continuous decoding of 32 concurrent 720p streams requires dedicated hardware video decode logic.
* Software CPU decoding 32 streams would consume $\approx 80\text{–}100\%$ of a 16-core CPU.
* **NVDEC Architecture Clarification:** The **RTX 4070 Ti Super (AD103)** features **1x NVDEC decoder (5th-Gen)** and **2x NVENC encoders (8th-Gen)**. (Dual NVDEC is only present on AD102 / RTX 4090).
* **Single NVDEC Empirical Throughput:** A single 5th-Gen NVDEC engine decodes over **3,800 FPS of 720p H.264/H.265** (equivalent to >120 concurrent 720p streams at 30 FPS). At 32 cameras $\times$ 3 FPS decimated = **96 FPS aggregate**, the single NVDEC engine operates at **$<3\%$ compute capacity** (or $<25\%$ even if decoding 32 streams at full 30 FPS). Hence, single-decoder capacity provides immense headroom for 30+ cameras.

### C. Aggregate AI Inference Frame Rate
$$\text{Aggregate AI FPS} = 32\text{ Cameras} \times 3.0\text{ FPS (Decimated)} = \mathbf{96\text{ FPS}}$$
$$\text{Per-Batch Processing Budget} = \frac{1000\text{ ms}}{96\text{ FPS}} \approx \mathbf{10.4\text{ ms}}$$
* TensorRT 10.x running batched YOLOv11s on an RTX 4070 Ti Super / RTX 5070 Ti executes in **$\approx 1.8\text{ ms}$ to $2.8\text{ ms}$**, leaving over **$70\%$ compute headroom** for secondary keypoint pose estimation and Re-ID feature extraction.

### D. VRAM Allocation Model & Critical "SUPER" Mandate
$$V_{\text{total}} = V_{\text{frame\_buffers}} + V_{\text{models}} + V_{\text{cuda\_context}} + V_{\text{headroom}}$$

$$\begin{aligned}
V_{\text{frame\_buffers}} &= 32 \times (1280 \times 720 \times 3 \text{ bytes} \times \text{FP16}) \times 4 \text{ batch} \approx \mathbf{1.8\text{ GB}} \\
V_{\text{models (TensorRT FP16)}} &= \underbrace{0.6\text{ GB}}_{\text{YOLOv11s}} + \underbrace{0.85\text{ GB}}_{\text{YOLO-Pose}} + \underbrace{0.9\text{ GB}}_{\text{FastReID}} + \underbrace{0.35\text{ GB}}_{\text{Demographics}} \approx \mathbf{2.7\text{ GB}} \\
V_{\text{cuda\_context \& shm}} &\approx \mathbf{2.5\text{ GB}} \\
V_{\text{local\_llm\_headroom}} &\approx \mathbf{4.5\text{ GB to } 6.0\text{ GB}} \quad (\text{Quantized Qwen 2.5 7B / Llama 3.2 3B}) \\
\hline
\mathbf{Total\ Minimum\ VRAM} &\approx \mathbf{11.5\text{ GB to } 13.0\text{ GB}} \implies \mathbf{16\text{ GB VRAM Minimum Required}}
\end{aligned}$$

> [!WARNING]
> **CRITICAL PROCUREMENT MANDATE: RTX 4070 Ti SUPER vs. NON-SUPER**  
> Do NOT purchase the baseline **RTX 4070 Ti (non-Super)**. It contains only **12GB VRAM** on a narrow 192-bit bus (AD104 die) and will encounter Out-of-Memory (OOM) fatal crashes during peak evening multi-camera tracking. **Only the RTX 4070 Ti SUPER (16GB VRAM, AD103 die, 256-bit bus) or RTX 5070 Ti (16GB GDDR7, GB203 die) satisfies this 16GB minimum requirement.**

---

## 3. Hardware Architecture Comparison Matrix (30+ Cameras)

| Parameter | **Option A: Custom Edge Workstation** <br> *(Top Recommendation - Best Value & Power)* | **Option B: Next-Gen Blackwell Build** <br> *(High-Bandwidth Upgrade)* | **Option C: Enterprise 1U/2U Rack Server** <br> *(Best for Datacenter / Server Rack)* |
| :--- | :--- | :--- | :--- |
| **Supported Cameras** | **30 – 48 Cameras** | **30 – 64+ Cameras** | **30 – 64+ Cameras** |
| **GPU Model** | **NVIDIA RTX 4070 Ti Super (16GB)** | **NVIDIA RTX 5070 Ti (16GB GDDR7)** | **NVIDIA RTX 4000 Ada (20GB)** or **L4 (24GB)** |
| **GPU Architecture** | Ada Lovelace (AD103, 16GB GDDR6X) | Blackwell (GB203-300, 16GB GDDR7) | Ada Lovelace Enterprise (Single-Slot, ECC) |
| **Video Decoders** | **1x 5th Gen NVDEC** (>3,800 FPS 720p decode) | **1x 6th Gen NVDEC** (AV1/HEVC upgraded VPU) | **1x NVDEC + AV1 Decode** (Enterprise 24/7) |
| **Video Encoders** | **2x 8th Gen NVENC** (Dual AV1/HEVC encoders) | **2x 9th Gen NVENC** (Dual AV1 4:2:2 pro) | **2x 8th Gen NVENC** (Dual encoders) |
| **Memory Bandwidth** | **672 GB/s** (256-bit GDDR6X) | **896 GB/s** (256-bit GDDR7 — +33% faster) | **360 GB/s** (160-bit GDDR6 ECC) |
| **AI Performance** | **706 Tensor TOPS (FP16/INT8)** | **900+ Tensor TOPS (Native FP4/FP8)** | **300+ TOPS (INT8) / 20GB ECC** |
| **Host CPU** | **Intel Core i7-14700** (20C/28T + UHD 770) | **Intel Core i7-14700** (20C/28T + UHD 770) | Intel Xeon E-2488 (8C/16T) or AMD EPYC |
| **System RAM** | **32 GB – 64 GB DDR5-5600MHz** | **32 GB – 64 GB DDR5-6000MHz** | **64 GB DDR5 ECC** |
| **Storage** | 2TB PCIe 4.0 NVMe SSD (7,000+ MB/s) | 2TB PCIe 4.0/5.0 NVMe SSD (7,400+ MB/s) | Dual 1.92TB Enterprise NVMe (RAID-1) |
| **Power Consumption** | 65W Idle / 240W–280W Peak (750W PSU) | 70W Idle / 280W–320W Peak (850W ATX3.1 PSU) | 80W Idle / 180W–240W Peak |
| **Est. System Cost** | **$1,799 – $2,025 USD** | **$1,850 – $2,100 USD** (MSRP $749 GPU) | **$3,200 – $4,500 USD** |
| **Recommendation** | ⭐⭐⭐⭐⭐ **(Proven Stability, Standard x86)** | ⭐⭐⭐⭐⭐ **(Best Future-Proofing & Speed)** | ⭐⭐⭐⭐☆ **(Best for Enterprise Server Rooms)** |

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

### 1. Dedicated Hardware Video Decoding (Single NVDEC Architecture)
* Standard GeForce RTX 40-series and 50-series cards feature unrestricted hardware decoding sessions (NVDEC).
* The **RTX 4070 Ti Super (AD103)** contains **1x 5th-Gen NVDEC engine** (paired with **Dual 8th-Gen NVENC encoders**).
* **Throughput Validation:** A single 5th-Gen NVDEC engine delivers **>3,800 FPS of 720p H.264/H.265 throughput**. For 32 cameras decimated to 3 FPS (96 aggregate FPS), the single decoder engine runs at **<3% compute load** (and <25% load even at full 30 FPS). It decodes 30+ streams with near-zero CPU overhead (<8% CPU load).
* **Blackwell RTX 5070 Ti (GB203):** Features **1x 6th-Gen NVDEC engine** with upgraded AV1/HEVC decoding efficiency and higher silicon throughput.

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
