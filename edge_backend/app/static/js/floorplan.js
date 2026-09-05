/**
 * 2D Interactive Supermarket Blueprint & Heatmap HUD Engine
 * HTML5 Canvas & Layered SVG Renderer with Camera FOV Cones & Drag-and-Drop Placement
 */

class StoreFloorplanHUD {
  constructor(canvasId) {
    this.canvas = document.getElementById(canvasId);
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext('2d');
    
    // Virtual World Dimensions
    this.worldWidth = 1200;
    this.worldHeight = 750;
    
    // Viewport Transform
    this.scale = 1.0;
    this.offsetX = 0;
    this.offsetY = 0;
    this.isDragging = false;
    this.dragStartX = 0;
    this.dragStartY = 0;
    
    // Camera Drag & Placement State
    this.cameras = [];
    this.draggedCamera = null;
    this.isDraggingCamera = false;
    this.hoveredCamera = null;
    this.isAddingCamera = false;
    this.showCameras = true;
    
    // Layer Toggles
    this.showHeatmap = true;
    this.showShoppers = true;
    this.showFlowVectors = true;
    this.heatmapIntensity = 1.0;
    
    // Selection State
    this.selectedZoneId = 'zone_produce';
    this.hoveredZoneId = null;
    
    // Store Zones Definitions (Bounding Boxes in Virtual World Coordinates)
    this.zones = [
      { id: 'zone_entrance', name: 'Entrance & Turnstiles', cat: 'Entrance', x: 40, y: 560, w: 140, h: 140, color: '#00f0ff', dwell: '14s', footfall: 284, conv: '92%', phi: 0.04, stock: 'N/A', cams: ['CAM-01', 'CAM-02', 'CAM-03'] },
      { id: 'zone_produce', name: 'Fresh Produce & Organics', cat: 'Fresh', x: 220, y: 460, w: 200, h: 220, color: '#00ff9d', dwell: '78s', footfall: 310, conv: '64%', phi: 0.38, stock: '420 u/d', cams: ['CAM-17', 'CAM-18'] },
      { id: 'zone_bakery', name: 'Artisan Bakery', cat: 'Fresh', x: 450, y: 560, w: 180, h: 140, color: '#ffaa00', dwell: '62s', footfall: 275, conv: '58%', phi: 0.28, stock: '240 u/d', cams: ['CAM-19'] },
      { id: 'zone_deli', name: 'Deli & Fresh Meats', cat: 'Fresh', x: 660, y: 560, w: 200, h: 140, color: '#ff0055', dwell: '95s', footfall: 220, conv: '49%', phi: 0.41, stock: '180 u/d', cams: ['CAM-20'] },
      
      // Aisles 1 to 12
      { id: 'zone_aisle_01', name: 'Aisle 1: Cereals & Spreads', cat: 'Grocery', x: 220, y: 280, w: 60, h: 150, color: '#00f0ff', dwell: '38s', footfall: 162, conv: '42%', phi: 0.22, stock: '84 u/d', cams: ['CAM-05'] },
      { id: 'zone_aisle_02', name: 'Aisle 2: Coffee & Drinks', cat: 'Grocery', x: 310, y: 280, w: 60, h: 150, color: '#00f0ff', dwell: '52s', footfall: 210, conv: '48%', phi: 0.35, stock: '142 u/d', cams: ['CAM-06'] },
      { id: 'zone_aisle_03', name: 'Aisle 3: Snacks & Chips', cat: 'Grocery', x: 400, y: 280, w: 60, h: 150, color: '#ffaa00', dwell: '64s', footfall: 245, conv: '14%', phi: 0.65, stock: '198 u/d', cams: ['CAM-07'] },
      { id: 'zone_aisle_04', name: 'Aisle 4: Canned & Pasta', cat: 'Grocery', x: 490, y: 280, w: 60, h: 150, color: '#00f0ff', dwell: '41s', footfall: 178, conv: '39%', phi: 0.18, stock: '112 u/d', cams: ['CAM-08'] },
      { id: 'zone_aisle_05', name: 'Aisle 5: Asian & Global', cat: 'Grocery', x: 580, y: 280, w: 60, h: 150, color: '#00f0ff', dwell: '48s', footfall: 140, conv: '36%', phi: 0.29, stock: '95 u/d', cams: ['CAM-09'] },
      { id: 'zone_aisle_06', name: 'Aisle 6: Oils & Spices', cat: 'Grocery', x: 670, y: 280, w: 60, h: 150, color: '#00f0ff', dwell: '34s', footfall: 115, conv: '31%', phi: 0.15, stock: '78 u/d', cams: ['CAM-10'] },
      
      { id: 'zone_aisle_07', name: 'Aisle 7: Cleaning', cat: 'Household', x: 220, y: 100, w: 60, h: 150, color: '#9d4edd', dwell: '29s', footfall: 98, conv: '28%', phi: 0.12, stock: '65 u/d', cams: ['CAM-11'] },
      { id: 'zone_aisle_08', name: 'Aisle 8: Paper & Pets', cat: 'Household', x: 310, y: 100, w: 60, h: 150, color: '#9d4edd', dwell: '33s', footfall: 122, conv: '35%', phi: 0.19, stock: '105 u/d', cams: ['CAM-12'] },
      { id: 'zone_aisle_09', name: 'Aisle 9: Health & Beauty', cat: 'Personal', x: 400, y: 100, w: 60, h: 150, color: '#ffaa00', dwell: '59s', footfall: 135, conv: '24%', phi: 0.48, stock: '88 u/d', cams: ['CAM-13'] },
      { id: 'zone_aisle_10', name: 'Aisle 10: Baby Care', cat: 'Personal', x: 490, y: 100, w: 60, h: 150, color: '#9d4edd', dwell: '46s', footfall: 85, conv: '33%', phi: 0.25, stock: '70 u/d', cams: ['CAM-14'] },
      { id: 'zone_aisle_11', name: 'Aisle 11: Frozen Foods', cat: 'Frozen', x: 580, y: 100, w: 60, h: 150, color: '#00f0ff', dwell: '42s', footfall: 190, conv: '44%', phi: 0.21, stock: '155 u/d', cams: ['CAM-15'] },
      { id: 'zone_aisle_12', name: 'Aisle 12: Chilled Dairy', cat: 'Chilled', x: 670, y: 100, w: 60, h: 150, color: '#00ff9d', dwell: '50s', footfall: 260, conv: '79%', phi: 0.31, stock: '310 u/d', cams: ['CAM-16'] },

      // High-Value Liquor Section
      { id: 'zone_liquor', name: 'Premium Spirits Cabinet', cat: 'Liquor', x: 770, y: 100, w: 100, h: 180, color: '#ff0055', dwell: '110s', footfall: 95, conv: '18%', phi: 0.52, stock: '65 u/d', cams: ['CAM-27'] },

      // POS Checkouts (Registers 1-6)
      { id: 'zone_pos', name: 'Checkouts & POS Lanes 1-6', cat: 'POS', x: 910, y: 100, w: 250, h: 420, color: '#00f0ff', dwell: '145s', footfall: 290, conv: '100%', phi: 0.08, stock: 'N/A', cams: ['CAM-21', 'CAM-22', 'CAM-23', 'CAM-24', 'CAM-25', 'CAM-26'] },

      // Stockroom & Dock
      { id: 'zone_dock', name: 'Stockroom & Loading Dock', cat: 'Restricted', x: 910, y: 550, w: 250, h: 150, color: '#8b949e', dwell: '240s', footfall: 18, conv: '0%', phi: 0.02, stock: 'N/A', cams: ['CAM-28'] }
    ];

    // Dynamic Shoppers Particles
    this.shoppers = this.initShoppers(28);

    // Heatmap Offscreen Canvas
    this.heatCanvas = document.createElement('canvas');
    this.heatCtx = this.heatCanvas.getContext('2d');

    this.initEvents();
    this.resizeCanvas();
    this.fetchFloorplanCameras();
    this.startRenderLoop();
    this.selectZone('zone_produce');
  }

