# Supermarket Edge AI CCTV System — Hardware Sizing, Calculations & Materials Checklist

> **Base Configuration:** Intel Core i5-13400F / i5-14400F | AMD Radeon™ RX 9060 XT (16GB GDDR6) | 16GB DDR5-5200 CL36 | 512GB NVMe SSD + Existing NAS Video Storage  
> **Channel Scale:** 32 Concurrent CCTV IP Camera Streams  
> **Material Classification:** `[MANDATORY]` for system operation; `[OPTIONAL]` marked based on existing supermarket infrastructure (Router, UPS, Cameras, Rack).  
> **Associated Word Document:** [Supermarket_Edge_PC_Low_Cost_Spec_and_Calculations.docx](Supermarket_Edge_PC_Low_Cost_Spec_and_Calculations.docx)  

---

## 1. Executive Summary & Cost Benefits

This engineering document establishes the mathematical sizing, memory allocations, and verified component requisitions for deploying a cost-optimized 32-camera edge AI video analytics platform.

By offloading continuous 24/7 video loop archival to the supermarket’s **existing Network Attached Storage (NAS)** and deploying an **Intel Core i5 paired with the AMD Radeon RX 9060 XT (16GB GDDR6)**, the compute appliance capital expenditure is reduced by over 50% (from **~$2,000 down to ~$833–$953 USD**) while strictly maintaining:
* **The 16GB VRAM Mandate:** Accommodates 32 hardware-decoded frame buffers, tensor staging queues, multi-model vision weights (YOLOv8 + Pose + ReID), and an 8B local market reasoning LLM in dedicated GPU memory.
* **Deterministic Hardware Decoding:** Dedicated AMD Video Core Next (VCN 4.0/5.0) silicon decodes all 32 camera streams with near-zero CPU load (CPU usage sits at 12%–20%).
* **Real-Time Loss Prevention & Kinematics:** Instant detection of shelf sweeping, pocket/bag concealment, sweethearting scanner bypass, and unpaid pushout exit attempts.

---

## 2. Engineering Calculations & Workload Sizing (32 Cameras)

### 2.1 Network Ingress Bandwidth Math
* **Sub-Streams (720p @ H.265 / 2.0 Mbps for AI Vision):**  
  32 cameras × 2.0 Mbps = **64.0 Mbps (~8.0 MB/s)**
* **Main-Streams (1080p @ H.265 / 6.0 Mbps for 24/7 DVR NAS Loop):**  
  32 cameras × 6.0 Mbps = **192.0 Mbps (~24.0 MB/s)**
* **Total Continuous Network Ingress:**  
  **256.0 Mbps (approx. 32.0 MB/s)**
* **Interface Sizing & Jitter Headroom:** On a **2.5 GbE (2,500 Mbps)** port, 256 Mbps continuous traffic utilizes only **~10.2% wire capacity**. Even during synchronized 32-camera I-frame bursts (500–600 Mbps), link saturation remains below 25%, guaranteeing zero packet jitter or dropped frames.

---

### 2.2 Hardware Video Decoding Throughput (AMD VCN 4.0 / 5.0)
* **Aggregate AI Decode Requirement:** 32 cameras decimated to 3 FPS = **96 FPS aggregate**.
* **Dedicated Decoder Capacity:** AMD VCN hardware decoder is rated for **>260 FPS of 1080p or >650 FPS of 720p H.264/H.265**.
* **Decoder Utilization:** At 96 FPS aggregate, the hardware decoder operates at approximately **15% to 18% capacity**, leaving massive headroom for temporary incident zoom or high-frame-rate checkout audits.

---

### 2.3 Dedicated GPU VRAM Allocation Model (16GB GDDR6)

