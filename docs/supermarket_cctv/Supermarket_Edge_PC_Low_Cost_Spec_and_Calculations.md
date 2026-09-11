# Supermarket Edge AI CCTV System — Hardware Sizing & Workload Calculations Guide

> **Target Configuration:** Intel Core i5-13400F / i5-14400F | AMD Radeon™ RX 9060 XT (16GB GDDR6) | 16GB DDR5-5200 CL36 | 512GB NVMe SSD + NAS Video Storage  
> **Target Scale:** 32 Concurrent CCTV IP Camera Feeds  
> **Document Status:** Finalized Low-Cost Procurement Specification  
> **Document File:** [Supermarket_Edge_PC_Low_Cost_Spec_and_Calculations.docx](Supermarket_Edge_PC_Low_Cost_Spec_and_Calculations.docx)  

---

## 1. Executive Summary & Cost Benefit

This engineering specification details the component requirements, memory budgets, and workload throughput mathematics for deploying a cost-optimized 32-camera edge AI video analytics system.

By shifting continuous 24/7 video loop recording to the supermarket’s **existing Network Attached Storage (NAS)** and deploying an **Intel Core i5 paired with the AMD Radeon RX 9060 XT (16GB GDDR6)**, the total system capital expenditure is reduced from **~$2,000 down to ~$830–$950 USD** (>50% savings) while strictly retaining:
* **The 16GB VRAM Mandate:** Holds all 32 decoding frame buffers, multi-model vision weights, and an 8B local market LLM.
* **Deterministic Hardware Decoding:** Dedicated AMD Video Core Next (VCN) silicon decodes all streams with near-zero CPU load.
* **Continuous Multi-Camera Vision:** Real-time YOLOv8 person detection, shelf wrist-reach kinematics, and loss prevention theft detection.

---

## 2. Engineering Calculations & Mathematical Sizing (32 Cameras)

### 2.1 Network Ingress Bandwidth Math
* **Sub-Streams (720p @ H.265 / 2.0 Mbps for AI Vision):**  
  32 cameras × 2.0 Mbps = **64.0 Mbps (~8.0 MB/s)**
* **Main-Streams (1080p @ H.265 / 6.0 Mbps for 24/7 DVR NAS Loop):**  
  32 cameras × 6.0 Mbps = **192.0 Mbps (~24.0 MB/s)**
* **Total Continuous Network Ingress:**  
  **256.0 Mbps (approx. 32.0 MB/s)**
* **Port Sizing:** On a **2.5 GbE (2,500 Mbps)** network port, 256 Mbps continuous traffic utilizes only **~10.2% wire capacity**. Even during synchronized I-frame bursts (up to 500–600 Mbps), link utilization stays under 25%, guaranteeing zero packet jitter or dropped frames.

---

### 2.2 Hardware Video Decoding Throughput (AMD VCN 4.0 / 5.0)
* **Aggregate AI Decode Requirement:** 32 cameras decimated to 3 FPS = **96 FPS aggregate**.
* **Decoder Capacity:** AMD VCN ASIC decodes **>260 FPS of 1080p or >650 FPS of 720p H.264/H.265**.
* **Load:** At 96 FPS aggregate, the hardware decoder operates at **~15% to 18% capacity**, providing immense headroom for temporary incident zoom or checkout frame rate boosts.

---

### 2.3 Dedicated GPU VRAM Allocation Model (16GB GDDR6)

| Workload Component | Allocation (GB) | Memory Target | Technical Function |
| :--- | :--- | :--- | :--- |
| **1. Video Decoding Buffers** | 0.90 – 1.10 GB | VCN / Surface Pool | Decoded Picture Buffer (DPB) for 32 reference streams |
| **2. Tensor Staging & Pre-processing** | 0.80 – 1.00 GB | VRAM Staging | NV12 to RGB planar, 640x640 scaling, batch queues |
| **3. AI Models (YOLO + Pose + ReID)** | 2.75 – 3.00 GB | ROCm / ONNX | YOLOv8s (0.6GB), Pose (0.85GB), ReID (0.95GB), Demo (0.35GB) |
| **4. Driver Context & Cache** | 2.10 – 2.40 GB | Kernel / OS | ROCm driver context, MIOpen/GEMM algorithms cache |
| **5. Local Market LLM (Ollama 8B)** | 5.20 – 5.80 GB | Weights & KV Cache | 4-bit quantized weights + 4K token KV cache |
| **6. Dynamic Headroom & Burst Buffer** | 2.70 – 3.25 GB | Free VRAM | Prevents Out-Of-Memory (OOM) faults during peak hours |
| **TOTAL COMBINED VRAM** | **14.45 / 16.0 GB** | **Fits Cleanly** | **Leaves ~1.5 GB to 3.0 GB safety margin** |

---

### 2.4 Host System RAM Footprint Calculation (Why 16GB Works)