  async fetchFloorplanCameras() {
    try {
      const res = await fetch('/api/v1/analytics/floorplan');
      if (!res.ok) return;
      const data = await res.json();
      this.cameras = data.cameras || [];
    } catch (e) {
      console.warn('Failed to load floorplan cameras:', e);
    }
  }

  initShoppers(count) {
    const list = [];
    const waypoints = [
      { x: 100, y: 620 }, { x: 300, y: 550 }, { x: 500, y: 620 },
      { x: 250, y: 350 }, { x: 340, y: 350 }, { x: 430, y: 350 },
      { x: 520, y: 350 }, { x: 610, y: 350 }, { x: 700, y: 350 },
      { x: 250, y: 180 }, { x: 430, y: 180 }, { x: 700, y: 180 },
      { x: 800, y: 200 }, { x: 980, y: 250 }, { x: 980, y: 400 }
    ];

    for (let i = 0; i < count; i++) {
      const wp = waypoints[i % waypoints.length];
      list.push({
        id: `sh_${i}`,
        x: wp.x + (Math.random() - 0.5) * 40,
        y: wp.y + (Math.random() - 0.5) * 40,
        vx: (Math.random() - 0.5) * 1.2,
        vy: (Math.random() - 0.5) * 1.2,
        targetWp: Math.floor(Math.random() * waypoints.length),
        dwellTimer: Math.random() * 80,
        isDwelling: Math.random() > 0.6
      });
    }
    return list;
  }

