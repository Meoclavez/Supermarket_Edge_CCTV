# IGA Supermarket — Bill of Materials (BOM)

> **Deployment Site:** IGA Supermarket (Pearcedale, Victoria)  
> **System Scope:** 32-Channel Edge AI CCTV Video Analytics  
> **Base Compute:** Intel Core i5 | AMD Radeon RX 9060 XT (16GB) | 16GB DDR5 | 512GB SSD + NAS Storage  
> **Documents:** [Word (.docx)](IGA_Pearcedale_CCTV_BOM.docx) | [PDF (.pdf)](IGA_Pearcedale_CCTV_BOM.pdf)  

---

## Mandatory

| Component | Description | Qty |
| :--- | :--- | :---: |
| **Dedicated AI GPU** | AMD Radeon™ RX 9060 XT (16GB GDDR6, 128-bit) [Alt: RX 7600 XT 16GB] | 1 |
| **Host Processor (CPU)** | Intel Core i5-13400F or i5-14400F (10 Cores, 16 Threads, 65W TDP) | 1 |
| **Motherboard** | Intel B760M Micro-ATX (2.5GbE LAN, Dual M.2 PCIe 4.0) | 1 |
| **System Memory (RAM)** | 16GB (2x 8GB) DDR5-5200 MHz CL36 Dual-Channel Kit | 1 |
| **Local Storage (SSD)** | 512GB M.2 PCIe 4.0 NVMe SSD (WD Blue SN580 / Crucial P3 Plus) | 1 |
| **Power Supply (PSU)** | 650W 80+ Bronze / Gold Certified ATX Power Supply | 1 |
| **Computer Chassis** | Micro-ATX High-Airflow Mesh Case with 4x Cooling Fans | 1 |
| **CPU Cooler** | Thermalright Assassin X 120 Refined SE (120mm Tower Cooler) | 1 |
| **Secondary Network Card** | PCIe 2.5 GbE Network Card (Realtek RTL8125B) for isolated CCTV VLAN | 1 |
| **48-Port PoE+ Switch** | TP-Link Omada TL-SG3452P (48× GbE PoE+, 384W Power Budget, 4× SFP) | 1 |
| **Cat6 Patch Panel** | 1U 19" 48-Port Cat6 Unshielded Patch Panel (110 Punch-Down / Keystone) | 1 |
| **Rackmount 8-Outlet PDU** | 1U 19" Horizontal PDU Power Strip with Surge Suppression | 1 |
| **Cat6 Molded Patch Leads** | 0.5m & 1.0m Snagless Slim Cat6 Patch Cords (Switch to Panel) | 35 |
| **RJ45 Connectors** | Cat6 Pass-Through RJ45 8P8C Plugs + Strain Relief Boots | 100 pk |
| **Existing NAS Storage** | Supermarket NAS mount via NFS/SMB for 24/7 video loop (24 MB/s stream) | Existing |

---

## Optional

| Component | Description | Qty |
| :--- | :--- | :---: |
| **Commercial 2.5GbE Router** | TP-Link Omada ER707-M2 (2x 2.5GbE, 4x GbE, 802.1Q VLANs) — *If current router lacks 2.5GbE/VLANs* | 1 |
| **Battery Backup (UPS)** | APC Smart-UPS SMT1500IC 1500VA/1000W Pure Sine Wave with USB cable — *If site lacks 1500VA UPS* | 1 |
| **Surveillance Cameras (32x)** | 32× Dahua 4MP WizSense IP Cameras (Aisles, Produce, Bakery, Checkouts with Mic, IK10 Domes) — *If replacing* | 32 |
| **32GB RAM Upgrade** | 32GB (2x 16GB) DDR5-5600 CL30 — *Upgrades base 16GB for 10GB RAM-disk pre-alarm buffer* | 1 |
| **Equipment Server Cabinet** | 9U or 12U Wall-Mount Network Enclosure (600mm Depth, Glass Door) — *If rack space unavailable* | 1 |
| **Bulk Cat6 Network Cable** | 305m (1000ft) Box Solid Bare Copper Cat6 UTP 23AWG (2 boxes = 610m) — *If re-cabling runs* | 2 boxes |
| **Horizontal Cable Manager** | 1U 19" Finger Duct Cable Management Panel with Removable Cover | 1 |
| **Cellular Failover Modem** | 4G/5G USB LTE Dongle *(Plugs into router for automatic cellular internet failover)* | 1 |