| Workload Component | Allocation (GB) | Memory Target | Technical Function |
| :--- | :---: | :--- | :--- |
| **1. Video Decoding Buffers** | 0.90 – 1.10 GB | VCN / Surface Pool | Decoded Picture Buffer (DPB) for 32 reference streams |
| **2. Tensor Staging & Pre-processing** | 0.80 – 1.00 GB | VRAM Staging | NV12 to RGB planar, 640x640 scaling, batch queues |
| **3. AI Models (YOLO + Pose + ReID)** | 2.75 – 3.00 GB | ROCm / ONNX | YOLOv8s (0.6GB), Pose (0.85GB), ReID (0.95GB), Demo (0.35GB) |
| **4. Driver Context & Cache** | 2.10 – 2.40 GB | Kernel / OS | ROCm driver context, MIOpen/GEMM kernel algorithm cache |
| **5. Local Market LLM (Ollama 8B)** | 5.20 – 5.80 GB | Weights & KV Cache | 4-bit quantized weights + 4K token KV cache context |
| **6. Dynamic Headroom & Burst Buffer** | 2.70 – 3.25 GB | Free VRAM | Prevents Out-Of-Memory (OOM) faults during customer rush |
| **TOTAL COMBINED VRAM** | **14.45 / 16.0 GB** | **Fits Cleanly** | **Leaves ~1.5 GB to 3.0 GB safety margin** |

---

### 2.4 Host System RAM Footprint Calculation (Why 16GB Works)

Because video decoding, frame scaling, tensor inferencing, and LLM reasoning all run inside the dedicated 16GB GPU VRAM, the host CPU system RAM is responsible only for OS processes, network socket queues, and local metadata caching:

| Subsystem / Process | Active RAM Footprint | Operational Function |
| :--- | :---: | :--- |
| **Linux OS & Kernel Core** | 1.10 – 1.30 GB | Headless Linux kernel 6.6+ LTS, udev, network stack |
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
* **Python Virtualenv, Dependencies & Toolchain:** 15 GB
* **AI Vision Models & Ollama Weights (YOLOv8, Pose, 8B LLM):** 20 GB
* **SQLite Database & DuckDB Metadata:** 30 GB
* **Local Emergency Failover Ring Buffer:** 50 GB *(stores up to 35 minutes of full 32-camera video if NAS drops)*
* **Free Unallocated Drive Space (Wear-Leveling):** **~372 GB (>72% free)**, ensuring 10+ years NVMe lifespan.

---

### 2.6 NAS Server Storage Throughput & Retention
* **Continuous Write Bandwidth to NAS:** 32 cameras × 6.0 Mbps = 192 Mbps (~**24.0 MB/s**).
* **NAS Network Port Headroom:** 24.0 MB/s utilizes only **21%** of a standard 1 GbE link (or **7.7%** of a 2.5 GbE link).
* **Required RAID Storage Capacity on NAS:**  
  24.0 MB/s = **86.4 GB/hour** = **2.07 TB/day**
  - **7-Day Retention:** ~14.5 TB
  - **14-Day Retention:** ~29.0 TB
  - **30-Day Retention:** ~62.0 TB

---

## 3. Itemized List of Materials Required (BOM)

### 3.1 Edge AI Compute Appliance (The Edge PC)

| Status | Component | Verified Part Specification | Qty | Est. Price (USD) |
| :--- | :--- | :--- | :---: | :---: |
| `[MANDATORY]` | **Dedicated GPU** | AMD Radeon™ RX 9060 XT (16GB GDDR6, 128-bit) [Alt: RX 7600 XT 16GB] | 1 | $330 – $360 |
| `[MANDATORY]` | **Host Processor (CPU)** | Intel Core i5-13400F or i5-14400F (10 Cores, 16 Threads, 65W TDP) | 1 | $165 – $190 |
| `[MANDATORY]` | **Motherboard** | ASRock B760M Pro RS or MSI PRO B760M-A (Micro-ATX, 2.5GbE, Dual M.2) | 1 | $110 – $130 |
| `[MANDATORY]` | **System Memory (RAM)** | 16GB (2x 8GB) DDR5-5200 MHz CL36 Dual-Channel Kit (Crucial/Kingston) | 1 | $55 – $65 |
| `[OPTIONAL]` | **RAM Capacity Upgrade** | 32GB (2x 16GB) DDR5-5600 CL30 (Enables 10GB RAM-disk pre-alarm buffer) | 1 | *(+$35 upgrade)* |
| `[MANDATORY]` | **Local Storage (SSD)** | 512GB WD Blue SN580 or Crucial P3 Plus PCIe 4.0 M.2 NVMe SSD | 1 | $40 – $48 |
| `[MANDATORY]` | **Power Supply (PSU)** | 650W 80+ Bronze/Gold (MSI MAG A650BN / Corsair CX650) | 1 | $55 – $65 |
| `[MANDATORY]` | **Computer Chassis** | Montech Air 100 Mesh or DeepCool CC360 (Micro-ATX, 4x Fans, High-Airflow) | 1 | $45 – $55 |
| `[MANDATORY]` | **CPU Cooler** | Thermalright Assassin X 120 Refined SE (120mm Silent Air Tower Cooler) | 1 | $18 – $20 |
| `[MANDATORY]` | **Secondary Network Card** | PCIe 2.5 GbE Network Card (Realtek RTL8125B) for CCTV isolated VLAN | 1 | $15 – $20 |
| **SUBTOTAL** | **Edge AI Compute PC** | **Complete Base Turnkey PC Build** | | **$833 – $953 USD** |

