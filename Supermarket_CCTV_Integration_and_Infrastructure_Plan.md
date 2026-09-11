# Supermarket Edge AI CCTV System — Site Integration & Infrastructure Deployment Plan

> **Site Location:** Pearcedale, Victoria 3912, Australia  
> **Target System:** 32 Concurrent CCTV IP Camera Feeds (Dahua System Integration)  
> **Existing Infrastructure:** Configured NAS Server, 32-Channel Dahua NVR/DVR (`192.168.20.160`, Gateway `192.168.20.254`)  
> **Edge PC Hardware:** Intel Core i5 | AMD Radeon™ RX 9060 XT (16GB GDDR6) | 16GB DDR5-5200 | 512GB NVMe SSD  
> **Word Document:** [Supermarket_CCTV_Integration_and_Infrastructure_Plan.docx](Supermarket_CCTV_Integration_and_Infrastructure_Plan.docx)  

---

## 1. Purpose & Integration Scope

This deployment specification establishes the integration requirements, network architecture, power backup sizing, camera specifications, and optional equipment needed to connect the new **Edge AI CCTV Surveillance & Retail Analytics Platform** into the supermarket’s existing site infrastructure.

Key engineering goals:
1. **Zero-Contention Network Isolation:** Prevent 32 camera streams (256 Mbps) from flooding store cash registers (POS) and payment terminals.
2. **NAS Storage Integration:** Stream 24/7 video directly to the existing NAS server via NFS/SMB, saving hardware cost and preventing SSD burnout.
3. **Power Resilience:** 1500VA UPS battery protection with automated Linux USB signaling to cleanly flush databases and unmount network shares before shutdown.
4. **Verified Optics:** Camera focal lengths tailored to aisles, produce, bakery, and checkouts for maximum AI accuracy.

---

## 2. Existing NAS Server Integration Plan

```
 ┌────────────────────────────────────────────────────────┐
 │                    EDGE AI CCTV PC                     │
 │ • Linux OS + AI Vision Models + Local SQLite Metadata  │
 └───────────────────────────┬────────────────────────────┘
                             │
                             ▼ (Continuous 24.0 MB/s Stream via NFS)
 ┌────────────────────────────────────────────────────────┐
 │              EXISTING SUPERMARKET NAS SERVER           │
 │ • Mount: /mnt/nas_dvr (NFS v4 / SMB 3.0)               │
 │ • Storage Pool: RAID-5 / RAID-6 (Surveillance HDDs)    │
 └────────────────────────────────────────────────────────┘
```

* **Network Protocol:** Native Linux **NFS v4** (recommended for low latency) or **SMB 3.0** mounted at `/mnt/nas_dvr`.
* **Bandwidth Throughput:** 32 cameras $\times$ 6.0 Mbps (1080p H.265) = **192.0 Mbps (~24.0 MB/s)**.
  - A standard 1 GbE NAS port (115 MB/s) operates at only **~21% capacity**.
  - A 2.5 GbE NAS port (312 MB/s) operates at **<8% capacity**.
* **Storage Consumption & Retention:**
  - Hourly footprint: **~86.4 GB / hour**
  - Daily footprint: **~2.07 TB / day**
  - **7-Day Retention:** ~14.5 TB
  - **14-Day Retention:** ~29.0 TB
  - **30-Day Retention:** ~62.0 TB
* **Local Failover Ring Buffer:** The Edge PC's 512GB local NVMe SSD reserves **50 GB** as an emergency buffer. If the NAS reboots or disconnects for up to 35 minutes, video is cached locally and automatically synced once the NAS reconnects.

---

## 3. Network Router & Managed PoE Switch Upgrade Plan

### 3.1 Commercial Security Router / Gateway
Standard ISP routers cannot manage multi-VLAN isolation or high-density video traffic.