  initEvents() {
    window.addEventListener('resize', () => this.resizeCanvas());

    // Mouse & Pointer events for Pan/Zoom/Click/Drag
    this.canvas.addEventListener('mousedown', (e) => {
      const pt = this.screenToWorld(e.clientX, e.clientY);

      // Handle Add Camera click mode
      if (this.isAddingCamera) {
        this.createNewCameraAt(pt.x, pt.y);
        this.isAddingCamera = false;
        const addBtn = document.getElementById('btnAddCameraTool');
        if (addBtn) addBtn.classList.remove('btn-primary');
        return;
      }

      // Check if clicking directly on a camera icon for dragging
      const hitCam = this.cameras.find(c => {
        const dx = pt.x - (c.floor_x || 100);
        const dy = pt.y - (c.floor_y || 100);
        return Math.sqrt(dx * dx + dy * dy) <= 18;
      });

      if (hitCam) {
        this.draggedCamera = hitCam;
        this.isDraggingCamera = true;
      } else {
        this.isDragging = true;
        this.dragStartX = e.clientX - this.offsetX;
        this.dragStartY = e.clientY - this.offsetY;
      }
    });

    window.addEventListener('mousemove', (e) => {
      const pt = this.screenToWorld(e.clientX, e.clientY);

      if (this.isDraggingCamera && this.draggedCamera) {
        this.draggedCamera.floor_x = Math.round(pt.x);
        this.draggedCamera.floor_y = Math.round(pt.y);
      } else if (this.isDragging) {
        this.offsetX = e.clientX - this.dragStartX;
        this.offsetY = e.clientY - this.dragStartY;
      }

      this.checkHover(e);
    });

    window.addEventListener('mouseup', async () => {
      if (this.isDraggingCamera && this.draggedCamera) {
        const cam = this.draggedCamera;
        this.isDraggingCamera = false;
        this.draggedCamera = null;

        // Persist new position via PATCH API
        try {
          await fetch(`/api/v1/cameras/${cam.camera_id}/position`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              floor_x: cam.floor_x,
              floor_y: cam.floor_y,
              azimuth_deg: cam.azimuth_deg || 0.0
            })
          });
          if (typeof showToast === 'function') {
            showToast(`📍 Repositioned ${cam.name} -> (${cam.floor_x}, ${cam.floor_y})`);
          }
        } catch (err) {
          console.error('Failed to update camera position:', err);
        }
      }
      this.isDragging = false;
    });

    // Double-click to configure camera
    this.canvas.addEventListener('dblclick', (e) => {
      const pt = this.screenToWorld(e.clientX, e.clientY);
      const hitCam = this.cameras.find(c => {
        const dx = pt.x - (c.floor_x || 100);
        const dy = pt.y - (c.floor_y || 100);
        return Math.sqrt(dx * dx + dy * dy) <= 20;
      });

      if (hitCam) {
        if (typeof openCameraConfigModal === 'function') {
          openCameraConfigModal(hitCam.camera_id);
        }
      }
    });

    this.canvas.addEventListener('click', (e) => {
      if (this.isDraggingCamera) return;
      const pt = this.screenToWorld(e.clientX, e.clientY);

      // Check zone selection
      const clickedZone = this.zones.find(z => 
        pt.x >= z.x && pt.x <= z.x + z.w && pt.y >= z.y && pt.y <= z.y + z.h
      );
      if (clickedZone) {
        this.selectZone(clickedZone.id);
      }
    });

    // Touch Support for Mobile
    this.canvas.addEventListener('touchstart', (e) => {
      if (e.touches.length === 1) {
        const pt = this.screenToWorld(e.touches[0].clientX, e.touches[0].clientY);
        const hitCam = this.cameras.find(c => {
          const dx = pt.x - (c.floor_x || 100);
          const dy = pt.y - (c.floor_y || 100);
          return Math.sqrt(dx * dx + dy * dy) <= 24;
        });

        if (hitCam) {
          this.draggedCamera = hitCam;
          this.isDraggingCamera = true;
        } else {
          this.isDragging = true;
          this.dragStartX = e.touches[0].clientX - this.offsetX;
          this.dragStartY = e.touches[0].clientY - this.offsetY;
        }
      }
    }, { passive: true });

    this.canvas.addEventListener('touchmove', (e) => {
      if (e.touches.length === 1) {
        const pt = this.screenToWorld(e.touches[0].clientX, e.touches[0].clientY);
        if (this.isDraggingCamera && this.draggedCamera) {
          this.draggedCamera.floor_x = Math.round(pt.x);
          this.draggedCamera.floor_y = Math.round(pt.y);
        } else if (this.isDragging) {
          this.offsetX = e.touches[0].clientX - this.dragStartX;
          this.offsetY = e.touches[0].clientY - this.dragStartY;
        }
      }
    }, { passive: true });

    this.canvas.addEventListener('touchend', async (e) => {
      if (this.isDraggingCamera && this.draggedCamera) {
        const cam = this.draggedCamera;
        this.isDraggingCamera = false;
        this.draggedCamera = null;
        try {
          await fetch(`/api/v1/cameras/${cam.camera_id}/position`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              floor_x: cam.floor_x,
              floor_y: cam.floor_y,
              azimuth_deg: cam.azimuth_deg || 0.0
            })
          });
          if (typeof showToast === 'function') {
            showToast(`📍 Repositioned ${cam.name}`);
          }
        } catch (err) {}
      }
      this.isDragging = false;
    });
  }

  async createNewCameraAt(worldX, worldY) {
    const channelNum = this.cameras.length + 1;
    const newId = `cam_custom_${channelNum}`;
    const newCam = {
      id: newId,
      name: `CAM-${channelNum < 10 ? '0' + channelNum : channelNum}: New Zone Camera`,
      location: 'Store Floor',
      channel_number: channelNum,
      department: 'GENERAL',
      rtsp_url: `rtsp://admin:Pearcedale3912@192.168.20.160:554/cam/realmonitor?channel=${channelNum}&subtype=1`,
      webrtc_url: `http://localhost:8000/api/v1/webrtc/offer?camera_id=${newId}`,
      status: 'ONLINE',
      fps: 25,
      resolution: '1920x1080',
      floor_x: Math.round(worldX),
      floor_y: Math.round(worldY),
      height_z: 3.2,
      azimuth_deg: 0.0,
      fov_deg: 85.0,
      is_ai_enabled: true,
      features: {
        dwell_tracking: true,
        shelf_interaction: true,
        theft_detection: true,
        fall_detection: true,
        queue_monitoring: false
      }
    };

    try {
      const res = await fetch('/api/v1/cameras', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newCam)
      });
      if (res.ok) {
        if (typeof showToast === 'function') {
          showToast(`➕ Added Camera ${newCam.name}`);
        }
        await this.fetchFloorplanCameras();
        if (typeof openCameraConfigModal === 'function') {
          openCameraConfigModal(newId);
        }
      }
    } catch (e) {
      console.error('Failed to create camera:', e);
    }
  }

  resizeCanvas() {
    if (!this.canvas) return;
    const parent = this.canvas.parentElement;
    this.canvas.width = parent.clientWidth;
    this.canvas.height = parent.clientHeight;
    
    this.heatCanvas.width = this.canvas.width;
    this.heatCanvas.height = this.canvas.height;
    
    const scaleX = this.canvas.width / this.worldWidth;
    const scaleY = this.canvas.height / this.worldHeight;
    this.scale = Math.min(scaleX, scaleY) * 0.95;
    this.offsetX = (this.canvas.width - this.worldWidth * this.scale) / 2;
    this.offsetY = (this.canvas.height - this.worldHeight * this.scale) / 2;
  }

  screenToWorld(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    const sx = clientX - rect.left;
    const sy = clientY - rect.top;
    return {
      x: (sx - this.offsetX) / this.scale,
      y: (sy - this.offsetY) / this.scale
    };
  }

  checkHover(e) {
    const pt = this.screenToWorld(e.clientX, e.clientY);

    // Check camera hover
    const foundCam = this.cameras.find(c => {
      const dx = pt.x - (c.floor_x || 100);
      const dy = pt.y - (c.floor_y || 100);
      return Math.sqrt(dx * dx + dy * dy) <= 18;
    });
    this.hoveredCamera = foundCam ? foundCam.camera_id : null;

    // Check zone hover
    const foundZone = this.zones.find(z => 
      pt.x >= z.x && pt.x <= z.x + z.w && pt.y >= z.y && pt.y <= z.y + z.h
    );
    this.hoveredZoneId = foundZone ? foundZone.id : null;

    if (this.isAddingCamera) {
      this.canvas.style.cursor = 'copy';
    } else if (foundCam) {
      this.canvas.style.cursor = 'grab';
    } else if (foundZone) {
      this.canvas.style.cursor = 'pointer';
    } else {
      this.canvas.style.cursor = this.isDragging ? 'grabbing' : 'crosshair';
    }
  }

  selectZone(zoneId) {
    this.selectedZoneId = zoneId;
    const zone = this.zones.find(z => z.id === zoneId);
    if (!zone) return;

    const nameEl = document.getElementById('zoneInspectorName');
    const catEl = document.getElementById('zoneInspectorCat');
    const dwellEl = document.getElementById('zoneInspectorDwell');
    const footfallEl = document.getElementById('zoneInspectorFootfall');
    const convEl = document.getElementById('zoneInspectorConv');
    const phiEl = document.getElementById('zoneInspectorPhi');
    const stockEl = document.getElementById('zoneInspectorStock');
    const camsEl = document.getElementById('zoneInspectorCams');

    if (nameEl) nameEl.textContent = zone.name;
    if (catEl) catEl.textContent = zone.cat.toUpperCase();
    if (dwellEl) dwellEl.textContent = zone.dwell;
    if (footfallEl) footfallEl.textContent = `${zone.footfall} / hr`;
    if (convEl) convEl.textContent = zone.conv;
    if (phiEl) {
      phiEl.textContent = `ϕ ${zone.phi}`;
      phiEl.style.color = zone.phi > 0.5 ? 'var(--accent-red)' : (zone.phi > 0.3 ? 'var(--accent-orange)' : 'var(--accent-green)');
    }
    if (stockEl) stockEl.textContent = zone.stock;
    if (camsEl) {
      camsEl.innerHTML = zone.cams.map(c => `<span class="badge" style="font-size:10px;">${c}</span>`).join(' ');
    }
  }

  updateShoppers() {
    this.shoppers.forEach(s => {
      if (s.isDwelling) {
        s.dwellTimer -= 0.5;
        if (s.dwellTimer <= 0) {
          s.isDwelling = false;
          s.dwellTimer = 40 + Math.random() * 60;
          s.vx = (Math.random() - 0.5) * 1.5;
          s.vy = (Math.random() - 0.5) * 1.5;
        }
      } else {
        s.x += s.vx;
        s.y += s.vy;

        if (s.x < 60 || s.x > this.worldWidth - 60) s.vx *= -1;
        if (s.y < 80 || s.y > this.worldHeight - 60) s.vy *= -1;

        if (Math.random() < 0.015) {
          s.isDwelling = true;
        }
      }
    });
  }

  render() {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

    ctx.save();
    ctx.translate(this.offsetX, this.offsetY);
    ctx.scale(this.scale, this.scale);

    // 1. Draw Architectural Grid & Outer Store Boundary
    this.drawStoreBoundary(ctx);

    // 2. Draw Floorplan Fixtures & Shelf Polygons
    this.drawZones(ctx);

    // 3. Draw Heatmap Layer
    if (this.showHeatmap) {
      this.drawHeatmap(ctx);
    }

    // 4. Draw Flow Vectors
    if (this.showFlowVectors) {
      this.drawFlowVectors(ctx);
    }

    // 5. Draw Dynamic Customer Dots
    if (this.showShoppers) {
      this.drawShoppers(ctx);
    }

    // 6. Draw Directional Camera FOV Cones & Icons
    if (this.showCameras) {
      this.drawCamerasAndFov(ctx);
    }

    ctx.restore();
  }

  drawStoreBoundary(ctx) {
    ctx.strokeStyle = 'rgba(0, 240, 255, 0.4)';
    ctx.lineWidth = 3;
    ctx.strokeRect(20, 40, this.worldWidth - 40, this.worldHeight - 60);

    ctx.strokeStyle = 'rgba(255, 255, 255, 0.03)';
    ctx.lineWidth = 1;
    for (let x = 20; x < this.worldWidth; x += 40) {
      ctx.beginPath(); ctx.moveTo(x, 40); ctx.lineTo(x, this.worldHeight - 20); ctx.stroke();
    }
    for (let y = 40; y < this.worldHeight; y += 40) {
      ctx.beginPath(); ctx.moveTo(20, y); ctx.lineTo(this.worldWidth - 20, y); ctx.stroke();
    }

    ctx.fillStyle = 'rgba(0, 240, 255, 0.6)';
    ctx.font = 'bold 13px "JetBrains Mono"';
    ctx.fillText('PEARCEDALE SUPERMARKET (VIC 3912) - 2D EDGE HUD BLUEPRINT', 30, 28);
  }

  drawZones(ctx) {
    this.zones.forEach(z => {
      const isSelected = z.id === this.selectedZoneId;
      const isHovered = z.id === this.hoveredZoneId;

      ctx.fillStyle = isSelected ? 'rgba(0, 240, 255, 0.25)' : (isHovered ? 'rgba(255, 255, 255, 0.12)' : 'rgba(16, 24, 38, 0.75)');
      ctx.fillRect(z.x, z.y, z.w, z.h);

      ctx.strokeStyle = isSelected ? 'var(--accent-cyan)' : (isHovered ? '#ffffff' : z.color);
      ctx.lineWidth = isSelected ? 2.5 : 1.5;
      ctx.strokeRect(z.x, z.y, z.w, z.h);

      ctx.fillStyle = isSelected ? '#ffffff' : 'rgba(240, 246, 252, 0.85)';
      ctx.font = 'bold 10.5px "Plus Jakarta Sans"';
      ctx.fillText(z.name, z.x + 6, z.y + 16, z.w - 12);

      ctx.fillStyle = 'rgba(139, 148, 158, 0.9)';
      ctx.font = '9px "JetBrains Mono"';
      ctx.fillText(`Dwell: ${z.dwell} | ϕ: ${z.phi}`, z.x + 6, z.y + z.h - 8, z.w - 12);
    });
  }

  drawHeatmap(ctx) {
    const intensity = this.heatmapIntensity;
    this.shoppers.forEach(s => {
      const grad = ctx.createRadialGradient(s.x, s.y, 4, s.x, s.y, 65 * intensity);
      if (s.isDwelling) {
        grad.addColorStop(0, 'rgba(255, 0, 85, 0.45)');
        grad.addColorStop(0.5, 'rgba(255, 170, 0, 0.25)');
        grad.addColorStop(1, 'rgba(0, 240, 255, 0)');
      } else {
        grad.addColorStop(0, 'rgba(0, 255, 157, 0.35)');
        grad.addColorStop(0.6, 'rgba(0, 240, 255, 0.15)');
        grad.addColorStop(1, 'rgba(0, 240, 255, 0)');
      }
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(s.x, s.y, 65 * intensity, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  drawFlowVectors(ctx) {
    ctx.strokeStyle = 'rgba(0, 240, 255, 0.25)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 4]);

    const vectors = [
      { from: { x: 110, y: 580 }, to: { x: 280, y: 520 } },
      { from: { x: 380, y: 520 }, to: { x: 500, y: 580 } },
      { from: { x: 350, y: 440 }, to: { x: 430, y: 290 } },
      { from: { x: 430, y: 270 }, to: { x: 430, y: 120 } },
      { from: { x: 610, y: 280 }, to: { x: 700, y: 120 } },
      { from: { x: 700, y: 280 }, to: { x: 910, y: 220 } },
      { from: { x: 770, y: 580 }, to: { x: 910, y: 400 } }
    ];

    vectors.forEach(v => {
      ctx.beginPath();
      ctx.moveTo(v.from.x, v.from.y);
      ctx.lineTo(v.from.y, v.to.y);
      ctx.stroke();
    });
    ctx.setLineDash([]);
  }

  drawShoppers(ctx) {
    this.shoppers.forEach((s) => {
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.isDwelling ? 6 : 5, 0, Math.PI * 2);
      ctx.fillStyle = s.isDwelling ? '#ffaa00' : '#00ff9d';
      ctx.fill();
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 1.5;
      ctx.stroke();

      if (!s.isDwelling) {
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.6)';
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.lineTo(s.x + s.vx * 8, s.y + s.vy * 8);
        ctx.stroke();
      }
    });
  }

  drawCamerasAndFov(ctx) {
    this.cameras.forEach(cam => {
      const cx = cam.floor_x || 100;
      const cy = cam.floor_y || 100;
      const azimuth = (cam.azimuth_deg || 0) * (Math.PI / 180);
      const fov = (cam.fov_deg || 85) * (Math.PI / 180);
      const isHovered = this.hoveredCamera === cam.camera_id;
      const radius = isHovered ? 85 : 70;

      // 1. Draw Directional FOV Cone
      const startAngle = azimuth - fov / 2;
      const endAngle = azimuth + fov / 2;

      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, radius, startAngle, endAngle);
      ctx.closePath();

      const fovGrad = ctx.createRadialGradient(cx, cy, 5, cx, cy, radius);
      fovGrad.addColorStop(0, isHovered ? 'rgba(0, 240, 255, 0.35)' : 'rgba(0, 240, 255, 0.18)');
      fovGrad.addColorStop(1, 'rgba(0, 240, 255, 0.01)');
      ctx.fillStyle = fovGrad;
      ctx.fill();

      ctx.strokeStyle = isHovered ? '#00f0ff' : 'rgba(0, 240, 255, 0.45)';
      ctx.lineWidth = isHovered ? 1.8 : 1.0;
      ctx.stroke();

      // 2. Draw Centerline Aim
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(azimuth) * radius, cy + Math.sin(azimuth) * radius);
      ctx.strokeStyle = 'rgba(0, 255, 157, 0.6)';
      ctx.lineWidth = 1.2;
      ctx.stroke();

      // 3. Draw Camera Mount Base Icon
      ctx.beginPath();
      ctx.arc(cx, cy, isHovered ? 9 : 7, 0, Math.PI * 2);
      ctx.fillStyle = isHovered ? '#ffaa00' : '#00f0ff';
      ctx.fill();
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 2;
      ctx.stroke();

      // 4. Camera Label Tag
      ctx.fillStyle = 'rgba(10, 16, 28, 0.85)';
      ctx.fillRect(cx - 24, cy - 22, 48, 14);
      ctx.strokeStyle = 'rgba(0, 240, 255, 0.4)';
      ctx.lineWidth = 1;
      ctx.strokeRect(cx - 24, cy - 22, 48, 14);

      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold 9px "JetBrains Mono"';
      ctx.textAlign = 'center';
      const label = cam.name.split(':')[0] || `CAM-${cam.channel_number || '01'}`;
      ctx.fillText(label, cx, cy - 12);
      ctx.textAlign = 'start';
    });
  }

  startRenderLoop() {
    const loop = () => {
      this.updateShoppers();
      this.render();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  toggleLayer(layerName, isEnabled) {
    if (layerName === 'heatmap') this.showHeatmap = isEnabled;
    if (layerName === 'shoppers') this.showShoppers = isEnabled;
    if (layerName === 'flow') this.showFlowVectors = isEnabled;
    if (layerName === 'cameras') this.showCameras = isEnabled;
  }

  setIntensity(val) {
    this.heatmapIntensity = parseFloat(val);
  }

  zoom(factor) {
    this.scale = Math.max(0.4, Math.min(2.5, this.scale * factor));
  }

  resetView() {
    this.resizeCanvas();
  }

  toggleAddCameraMode() {
    this.isAddingCamera = !this.isAddingCamera;
    const addBtn = document.getElementById('btnAddCameraTool');
    if (addBtn) {
      if (this.isAddingCamera) {
        addBtn.classList.add('btn-primary');
        if (typeof showToast === 'function') {
          showToast('🎯 Click anywhere on the floorplan to place a new camera.');
        }
      } else {
        addBtn.classList.remove('btn-primary');
      }
    }
  }
}

// Instantiate and expose globally
window.storeFloorplanHUD = null;
document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('floorplanCanvas')) {
    window.storeFloorplanHUD = new StoreFloorplanHUD('floorplanCanvas');
  }
});