---

### 3.2 Network Infrastructure (Switching & Routing)

| Status | Component | Verified Part Specification | Qty | Est. Price (USD) |
| :--- | :--- | :--- | :---: | :---: |
| `[MANDATORY]` | **48-Port PoE+ Managed Switch** | TP-Link Omada TL-SG3452P (48x GbE PoE+, 384W power budget, 4x SFP) | 1 | $480 – $520 |
| `[OPTIONAL]` | **Commercial 2.5GbE Router** | TP-Link Omada ER707-M2 (2x 2.5GbE, 4x GbE, 802.1Q VLANs, Multi-WAN) *(Procure if current router lacks 2.5GbE or 802.1Q VLAN support)* | 1 | $165 – $190 |
| `[OPTIONAL]` | **Cellular Failover Modem** | 4G/5G USB Dongle (Plugs into router for automatic internet failover) | 1 | $45 – $70 |

---

### 3.3 Power Protection & Battery Backup (UPS)

| Status | Component | Verified Part Specification | Qty | Est. Price (USD) |
| :--- | :--- | :--- | :---: | :---: |
| `[OPTIONAL]` | **1500VA Pure Sine Wave UPS** | APC Smart-UPS SMT1500IC (1500VA / 1000W Pure Sine Wave, LCD, USB) *(Procure if existing site lacks 1500VA UPS with USB auto-shutdown)* | 1 | $550 – $620 |
| `[OPTIONAL]` | **Alternative UPS (Budget)** | CyberPower PR1500ELCD (1500VA / 1500W Pure Sine Wave, LCD, USB) | 1 | $460 – $510 |
| `[MANDATORY]` | **UPS USB Signaling Cable** | Type-A to Type-B Cable (Connects UPS to Edge PC for Linux auto-shutdown) | 1 | Included in box |

---

### 3.4 Cameras (32 Channels — Optional if Existing Cameras Reused)

| Status | Zone / Department | Recommended Part Specification | Qty | Est. Price (USD) |
| :--- | :--- | :--- | :---: | :---: |
| `[OPTIONAL]` | **Grocery Aisles 1–12** | Dahua IPC-HDW2441T-S (3.6mm Lens, 88° FOV, 4MP Starlight, 120dB WDR, IP67) | 12 | $900 – $1,020 |
| `[OPTIONAL]` | **Fresh Produce & Bakery** | Dahua IPC-HDW2441T-S (2.8mm Wide Lens, 107° FOV, 4MP Starlight, 120dB WDR) | 4 | $300 – $340 |
| `[OPTIONAL]` | **POS Checkouts 1–6** | Dahua IPC-HDW2441T-S (3.6mm/6mm Lens, 4MP @ 30 FPS, Built-in Mic) | 6 | $450 – $510 |
| `[OPTIONAL]` | **Entrances & Foyers** | Dahua IPC-HDBW2441E-S (2.8mm Wide, 4MP, 120dB True WDR, IK10 Vandal Dome) | 4 | $320 – $360 |
| `[OPTIONAL]` | **Liquor & Logistics** | Dahua IPC-HDW2441T-S (Turret) + Dahua IPC-HFW2441S-S (Bullet IP67) | 6 | $450 – $510 |
| **SUBTOTAL** | **32x Surveillance Cameras** | **32× Dahua 4MP WizSense IP Cameras (Optional if existing retained)** | | **$2,420 – $2,740 USD** |