| Model | Ports & Speed | Key Capabilities | Est. Price (USD) |
| :--- | :--- | :--- | :--- |
| **TP-Link Omada ER707-M2** *(Top Choice)* | 2× 2.5GbE + 4× 1GbE + 1× SFP | Multi-WAN failover (4G backup), 802.1Q VLANs, WireGuard VPN, 500k sessions | **$165 – $190** |
| **Ubiquiti UniFi Cloud Gateway Max** | 5× 2.5GbE RJ45 ports | 2.5 Gbps routing with IDS/IPS, integrated UniFi controller | **$199 – $220** |

#### Network Subnet Architecture:
* **VLAN 10 (Store LAN / POS):** `192.168.1.0/24` (Cash registers, billing PCs, store Wi-Fi).
* **VLAN 20 (CCTV Isolated Network):** `192.168.20.0/24` (32 IP cameras, NVR, Edge PC Port 1).
* **VLAN 30 (Guest Wi-Fi):** `192.168.30.0/24` (Customer access, strictly firewalled).

---

### 3.2 High-Density PoE+ Managed Switch (32 Cameras)

| Switch Model | Port Density & PoE Budget | Uplinks & SFP | Est. Price (USD) |
| :--- | :--- | :--- | :--- |
| **TP-Link Omada TL-SG3452P** *(Recommended)* | 48× Gigabit PoE+ (**384W Budget**) | 4× Gigabit SFP | **$480 – $520** |
| **Dual TP-Link TL-SG3428MP** | 2× 24× PoE+ (**384W Each = 768W**) | 4× SFP | **2× $285 = $570** |
| **Ubiquiti UniFi USW-Pro-48-PoE** | 48× PoE+ / PoE++ (**400W Budget**) | 4× 10G SFP+ | **$899** |

* **Power Sizing:** 32 cameras drawing ~6.5W average = **~208W total PoE load**. The TL-SG3452P's 384W budget operates at **~54% load**, leaving **176W headroom** and **16 spare ports** for the Edge PC, NAS, access points, and POS terminals.

---

## 4. Power Protection & UPS Sizing Plan

### 4.1 System Load Breakdown
* **Edge AI PC (i5 + RX 9060 XT):** ~220W – 240W
* **32 PoE Cameras (via Switch):** ~190W – 210W
* **PoE Switch & Router:** ~45W
* **Existing NAS Server:** ~60W – 75W
* **Total Operational Power Draw:** **~515W to 570W**

### 4.2 Recommended UPS Models

| Model | Capacity & Output | Runtime @ 550W | Est. Price (USD) |
| :--- | :--- | :--- | :--- |
| **APC Smart-UPS SMT1500IC** *(Top Commercial)* | 1500VA / 1000W Pure Sine Wave | **18 – 24 minutes** | **$550 – $620** |
| **CyberPower PR1500ELCD** *(Budget Commercial)* | 1500VA / 1500W Pure Sine Wave | **20 – 26 minutes** | **$460 – $510** |

### 4.3 Automated Linux Shutdown Mechanism (`apcupsd` / `NUT`)
A USB communication cable connects the UPS to the Edge PC (`/dev/usb/hiddev0`). When remaining battery hits **15%** (after ~15 minutes of outage), an automated daemon triggers:
1. Dispatches high-priority power outage alert to manager via dashboard and mobile.
2. Flushes SQLite WAL database (`PRAGMA wal_checkpoint(TRUNCATE)`).
3. Safely unmounts the NAS NFS volume (`umount -f /mnt/nas_dvr`).
4. Issues clean ACPI `poweroff` to prevent NVMe disk corruption and database lockups.

---

## 5. Camera Specifications Checklist (32 Channels)