| Subsystem / Process | Active RAM Footprint | Operational Function |
| :--- | :--- | :--- |
| **Linux OS & Kernel Core** | 1.10 – 1.30 GB | Headless Linux kernel 6.6+ LTS, udev, network drivers |
| **System Daemons & Logging** | 0.30 – 0.40 GB | systemd, journald, logrotate, SSH server |
| **Edge CCTV Backend Core** | 0.80 – 1.00 GB | FastAPI server, Uvicorn workers, REST routes, WebSockets |
| **32-Stream RTSP TCP/UDP Buffers** | 1.20 – 1.50 GB | Socket ring queues, de-jitter buffers across 32 cameras |
| **SQLite (WAL) & DuckDB Time-Series** | 0.90 – 1.20 GB | In-memory query cache, shopper tracks, theft incident logs |
| **Temporary Frame Staging & IPC** | 0.60 – 0.80 GB | Inter-process shared memory between ingest and tracker |
| **Linux Page Cache & Write Buffer** | 2.00 – 2.30 GB | Asynchronous buffered I/O writes to NVMe and NAS |
| **Total Active RAM Footprint** | **6.90 – 8.50 GB** | **~50% of 16GB Total** |
| **Free Safety Buffer** | **7.50 – 9.10 GB** | **47% to 57% Headroom remaining** |

---

### 2.5 Local Storage Allocation (512GB M.2 PCIe 4.0 NVMe SSD)

* **Linux OS, Kernel, Drivers & Docker:** 25 GB
* **Python Virtualenv & Toolchain:** 15 GB
* **AI Vision Models & Ollama Weights (YOLOv8, Pose, 8B LLM):** 20 GB
* **SQLite Database & DuckDB Metadata:** 30 GB
* **Local Emergency Failover Ring Buffer:** 50 GB *(holds ~35 minutes of 32-camera video if NAS temporarily disconnects)*
* **Free Unallocated Drive Space (Wear-Leveling):** **~372 GB (>72% free)**, ensuring a 10+ year NVMe operating lifespan.

---

### 2.6 NAS Server Storage Throughput & Retention
* **Continuous Write Bandwidth to NAS:** 32 cameras × 6 Mbps = 192 Mbps (~**24.0 MB/s**).
* **NAS Network Headroom:** 24.0 MB/s utilizes only **21%** of a standard 1 GbE NAS link (or **7.7%** of a 2.5 GbE NAS link).
* **Storage Capacity Required on NAS:**  
  24.0 MB/s = **86.4 GB/hour** = **2.07 TB/day**
  - **7-Day Retention:** ~14.5 TB
  - **14-Day Retention:** ~29.0 TB
  - **30-Day Retention:** ~62.0 TB

---

## 3. Itemized Bill of Materials (BOM)

| Component | Recommended Part Specification | Qty | Est. Price (USD) |
| :--- | :--- | :--- | :--- |
| **1. Dedicated GPU** | AMD Radeon™ RX 9060 XT (16GB GDDR6, 128-bit) [Alt: RX 7600 XT 16GB] | 1 | $330 – $360 |
| **2. Host Processor (CPU)** | Intel Core i5-13400F or i5-14400F (10C / 16T, 65W TDP) | 1 | $165 – $190 |
| **3. Motherboard** | ASRock B760M Pro RS or MSI PRO B760M-A (Micro-ATX, 2.5GbE, Dual M.2) | 1 | $110 – $130 |
| **4. System Memory (RAM)** | 16GB (2x 8GB) Crucial / Kingston DDR5-5200 MHz CL36 (Dual-Channel) | 1 | $55 – $65 |
| **5. Local Storage (SSD)** | 512GB WD Blue SN580 or Crucial P3 Plus PCIe 4.0 M.2 NVMe | 1 | $40 – $48 |
| **6. 24/7 DVR Storage** | Existing Supermarket NAS Server (NFS / SMB Mount over Ethernet) | 1 | $0.00 (Existing) |
| **7. Power Supply (PSU)** | 650W 80+ Bronze / Gold (MSI MAG A650BN / Corsair CX650) | 1 | $55 – $65 |
| **8. Computer Chassis** | Montech Air 100 Mesh or DeepCool CC360 (Micro-ATX, High-Airflow) | 1 | $45 – $55 |
| **9. CPU Cooler** | Thermalright Assassin X 120 Refined SE (120mm Silent Air Tower) | 1 | $18 – $20 |
| **10. Secondary NIC** | PCIe 2.5 GbE Network Card (Realtek RTL8125B) for isolated CCTV VLAN | 1 | $15 – $20 |
| **TOTAL ESTIMATED INVESTMENT** | **Turnkey Hardware Deployment** | | **$833 – $953 USD** |

---

## 4. Physical Deployment & Network Isolation Topology

* **Port 1 (Onboard 2.5 GbE):** Connects to the 32-port PoE switch on dedicated subnet `192.168.20.0/24`.
* **Port 2 (PCIe 2.5 GbE):** Connects to the store router and NAS on subnet `192.168.1.0/24`.
* **Power Draw:** Real-world continuous draw is **~220W – 240W**. A 650W PSU operates right at its peak 40%–50% efficiency curve.
* **UPS Protection:** Pair with an **APC Back-UPS Pro 1000VA** (~$160 USD) to provide ~35 minutes of runtime during brownouts.