---

### 3.5 Server Cabinet, Cabling & Installation Accessories

| Status | Component | Verified Part Specification | Qty | Est. Price (USD) |
| :--- | :--- | :--- | :---: | :---: |
| `[OPTIONAL]` | **Equipment Server Cabinet** | 9U or 12U Wall-Mount Enclosure (600mm Depth, Glass Door) *(Procure if missing)* | 1 | $140 – $180 |
| `[MANDATORY]` | **Cat6 48-Port Patch Panel** | 1U 19" Cat6 110 Punch-Down or Keystone Jack Panel *(Procure if missing)* | 1 | $35 – $50 |
| `[OPTIONAL]` | **Bulk Cat6 UTP Cable** | 305m (1000ft) Box Solid Bare Copper UTP 23AWG (2 boxes = 610m) *(If rewiring)* | 2 boxes | $220 – $280 |
| `[MANDATORY]` | **Pass-Through RJ45 Plugs** | Cat6 Pass-Through RJ45 8P8C Plugs + Relief Boots (Pack of 100) | 1 pk | $18 – $22 |
| `[MANDATORY]` | **Rackmount 8-Outlet PDU** | 1U 19" Horizontal PDU Power Strip with Surge Suppression | 1 | $35 – $45 |
| `[MANDATORY]` | **Cat6 Patch Leads (35-pk)** | 0.5m & 1.0m Snagless Molded Slim Cat6 Patch Cords (Switch to Panel) | 1 pk | $28 – $35 |
| `[OPTIONAL]` | **Horizontal Cable Manager** | 1U 19" Finger Duct Cable Management Panel with Removable Cover | 1 | $15 – $20 |

---

### 3.6 Existing NAS Server Requirements (Zero New Purchase)

| Status | Component | Required Configuration & Specifications | Qty | Est. Price (USD) |
| :--- | :--- | :--- | :---: | :---: |
| `[EXISTING]` | **Network Storage (NAS)** | Existing Supermarket NAS (Synology, QNAP, TrueNAS, unRAID) via NFS v4 / SMB 3.0 | 1 unit | **$0.00 (Existing)** |
| `[EXISTING]` | **NAS Storage Pool** | RAID-5 / RAID-6 pool with surveillance HDDs (~14.5TB for 7-day, ~29TB for 14-day) | 1 pool | **$0.00 (Existing)** |
| `[EXISTING]` | **NAS Network Interface** | 1 GbE or 2.5 GbE Ethernet connection to core switch (~24 MB/s throughput) | 1 port | **$0.00 (Existing)** |

---

## 4. Procurement Investment Summary

| Requisition Category | Scope Included | Price Range (USD) |
| :--- | :--- | :---: |
| **1. Edge AI Appliance PC `[MANDATORY]`** | Core i5, RX 9060 XT 16GB, 16GB DDR5, 512GB SSD, 650W PSU, Case, Dual NIC | **$833 – $953 USD** |
| **2. Network Infrastructure `[MANDATORY / OPT]`** | 48-Port PoE+ Switch ($480–$520) `[MANDATORY]` + Router ($165–$190) `[OPTIONAL]` | **$480 – $710 USD** |
| **3. Battery Backup UPS `[OPTIONAL]`** | 1500VA / 1000W Pure Sine Wave Smart UPS with USB auto-shutdown cable | **$460 – $620 USD** |
| **4. Racking & Cabling `[MANDATORY / OPT]`** | Patch panel, PDU, Patch leads, RJ45 `[MANDATORY]`; Rack & Bulk Cable `[OPTIONAL]` | **$116 – $632 USD** |
| **5. 32x Surveillance Cameras `[OPTIONAL]`** | 32× Dahua 4MP WizSense IP Cameras (Optional if existing cameras are retained) | **$2,420 – $2,740 USD** |

* **Total Mandatory Hardware (Edge AI PC Only):** **$833 – $953 USD**
* **Total Infrastructure Upgrade (PC + PoE Switch + Router + UPS + Rack/Cables):** **$2,429 – $2,915 USD**
* **All-Inclusive Complete Total (With 32 New Dahua Cameras):** **$4,849 – $5,655 USD**