| Zone / Department | Qty | Recommended Camera Model | Optical & Sensor Specs | AI & Surveillance Purpose |
| :--- | :---: | :--- | :--- | :--- |
| **Grocery Aisles 1–12** | 12 | **Dahua IPC-HDW2441T-S** | 3.6mm Lens (88° FOV), 4MP Starlight, 120dB WDR, 30m IR, IP67 | Corridor tracking, shopper dwell, trolley clash |
| **Fresh Produce & Bakery** | 4 | **Dahua IPC-HDW2441T-S** | 2.8mm Wide Lens (107° FOV), 4MP Starlight, 120dB WDR | Department overview, produce freshness color fidelity |
| **POS Checkouts 1–6** | 6 | **Dahua IPC-HDW2441T-S** | 3.6mm or 6mm Lens, 4MP @ 30 FPS, Built-in Mic | Barcode scanner and cash drawer focus; anti-sweethearting |
| **Entrances & Foyers** | 4 | **Dahua IPC-HDBW2441E-S** | 2.8mm Wide Lens, 4MP, 120dB True WDR, **IK10 Vandal Dome** | Counters glass door daylight glare; customer footfall tripwires |
| **High-Value & Logistics** | 6 | **Dahua IPC-HDW2441T-S** + **IPC-HFW2441S-S** (Bullet) | 3.6mm Lens, 4MP Starlight, Built-in Mic, IP67 Weatherproof | Shelf reach & concealment tracking (Liquor), dock logistics |

*All cameras verified to support ONVIF (Profile S/G/T), RTSP, H.265 compression, and standard 802.3af PoE.*

---

## 6. Additional Infrastructure & Optional Items Checklist

| Item Description | Technical Specification | Necessity | Est. Price (USD) | Status |
| :--- | :--- | :---: | :--- | :--- |
| **Server Equipment Cabinet** | 9U or 12U Wall-Mount Enclosure (600mm Depth, Lockable Glass Door) | **Mandatory** | $140 – $180 | Procure if missing |
| **Cat6 Patch Panel (48-Port)** | 1U 19" Cat6 110 Punch-Down or Keystone Jack Panel | **Mandatory** | $35 – $50 | Procure if missing |
| **Solid Copper Cat6 Cable** | 305m (1000ft) Box UTP Solid Bare Copper 23AWG (Blue/Grey) | **Mandatory** | $110 – $140 / box | 2 boxes (~600m) |
| **Pass-Through RJ45 Plugs** | Cat6 Pass-Through RJ45 8P8C Connectors + Relief Boots (100-pack) | **Mandatory** | $18 – $22 | Procure |
| **1U Horizontal Cable Duct** | 19" 1U Finger Duct Cable Management Panel with Cover | **Recommended** | $15 – $20 | Procure |
| **Rackmount 8-Outlet PDU** | 1U 19" Horizontal PDU Power Strip with Surge Suppression | **Mandatory** | $35 – $45 | Procure |
| **Cat6 Molded Patch Cords** | 0.5m & 1.0m Snagless Cat6 Patch Leads (35-pack for switch-to-panel) | **Mandatory** | $28 – $35 | Procure |
| **UPS USB Communication Cable** | Type-A to Type-B or RJ50-to-USB Cable for APC auto-shutdown | **Mandatory** | $10 – $12 | Included in UPS box |

---

## 7. Turnkey Procurement Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                   TOTAL TURNKEY INFRASTRUCTURE INVESTMENT                   │
├─────────────────────────────────────────────┬───────────────────────────────┤
│ Package Component                           │ Estimated Subtotal (USD)      │
├─────────────────────────────────────────────┼───────────────────────────────┤
│ 1. Edge AI Appliance PC (i5, RX 9060 XT 16G)│ $833 – $953                   │
│ 2. Router & 48-Port PoE+ Switch (384W)      │ $645 – $710                   │
│ 3. Battery Protection (1500VA Smart UPS)    │ $460 – $620                   │
│ 4. Server Rack, Patch Panels & Cabling      │ $391 – $484                   │
├─────────────────────────────────────────────┼───────────────────────────────┤
│ 🌟 TOTAL INFRASTRUCTURE (Excl. Cameras)     │ $2,329 – $2,767 USD           │
├─────────────────────────────────────────────┼───────────────────────────────┤
│ 5. 32x 4MP Dahua Cameras (If Procuring New) │ $2,400 – $2,720 USD (Optional)│
├─────────────────────────────────────────────┼───────────────────────────────┤
│ 🏆 COMPLETE ALL-IN TURNKEY SYSTEM           │ $4,729 – $5,487 USD           │
└─────────────────────────────────────────────┴───────────────────────────────┘
```
