/**
 * CCTV Physical Display Monitoring - Standalone Frontend Application
 * Framework-agnostic client for real-time video, telemetry, alerts, identity management, and system configuration.
 *
 * Webcam Architecture:
 *   1. Browser opens webcam via getUserMedia()
 *   2. Every 100ms, a frame is captured from <video> onto an offscreen canvas
 *   3. The JPEG is sent as binary over WebSocket to /ws/stream
 *   4. Server returns JSON detections; browser draws bounding boxes on #detection-canvas
 */

// Global client state
let currentViewMode = 'original';
let audioEnabled = true;
let audioCtx = null;
let activeTagPerson = null;
let activeEventFilter = 'all';
let currentConfig = null;
let currentPipelineMode = 'CCTV_ONLY';  // CCTV_ONLY (Default) | ROOM_ONLY | AUTO

// WebSocket & webcam state
let _ws = null;
let _wsReconnectTimer = null;
let _webcamStream = null;
let _captureInterval = null;
let _offscreenCanvas = null;
let _offscreenCtx = null;
let _lastDetectedPersons = [];
let _lastPanes = [];
let _lastMode = 'CCTV_WALL_MONITOR';
let _awaitingResponse = false;    // flow control: true while waiting for server response
let _lastDetectionTime = 0;       // timestamp of last detection response (for stale expiry)

// Multi-Pane Split Display View Mode State (Enabled only after grid is finalized & stabilized)
let displayViewMode = 'COMPOSITE_WALL'; // 'COMPOSITE_WALL' (Default before finalization) | 'SPLIT_GRID'
let soloFocusedPaneId = null;
let maximizedPaneId = null;
let minimizedPanes = new Set();
let isGridLayoutFinalized = false;
let _currentPaneUnknownPersons = {};
let _currentPanePersons = {};
let _cachedLastPanes = [];
let _cachedLastPersons = [];
let _cachedLastMode = 'CCTV_WALL_MONITOR';

function toggleDisplayViewMode() {
  if (!isGridLayoutFinalized) {
    showToast('⚠️ Split view is available only after finalising the grid layout.');
    return;
  }
  displayViewMode = displayViewMode === 'SPLIT_GRID' ? 'COMPOSITE_WALL' : 'SPLIT_GRID';
  soloFocusedPaneId = null;
  maximizedPaneId = null;
  syncDisplayViewModeDOM();
  if (displayViewMode === 'SPLIT_GRID') {
    renderSplitGrid(_cachedLastPanes, _cachedLastPersons);
  } else {
    drawDetections(_cachedLastPersons, _cachedLastPanes, _cachedLastMode);
  }
  showToast(displayViewMode === 'SPLIT_GRID' ? '🪟 View: Multi-Pane Split Grid' : '📺 View: Full Wall Composite');
}

function syncDisplayViewModeDOM() {
  const btn = document.getElementById('btn-display-split-toggle');
  const splitContainer = document.getElementById('split-grid-container');
  const compositeCard = document.getElementById('composite-video-card');
  const soloBanner = document.getElementById('solo-pane-banner');

  // Do not show split mode button or option until user finalises the grid layout and panes are fixed
  if (btn) {
    if (!isGridLayoutFinalized) {
      btn.style.display = 'none';
    } else {
      btn.style.display = 'inline-flex';
      if (displayViewMode === 'SPLIT_GRID') {
        btn.innerHTML = '🪟 View: Split Grid';
        btn.classList.add('btn-cyan');
      } else {
        btn.innerHTML = '📺 View: Full Wall';
        btn.classList.remove('btn-cyan');
      }
    }
  }

  // If not finalized, strictly force COMPOSITE_WALL view
  const effectiveMode = isGridLayoutFinalized ? displayViewMode : 'COMPOSITE_WALL';
  const isFocused = Boolean(soloFocusedPaneId || maximizedPaneId);

  if (effectiveMode === 'SPLIT_GRID') {
    if (splitContainer) splitContainer.style.display = 'grid';
    if (compositeCard) compositeCard.style.display = 'none';
    if (soloBanner) soloBanner.style.display = isFocused ? 'flex' : 'none';
  } else {
    if (splitContainer) splitContainer.style.display = 'none';
    if (compositeCard) compositeCard.style.display = 'block';
    if (soloBanner) soloBanner.style.display = 'none';
  }
}

function focusSoloPane(paneId) {
  toggleMaximizePane(paneId);
}

function exitSoloPane() {
  soloFocusedPaneId = null;
  maximizedPaneId = null;
  syncDisplayViewModeDOM();
  renderSplitGrid(_cachedLastPanes, _cachedLastPersons);
  showToast('🪟 Returned to Multi-Grid View');
}

function toggleMinimizePane(paneId) {
  if (minimizedPanes.has(paneId)) {
    minimizedPanes.delete(paneId);
    showToast(`↩ Restored ${paneId}`);
  } else {
    minimizedPanes.add(paneId);
    if (maximizedPaneId === paneId || soloFocusedPaneId === paneId) {
      maximizedPaneId = null;
      soloFocusedPaneId = null;
    }
    showToast(`− Minimized ${paneId} (background monitoring continues)`);
  }
  syncDisplayViewModeDOM();
  renderSplitGrid(_cachedLastPanes, _cachedLastPersons);
}

function toggleMaximizePane(paneId) {
  if (maximizedPaneId === paneId || soloFocusedPaneId === paneId) {
    maximizedPaneId = null;
    soloFocusedPaneId = null;
    showToast('🪟 Returned to Multi-Grid View');
  } else {
    maximizedPaneId = paneId;
    soloFocusedPaneId = paneId;
    minimizedPanes.delete(paneId);
    const pane = (_cachedLastPanes || []).find(p => p.pane_id === paneId);
    const title = document.getElementById('solo-pane-title');
    if (title) title.innerText = `${paneId} | ${pane ? pane.camera_label : 'CAMERA'}`;
    showToast(`⛶ Maximized ${paneId}`);
  }
  syncDisplayViewModeDOM();
  renderSplitGrid(_cachedLastPanes, _cachedLastPersons);
}

function restorePane(paneId) {
  minimizedPanes.delete(paneId);
  if (maximizedPaneId === paneId || soloFocusedPaneId === paneId) {
    maximizedPaneId = null;
    soloFocusedPaneId = null;
  }
  syncDisplayViewModeDOM();
  renderSplitGrid(_cachedLastPanes, _cachedLastPersons);
  showToast(`↩ Restored ${paneId}`);
}

function openTagModalForPane(paneId, clickEvent = null) {
  const pCanvas = document.getElementById(`pane-canvas-${paneId}`);
  const personsInPane = _currentPanePersons[paneId] || [];
  let targetPerson = null;

  // 1. If user clicked directly on the canvas, check if click hit an existing detected person bounding box
  if (clickEvent && pCanvas && personsInPane.length > 0) {
    const rect = pCanvas.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      const scaleX = pCanvas.width / rect.width;
      const scaleY = pCanvas.height / rect.height;
      const clickX = (clickEvent.clientX - rect.left) * scaleX;
      const clickY = (clickEvent.clientY - rect.top) * scaleY;

      const paneDef = (_cachedLastPanes || []).find(p => p.pane_id === paneId);
      const src = getActiveFeedSource();
      const srcW = (src && src.element) ? (src.element.videoWidth || src.element.naturalWidth || 640) : 640;
      const srcH = (src && src.element) ? (src.element.videoHeight || src.element.naturalHeight || 480) : 480;

      let sx = 0, sy = 0, sw = srcW, sh = srcH;
      if (paneDef) {
        if (paneDef.bbox && paneDef.bbox.length === 4) {
          sx = paneDef.bbox[0]; sy = paneDef.bbox[1]; sw = paneDef.bbox[2]; sh = paneDef.bbox[3];
        } else if (paneDef.norm_bbox && paneDef.norm_bbox.length === 4) {
          sx = paneDef.norm_bbox[0] * srcW; sy = paneDef.norm_bbox[1] * srcH;
          sw = paneDef.norm_bbox[2] * srcW; sh = paneDef.norm_bbox[3] * srcH;
        }
      }

      for (const per of personsInPane) {
        const lx = ((per.local_sx - sx) / sw) * pCanvas.width;
        const ly = ((per.local_sy - sy) / sh) * pCanvas.height;
        const lw = (per.local_sw / sw) * pCanvas.width;
        const lh = (per.local_sh / sh) * pCanvas.height;

        if (clickX >= lx && clickX <= lx + lw && clickY >= ly && clickY <= ly + lh) {
          targetPerson = per;
          break;
        }
      }
    }
  }

  // 2. If user clicked on the canvas but didn't hit a person, do nothing.
  // This prevents "clicking anywhere starts tagging".
  if (clickEvent && !targetPerson) {
    return;
  }

  // Fallbacks ONLY for non-click events (e.g., clicking "Tag Person" button)
  if (!clickEvent) {
    if (!targetPerson) {
      targetPerson = _currentPaneUnknownPersons[paneId] || personsInPane.find(per => !per.is_known);
    }
    if (!targetPerson && personsInPane.length > 0) {
      targetPerson = personsInPane[0];
    }
  }

  // 4. If person is found with an existing snapshot and no clickEvent, open modal directly
  if (targetPerson && targetPerson.snapshot_base64 && !clickEvent) {
    openTagModal(targetPerson);
    return;
  }

  // 5. Direct Canvas Extraction:
  // If we have a targetPerson (via click or fallback), crop a snapshot from their current location.
  if (pCanvas && pCanvas.width > 0 && pCanvas.height > 0 && targetPerson) {
    try {
      let cropX = 0, cropY = 0, cropW = pCanvas.width, cropH = pCanvas.height;

      if (clickEvent) {
        const rect = pCanvas.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
          const scaleX = pCanvas.width / rect.width;
          const scaleY = pCanvas.height / rect.height;
          const clickX = (clickEvent.clientX - rect.left) * scaleX;
          const clickY = (clickEvent.clientY - rect.top) * scaleY;

          const boxSize = Math.min(pCanvas.width, pCanvas.height, 220);
          cropW = boxSize;
          cropH = boxSize;
          cropX = Math.max(0, Math.min(pCanvas.width - cropW, clickX - cropW / 2));
          cropY = Math.max(0, Math.min(pCanvas.height - cropH, clickY - cropH / 2));
        }
      } else if (targetPerson && targetPerson.local_sx !== undefined) {
        const paneDef = (_cachedLastPanes || []).find(p => p.pane_id === paneId);
        const src = getActiveFeedSource();
        const srcW = (src && src.element) ? (src.element.videoWidth || src.element.naturalWidth || 640) : 640;
        const srcH = (src && src.element) ? (src.element.videoHeight || src.element.naturalHeight || 480) : 480;
        let sx = 0, sy = 0, sw = srcW, sh = srcH;
        if (paneDef) {
          if (paneDef.bbox && paneDef.bbox.length === 4) {
            sx = paneDef.bbox[0]; sy = paneDef.bbox[1]; sw = paneDef.bbox[2]; sh = paneDef.bbox[3];
          } else if (paneDef.norm_bbox && paneDef.norm_bbox.length === 4) {
            sx = paneDef.norm_bbox[0] * srcW; sy = paneDef.norm_bbox[1] * srcH;
            sw = paneDef.norm_bbox[2] * srcW; sh = paneDef.norm_bbox[3] * srcH;
          }
        }
        cropX = Math.max(0, ((targetPerson.local_sx - sx) / sw) * pCanvas.width);
        cropY = Math.max(0, ((targetPerson.local_sy - sy) / sh) * pCanvas.height);
        cropW = Math.min(pCanvas.width - cropX, (targetPerson.local_sw / sw) * pCanvas.width);
        cropH = Math.min(pCanvas.height - cropY, (targetPerson.local_sh / sh) * pCanvas.height);
      }

      // Draw crop to offscreen canvas
      const cropCanvas = document.createElement('canvas');
      const outW = 200;
      const outH = 200;
      cropCanvas.width = outW;
      cropCanvas.height = outH;
      const cropCtx = cropCanvas.getContext('2d');
      cropCtx.drawImage(pCanvas, cropX, cropY, cropW, cropH, 0, 0, outW, outH);
      const snapDataUrl = cropCanvas.toDataURL('image/jpeg', 0.92);
      const snapBase64 = snapDataUrl.replace(/^data:image\/[a-z]+;base64,/, '');

      const fallbackPerson = {
        person_id: (targetPerson && targetPerson.person_id) ? targetPerson.person_id : `trk_${paneId}_${Date.now().toString(36)}`,
        person_name: (targetPerson && targetPerson.person_name) ? targetPerson.person_name : 'Unidentified Person',
        is_known: false,
        confidence: (targetPerson && targetPerson.confidence) ? targetPerson.confidence : 0.95,
        bbox: [Math.round(cropX), Math.round(cropY), Math.round(cropW), Math.round(cropH)],
        snapshot_base64: snapBase64,
        pane_id: paneId,
      };

      openTagModal(fallbackPerson);
      return;
    } catch (err) {
      console.warn("Could not extract canvas crop:", err);
    }
  }

  // 6. If targetPerson exists even without canvas crop
  if (targetPerson) {
    openTagModal(targetPerson);
    return;
  }

  showToast(`⚠️ Unable to capture person from ${paneId}. Please make sure video feed is active.`);
}

async function finalizeCurrentGrid() {
  return saveManualGridLayout(true);
}

function generateDefaultPanesFromPreset(preset, w, h) {
  const presetsMap = {
    '1X1': { r: 1, c: 1 },
    '1X2': { r: 1, c: 2 },
    '2X1': { r: 2, c: 1 },
    '2X2': { r: 2, c: 2 },
    '2X3': { r: 2, c: 3 },
    '3X2': { r: 3, c: 2 },
    '3X3': { r: 3, c: 3 },
    '3X4': { r: 3, c: 4 },
    '4X3': { r: 4, c: 3 },
    '4X4': { r: 4, c: 4 },
  };
  const dim = presetsMap[preset] || { r: 2, c: 2 };
  const rows = dim.r;
  const cols = dim.c;
  const list = [];
  let idx = 0;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      idx++;
      const pid = `Pane-${String(idx).padStart(2, '0')}`;
      const nx = c / cols;
      const ny = r / rows;
      const nw = 1 / cols;
      const nh = 1 / rows;
      list.push({
        pane_id: pid,
        index: idx - 1,
        row: r,
        col: c,
        camera_label: `CAM-${String(idx).padStart(2, '0')}`,
        norm_bbox: [nx, ny, nw, nh],
        bbox: [nx * w, ny * h, nw * w, nh * h],
        ocr_confidence: 0.95,
        geometry_confidence: 1.0,
        is_stable: true,
      });
    }
  }
  return list;
}

/**
 * Renders the multi-pane split grid viewport.
 * Dynamically crops the active surveillance feed for each detected/selected pane,
 * scales the cropped video onto each individual card canvas, translates local
 * person detections, and applies live intruder/authorized alert highlights.
 */
function renderSplitGrid(panes, persons) {
  const container = document.getElementById('split-grid-container');
  if (!container) return;

  const src = getActiveFeedSource();
  const srcW = src ? src.width : 1280;
  const srcH = src ? src.height : 720;

  // Use provided panes or fallback to cached panes or generate default 2x2
  let activePanes = (panes && panes.length > 0) ? panes : _cachedLastPanes;
  if (!activePanes || activePanes.length === 0) {
    const presetSelect = document.getElementById('select-grid-preset');
    const curPreset = (presetSelect && presetSelect.value !== 'AUTO' && presetSelect.value !== 'CUSTOM') ? presetSelect.value : '2X2';
    activePanes = generateDefaultPanesFromPreset(curPreset, srcW, srcH);
  }

  _cachedLastPanes = activePanes;
  if (persons) _cachedLastPersons = persons;
  const currentPersons = persons || _cachedLastPersons || [];

  // Filter if maximized / solo pane focus is active
  let displayPanes = activePanes;
  const activeFocusId = maximizedPaneId || soloFocusedPaneId;
  if (activeFocusId) {
    displayPanes = activePanes.filter(p => p.pane_id === activeFocusId);
    if (displayPanes.length === 0) {
      displayPanes = activePanes;
      maximizedPaneId = null;
      soloFocusedPaneId = null;
      syncDisplayViewModeDOM();
    }
  }

  // Calculate dynamic CSS grid layout
  if (activeFocusId) {
    container.style.gridTemplateColumns = '1fr';
  } else {
    let maxCols = 1;
    let maxRows = 1;
    displayPanes.forEach(p => {
      if (p.col !== undefined && p.col + 1 > maxCols) maxCols = p.col + 1;
      if (p.row !== undefined && p.row + 1 > maxRows) maxRows = p.row + 1;
    });
    if (maxCols === 1 && displayPanes.length > 1) {
      if (displayPanes.length <= 2) maxCols = 2;
      else if (displayPanes.length <= 4) maxCols = 2;
      else if (displayPanes.length <= 6) maxCols = 3;
      else if (displayPanes.length <= 9) maxCols = 3;
      else maxCols = 4;
    }
    container.style.gridTemplateColumns = `repeat(${maxCols}, minmax(0, 1fr))`;
  }

  // DOM element reconciliation to eliminate canvas recreation flicker
  const currentCardElements = container.querySelectorAll('.pane-card');
  const targetPaneIds = new Set(displayPanes.map(p => p.pane_id));

  currentCardElements.forEach(card => {
    const pid = card.getAttribute('data-pane-id');
    if (!targetPaneIds.has(pid)) {
      card.remove();
    }
  });

  const now = new Date();
  const timeStr = now.toLocaleTimeString('en-GB') + '.' + String(now.getMilliseconds()).padStart(3, '0');

  displayPanes.forEach((p, idx) => {
    const isMinimized = minimizedPanes.has(p.pane_id);
    const isMaximized = (maximizedPaneId === p.pane_id || soloFocusedPaneId === p.pane_id);

    let card = document.getElementById(`pane-card-${p.pane_id}`);
    if (!card) {
      card = document.createElement('div');
      card.id = `pane-card-${p.pane_id}`;
      card.className = 'pane-card splitting-card';
      card.style.animationDelay = `${idx * 0.06}s`;
      card.setAttribute('data-pane-id', p.pane_id);

      card.innerHTML = `
        <div class="pane-card-header">
          <div class="pane-card-title">
            <span class="pane-live-dot"></span>
            <span class="pane-name-text">${p.pane_id} | ${p.camera_label || `CAM-${String(idx + 1).padStart(2, '0')}`}</span>
            <span class="pane-minimized-summary" style="display: none;"></span>
          </div>
          <div class="pane-card-actions">
            <button class="pane-action-btn btn-tag-pane" title="Tag / Authorize a person in this camera view" onclick="openTagModalForPane('${p.pane_id}')">
              🏷️ Tag Person
            </button>
            <button class="pane-action-btn btn-zone-pane" title="Configure zones & triggers for this camera view" onclick="openZoneEditorModal('${p.pane_id}')">
              🎯 Zones
            </button>
            <button class="pane-action-btn btn-min-toggle" title="Minimize/Restore Pane" onclick="toggleMinimizePane('${p.pane_id}')">
              − Minimize
            </button>
            <button class="pane-action-btn btn-max-toggle" title="Maximize Pane" onclick="toggleMaximizePane('${p.pane_id}')">
              ${isMaximized ? '↩ Restore' : '⛶ Maximize'}
            </button>
          </div>
        </div>
        <div class="pane-card-body">
          <canvas id="pane-canvas-${p.pane_id}" class="pane-canvas" title="Click on a person or anywhere in this camera view to Tag & Authorize" onclick="openTagModalForPane('${p.pane_id}', event)"></canvas>
        </div>
        <div class="pane-info-panel" id="pane-info-${p.pane_id}">
          <div class="pane-info-row">
            <span class="pane-info-label">Location:</span>
            <span class="pane-info-value pane-info-location">${p.camera_label || `CAM-${String(idx + 1).padStart(2, '0')}`}</span>
          </div>
          <div class="pane-info-row">
            <span class="pane-info-label">Person:</span>
            <span class="pane-info-value pane-info-person">Clear</span>
          </div>
          <div class="pane-info-row">
            <span class="pane-info-label">Activity:</span>
            <span class="pane-info-value pane-info-activity">Standing</span>
          </div>
          <div class="pane-info-row">
            <span class="pane-info-label">Last Event:</span>
            <span class="pane-info-value pane-info-event">Monitoring active</span>
          </div>
          <div class="pane-info-row" style="margin-top: 2px;">
            <span class="pane-info-label">Status:</span>
            <span class="pane-info-value pane-info-status" style="display: flex; align-items: center; gap: 6px;">
              <span class="pane-info-status-text">No Alert</span>
              <span class="pane-tag-slot"></span>
            </span>
          </div>
        </div>
        <div class="pane-card-footer">
          <span class="pane-footer-time">${timeStr}</span>
          <span class="pane-footer-badge">● LIVE</span>
        </div>
      `;
      container.appendChild(card);
    }

    // Card minimization class & toggle buttons
    if (isMinimized) {
      card.classList.add('pane-card-minimized');
    } else {
      card.classList.remove('pane-card-minimized');
    }

    const minBtn = card.querySelector('.btn-min-toggle');
    if (minBtn) {
      minBtn.innerText = isMinimized ? '↩ Restore' : '− Minimize';
      minBtn.title = isMinimized ? 'Restore Camera' : 'Minimize Camera';
    }
    const maxBtn = card.querySelector('.btn-max-toggle');
    if (maxBtn) {
      maxBtn.innerText = isMaximized ? '↩ Restore' : '⛶ Maximize';
      maxBtn.title = isMaximized ? 'Exit Maximize' : 'Maximize Camera View';
    }

    // Ensure Tag Person button exists in actions
    const actionsEl = card.querySelector('.pane-card-actions');
    if (actionsEl && !actionsEl.querySelector('.btn-tag-pane')) {
      const tagBtn = document.createElement('button');
      tagBtn.className = 'pane-action-btn btn-tag-pane';
      tagBtn.title = 'Tag / Authorize a person in this camera view';
      tagBtn.innerText = '🏷️ Tag Person';
      tagBtn.onclick = () => openTagModalForPane(p.pane_id);
      actionsEl.prepend(tagBtn);
    }

    // Ensure canvas click listener
    const pCanvasInit = document.getElementById(`pane-canvas-${p.pane_id}`);
    if (pCanvasInit && !pCanvasInit.onclick) {
      pCanvasInit.title = 'Click on a person or anywhere in this camera view to Tag & Authorize';
      pCanvasInit.onclick = (e) => openTagModalForPane(p.pane_id, e);
    }

    // Update title, time and actions
    const titleText = card.querySelector('.pane-name-text');
    if (titleText) {
      titleText.innerText = `${p.pane_id} | ${p.camera_label || `CAM-${String(idx + 1).padStart(2, '0')}`}`;
    }
    const timeEl = card.querySelector('.pane-footer-time');
    if (timeEl) timeEl.innerText = timeStr;
    const badgeEl = card.querySelector('.pane-footer-badge');

    // Determine normalized & pixel crop coordinates from source
    let sx, sy, sw, sh;
    let normX, normY, normW, normH;
    if (p.norm_bbox && p.norm_bbox.length === 4) {
      normX = p.norm_bbox[0];
      normY = p.norm_bbox[1];
      normW = p.norm_bbox[2];
      normH = p.norm_bbox[3];
      sx = normX * srcW;
      sy = normY * srcH;
      sw = normW * srcW;
      sh = normH * srcH;
    } else if (p.bbox && p.bbox.length === 4) {
      sx = p.bbox[0];
      sy = p.bbox[1];
      sw = p.bbox[2];
      sh = p.bbox[3];
      normX = sx / srcW;
      normY = sy / srcH;
      normW = sw / srcW;
      normH = sh / srcH;
    } else {
      sx = 0; sy = 0; sw = srcW; sh = srcH;
      normX = 0; normY = 0; normW = 1; normH = 1;
    }

    // Find persons located inside this camera pane
    const personsInPane = [];
    let hasIntruder = false;
    let hasAuthorized = false;

    currentPersons.forEach(person => {
      let pnormX, pnormY, pnormW, pnormH;
      let ppx, ppy, ppw, pph;
      if (person.norm_bbox && person.norm_bbox.length === 4) {
        pnormX = person.norm_bbox[0];
        pnormY = person.norm_bbox[1];
        pnormW = person.norm_bbox[2];
        pnormH = person.norm_bbox[3];
        ppx = pnormX * srcW;
        ppy = pnormY * srcH;
        ppw = pnormW * srcW;
        pph = pnormH * srcH;
      } else if (person.bbox && person.bbox.length === 4) {
        ppx = person.bbox[0];
        ppy = person.bbox[1];
        ppw = person.bbox[2];
        pph = person.bbox[3];
        pnormX = ppx / srcW;
        pnormY = ppy / srcH;
        pnormW = ppw / srcW;
        pnormH = pph / srcH;
      } else {
        return;
      }

      const pcx = pnormX + pnormW / 2;
      const pcy = pnormY + pnormH / 2;

      // Check if person matches this pane (via explicit pane_id or center coordinates)
      const matchesPane = (person.pane_id && person.pane_id === p.pane_id) ||
                          (pcx >= normX - 0.02 && pcx <= normX + normW + 0.02 && pcy >= normY - 0.02 && pcy <= normY + normH + 0.02);
      if (matchesPane) {
        personsInPane.push({
          ...person,
          local_sx: ppx,
          local_sy: ppy,
          local_sw: ppw,
          local_sh: pph,
        });
        if (!person.is_known) {
          hasIntruder = true;
        } else {
          hasAuthorized = true;
        }
      }
    });

    // Alert border & badge styling
    if (hasIntruder) {
      card.classList.add('intruder-alert');
      card.classList.remove('authorized-present');
      if (badgeEl) {
        badgeEl.innerText = '🚨 INTRUDER';
        badgeEl.style.color = 'var(--accent-red)';
      }
    } else if (hasAuthorized) {
      card.classList.add('authorized-present');
      card.classList.remove('intruder-alert');
      if (badgeEl) {
        badgeEl.innerText = '✓ AUTHORIZED';
        badgeEl.style.color = 'var(--accent-green)';
      }
    } else {
      card.classList.remove('intruder-alert');
      card.classList.remove('authorized-present');
      if (badgeEl) {
        badgeEl.innerText = '● LIVE';
        badgeEl.style.color = 'var(--accent-cyan)';
      }
    }

    // Minimized Mode Header Summary Update
    const minSummaryEl = card.querySelector('.pane-minimized-summary');
    const personSummary = personsInPane.length > 0 
      ? personsInPane.map(per => `${per.person_name || (per.is_known ? 'Authorized' : 'Unknown')}`).join(', ')
      : 'Clear';
    const monitoringStatus = hasIntruder ? '🚨 UNIDENTIFIED' : (hasAuthorized ? '✓ IDENTIFIED' : 'MONITORING');
    if (minSummaryEl) {
      minSummaryEl.innerText = `— ${personSummary} (${monitoringStatus})`;
      minSummaryEl.style.display = isMinimized ? 'inline-block' : 'none';
      if (hasIntruder) minSummaryEl.style.color = 'var(--accent-red)';
      else if (hasAuthorized) minSummaryEl.style.color = 'var(--accent-green)';
      else minSummaryEl.style.color = 'var(--text-muted)';
    }

    // Fill Pane Information Panel
    const locEl = card.querySelector('.pane-info-location');
    if (locEl) locEl.innerText = p.camera_label || `CAM-${String(idx + 1).padStart(2, '0')}`;

    const personEl = card.querySelector('.pane-info-person');
    if (personEl) {
      if (personsInPane.length === 0) {
        personEl.innerText = 'No person detected';
        personEl.style.color = 'var(--text-muted)';
      } else {
        const pnames = personsInPane.map(per => {
          const conf = Math.round((per.confidence || 0.9) * 100);
          return `${per.person_name || 'Unknown'} (${conf}%)`;
        }).join(', ');
        personEl.innerText = pnames;
        personEl.style.color = hasIntruder ? 'var(--accent-red)' : 'var(--accent-green)';
      }
    }

    const actEl = card.querySelector('.pane-info-activity');
    if (actEl) {
      const actStr = p.summary || (personsInPane.length > 0 ? (hasIntruder ? 'Moving' : 'Standing') : 'Clear');
      actEl.innerText = actStr;
    }

    const evtEl = card.querySelector('.pane-info-event');
    if (evtEl) {
      evtEl.innerText = p.last_event || (personsInPane.length > 0 ? `Active tracking (${personsInPane.length} in view)` : 'Monitoring active');
    }

    const statusTextEl = card.querySelector('.pane-info-status-text');
    if (statusTextEl) {
      statusTextEl.innerText = hasIntruder ? '🚨 UNIDENTIFIED' : (hasAuthorized ? '✓ IDENTIFIED' : 'No Alert');
      statusTextEl.style.color = hasIntruder ? 'var(--accent-red)' : (hasAuthorized ? 'var(--accent-green)' : 'var(--text-muted)');
    }

    _currentPanePersons[p.pane_id] = personsInPane;
    const firstUnknown = personsInPane.find(per => !per.is_known);
    if (firstUnknown) {
      _currentPaneUnknownPersons[p.pane_id] = firstUnknown;
    } else {
      delete _currentPaneUnknownPersons[p.pane_id];
    }

    const tagSlot = card.querySelector('.pane-tag-slot');
    if (tagSlot) {
      if (hasIntruder) {
        tagSlot.innerHTML = `<button class="pane-info-tag-btn alert-tag-btn" onclick="openTagModalForPane('${p.pane_id}')" title="🚨 Unidentified person detected! Tag now to authorize">🚨 Tag Intruder</button>`;
      } else {
        tagSlot.innerHTML = `<button class="pane-info-tag-btn" onclick="openTagModalForPane('${p.pane_id}')" title="Tag & Authorize a person in this view">🏷️ Tag Person</button>`;
      }
    }

    // Canvas rendering for this pane's cropped video (skip if minimized)
    if (!isMinimized) {
      const pCanvas = document.getElementById(`pane-canvas-${p.pane_id}`);
      if (pCanvas && src && src.element && sw > 0 && sh > 0) {
        const targetW = Math.max(Math.round(sw), 320);
        const targetH = Math.max(Math.round(sh), 240);
        if (pCanvas.width !== targetW || pCanvas.height !== targetH) {
          pCanvas.width = targetW;
          pCanvas.height = targetH;
        }
        const pctx = pCanvas.getContext('2d');
        pctx.clearRect(0, 0, pCanvas.width, pCanvas.height);

        // 1. Draw sub-image crop (with quad clip if tilted)
        try {
          if (p.corners && p.corners.length === 4 && normW > 0 && normH > 0) {
            pctx.save();
            pctx.beginPath();
            pctx.moveTo(((p.corners[0][0] - normX) / normW) * pCanvas.width, ((p.corners[0][1] - normY) / normH) * pCanvas.height);
            pctx.lineTo(((p.corners[1][0] - normX) / normW) * pCanvas.width, ((p.corners[1][1] - normY) / normH) * pCanvas.height);
            pctx.lineTo(((p.corners[2][0] - normX) / normW) * pCanvas.width, ((p.corners[2][1] - normY) / normH) * pCanvas.height);
            pctx.lineTo(((p.corners[3][0] - normX) / normW) * pCanvas.width, ((p.corners[3][1] - normY) / normH) * pCanvas.height);
            pctx.closePath();
            pctx.clip();
          }
          pctx.drawImage(src.element, sx, sy, sw, sh, 0, 0, pCanvas.width, pCanvas.height);
          if (p.corners && p.corners.length === 4 && normW > 0 && normH > 0) {
            pctx.restore();
          }
        } catch (drawErr) {
          // Feed frame unavailable
        }

        // 2. Draw configured zones for this pane
        const paneZonesList = (window._activeConfigZones && window._activeConfigZones[p.pane_id]) || [];
        paneZonesList.forEach(z => {
          if (z.bbox && z.bbox.length === 4) {
            const zx = z.bbox[0] * pCanvas.width;
            const zy = z.bbox[1] * pCanvas.height;
            const zw = z.bbox[2] * pCanvas.width;
            const zh = z.bbox[3] * pCanvas.height;
            const zTheme = getZoneColor(z.zone_type);

            pctx.strokeStyle = zTheme.stroke;
            pctx.lineWidth = 1.5;
            pctx.setLineDash([4, 4]);
            pctx.strokeRect(zx, zy, zw, zh);
            pctx.setLineDash([]);

            pctx.fillStyle = zTheme.fill;
            pctx.fillRect(zx, zy, zw, zh);

            pctx.font = 'bold 10px "JetBrains Mono", monospace';
            const zTag = `${zTheme.icon} ${z.label || z.zone_type.toUpperCase()}`;
            const zTagW = pctx.measureText(zTag).width;
            pctx.fillStyle = 'rgba(10, 14, 26, 0.85)';
            pctx.fillRect(zx, zy, Math.min(zw, zTagW + 8), 15);
            pctx.fillStyle = zTheme.stroke;
            pctx.fillText(zTag, zx + 3, zy + 11);
          }
        });

        // 3. Draw overlay bounding boxes translated to local pane coordinates
        personsInPane.forEach(per => {
          const lx = ((per.local_sx - sx) / sw) * pCanvas.width;
          const ly = ((per.local_sy - sy) / sh) * pCanvas.height;
          const lw = (per.local_sw / sw) * pCanvas.width;
          const lh = (per.local_sh / sh) * pCanvas.height;

          const isKnown = per.is_known;
          const color = isKnown ? '#00ff88' : '#ff3366';
          const label = isKnown ? `✓ ${per.person_name || 'Authorized'} [${per.tag || 'STAFF'}]` : `⚠ Unknown — ${Math.round((per.confidence || 0.9) * 100)}%`;

          pctx.strokeStyle = color;
          pctx.lineWidth = 2.5;
          pctx.shadowColor = color;
          pctx.shadowBlur = 8;
          pctx.strokeRect(lx, ly, lw, lh);
          pctx.shadowBlur = 0;

          // Label tag badge
          pctx.font = 'bold 12px "JetBrains Mono", monospace';
          const textW = pctx.measureText(label).width;
          const tagY = Math.max(ly - 22, 0);

          pctx.fillStyle = isKnown ? 'rgba(0, 40, 20, 0.88)' : 'rgba(40, 0, 10, 0.88)';
          pctx.fillRect(lx, tagY, textW + 12, 20);
          pctx.strokeStyle = color;
          pctx.lineWidth = 1;
          pctx.strokeRect(lx, tagY, textW + 12, 20);

          pctx.fillStyle = color;
          pctx.fillText(label, lx + 6, tagY + 14);
        });
      }
    }
  });
}

// Feed mode state: 'camera' (CCTV Live Camera Detection) | 'video' (Video Playback)
let _activeCameraDeviceId = null;
let currentFeedMode = 'camera'; // 'camera' | 'video'
let _uploadedVideoFile = null;

function updateFeedSourceBtnUI() {
  const btnCam = document.getElementById('btn-mode-live-camera');
  const btnVid = document.getElementById('btn-mode-video-playback');
  const vidBar = document.getElementById('video-playback-controls-bar');
  if (currentFeedMode === 'camera') {
    if (btnCam) btnCam.classList.add('active');
    if (btnVid) btnVid.classList.remove('active');
    if (vidBar) vidBar.style.display = 'none';
  } else {
    if (btnCam) btnCam.classList.remove('active');
    if (btnVid) btnVid.classList.add('active');
    if (vidBar) vidBar.style.display = 'flex';
  }
}

async function switchToLiveCameraMode() {
  currentFeedMode = 'camera';
  updateFeedSourceBtnUI();

  const video = document.getElementById('live-video');
  const feedCanvas = document.getElementById('feed-canvas');
  if (feedCanvas) feedCanvas.style.display = 'none';

  // Pause file playback if it was active
  if (video && video.src && !video.srcObject) {
    video.pause();
    video.removeAttribute('src');
    video.load();
  }

  showToast('📹 Switching to CCTV Live Camera...');
  await initWebcam(true);
}

function switchToVideoPlaybackMode() {
  currentFeedMode = 'video';
  updateFeedSourceBtnUI();

  // Stop physical webcam stream if active
  if (_webcamStream) {
    _webcamStream.getTracks().forEach(t => t.stop());
    _webcamStream = null;
  }

  const video = document.getElementById('live-video');
  const feedCanvas = document.getElementById('feed-canvas');
  if (feedCanvas) feedCanvas.style.display = 'none';
  if (video) video.style.display = 'block';

  // If a video file was already loaded, resume playing
  if (video && video.src && video.src !== '' && video.src !== location.href) {
    video.play().catch(() => {});
    const statusBanner = document.getElementById('camera-status-banner');
    if (statusBanner) statusBanner.style.display = 'none';
    showToast('📼 Resuming CCTV Video Playback');
    startFrameCapture();
  } else {
    // Prompt user to select a video file
    showToast('📼 Please select a CCTV video file for playback');
    const fileInput = document.getElementById('cctv-video-file-input');
    if (fileInput) fileInput.click();
  }
}

function toggleFeedSource() {
  if (currentFeedMode === 'camera') {
    switchToVideoPlaybackMode();
  } else {
    switchToLiveCameraMode();
  }
}

function toggleVideoPlayPause() {
  const video = document.getElementById('live-video');
  const btn = document.getElementById('btn-video-play-pause');
  if (!video) return;

  if (video.paused) {
    video.play().then(() => {
      if (btn) btn.innerHTML = '⏸ Pause';
    }).catch(e => console.warn('Play error:', e));
  } else {
    video.pause();
    if (btn) btn.innerHTML = '▶ Play';
  }
}

function toggleVideoLoop() {
  const video = document.getElementById('live-video');
  const btn = document.getElementById('btn-video-loop');
  if (!video || !btn) return;

  video.loop = !video.loop;
  if (video.loop) {
    btn.classList.add('active');
    btn.innerHTML = '🔁 Loop: ON';
  } else {
    btn.classList.remove('active');
    btn.innerHTML = '🔁 Loop: OFF';
  }
}

function onVideoSeek(val) {
  const video = document.getElementById('live-video');
  if (!video || isNaN(video.duration) || video.duration <= 0) return;
  video.currentTime = (parseFloat(val) / 100) * video.duration;
  updateVideoTimeDisplay();
}

function updateVideoTimeDisplay() {
  const video = document.getElementById('live-video');
  const timeElem = document.getElementById('video-time-display');
  const slider = document.getElementById('video-seek-slider');
  if (!video || !timeElem) return;

  const cur = formatDuration(video.currentTime || 0);
  const dur = formatDuration(video.duration || 0);
  timeElem.innerText = `${cur} / ${dur}`;

  if (slider && video.duration > 0) {
    slider.value = ((video.currentTime || 0) / video.duration) * 100;
  }
}

function formatDuration(sec) {
  if (isNaN(sec) || sec < 0) return '00:00';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/**
 * Returns the currently active visual frame source (webcam <video>, video playback <video>, or <canvas id="feed-canvas">)
 * along with its current width and height.
 */
function getActiveFeedSource() {
  const feedCanvas = document.getElementById('feed-canvas');
  const video = document.getElementById('live-video');

  if (currentFeedMode === 'video') {
    if (feedCanvas && feedCanvas.style.display !== 'none' && feedCanvas.width > 0 && feedCanvas.height > 0) {
      return { element: feedCanvas, width: feedCanvas.width, height: feedCanvas.height };
    }
    if (video && video.videoWidth > 0 && video.videoHeight > 0) {
      return { element: video, width: video.videoWidth, height: video.videoHeight };
    }
  }

  // Camera feed mode: return video as long as video has loaded stream dimensions
  if (video && video.videoWidth > 0 && video.videoHeight > 0) {
    return { element: video, width: video.videoWidth, height: video.videoHeight };
  }
  if (feedCanvas && feedCanvas.width > 0 && feedCanvas.height > 0) {
    return { element: feedCanvas, width: feedCanvas.width, height: feedCanvas.height };
  }
  return null;
}

/**
 * Enumerate available video inputs and populate dropdown selector.
 */
async function enumerateCameraDevices() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const videoDevices = devices.filter(d => d.kind === 'videoinput');
    const select = document.getElementById('camera-device-select');
    const container = document.getElementById('camera-select-container');
    
    if (select && videoDevices.length > 0) {
      select.innerHTML = '<option value="">-- Switch Camera Input --</option>';
      videoDevices.forEach((dev, idx) => {
        const opt = document.createElement('option');
        opt.value = dev.deviceId;
        opt.innerText = dev.label || `Camera ${idx + 1}`;
        if (_activeCameraDeviceId && dev.deviceId === _activeCameraDeviceId) {
          opt.selected = true;
        }
        select.appendChild(opt);
      });
      if (container && videoDevices.length > 1) {
        container.style.display = 'block';
      }
    }
  } catch (e) {
    console.warn('enumerateDevices error:', e);
  }
}

function onCameraDeviceSelect(deviceId) {
  if (deviceId) {
    _activeCameraDeviceId = deviceId;
    initWebcam(true, deviceId);
  }
}

/**
 * Robust camera initialization with progressive constraint fallback
 * and interactive permissions handling.
 */
async function initWebcam(userInitiated = false, specificDeviceId = null) {
  const video = document.getElementById('live-video');
  const feedCanvas = document.getElementById('feed-canvas');
  const statusBanner = document.getElementById('camera-status-banner');
  const statusTitle = document.getElementById('camera-status-title');
  const statusText = document.getElementById('camera-status-text');
  const startBtn = document.getElementById('btn-start-camera');

  if (specificDeviceId) {
    _activeCameraDeviceId = specificDeviceId;
  }

  // Clean up previous streams or video objects
  if (_webcamStream) {
    _webcamStream.getTracks().forEach(t => t.stop());
    _webcamStream = null;
  }
  if (video && video.src && !video.srcObject) {
    video.removeAttribute('src');
  }

  // Security Context check (Chrome/Safari block getUserMedia on plain HTTP unless localhost/127.0.0.1)
  const isLocalhost = location.hostname === 'localhost' ||
    location.hostname === '127.0.0.1' ||
    location.hostname === '0.0.0.0' ||
    location.hostname === '::1' ||
    location.hostname.endsWith('.localhost');

  if (!window.isSecureContext && !isLocalhost) {
    if (statusTitle) statusTitle.innerText = 'Browser Security Restriction';
    if (statusText) statusText.innerHTML = `⚠️ Webcams require <b>http://localhost:8080</b> or HTTPS.<br>Current URL: <code>${location.origin}</code>.<br><br>👉 Please open <b><a href="http://localhost:8080" style="color:var(--accent-cyan); text-decoration: underline;">http://localhost:8080</a></b> in your browser, or switch to <b>📼 Video Playback</b> mode below.`;
    if (statusBanner) statusBanner.style.display = 'block';
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    if (statusTitle) statusTitle.innerText = 'Camera API Unsupported';
    if (statusText) statusText.innerHTML = '❌ Camera access requires HTTPS or localhost.<br><span style="color:var(--accent-cyan);">Click <b>📼 Video Playback Mode</b> below to upload and analyze CCTV footage.</span>';
    if (statusBanner) statusBanner.style.display = 'block';
    return;
  }

  if (statusTitle) statusTitle.innerText = 'Connecting Camera...';
  if (statusText) statusText.innerHTML = '👉 If your browser displays a permission prompt, please click <b>Allow</b> (usually near top address bar 🔒).';
  if (startBtn) startBtn.innerHTML = '⏳ <b>Connecting to Camera...</b>';

  // 1. Progressive constraint attempts
  let stream = null;
  let lastErr = null;

  const constraintOptions = [];
  if (_activeCameraDeviceId) {
    constraintOptions.push({ video: { deviceId: { exact: _activeCameraDeviceId }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
    constraintOptions.push({ video: { deviceId: { exact: _activeCameraDeviceId } }, audio: false });
  }
  constraintOptions.push({ video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' }, audio: false });
  constraintOptions.push({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
  constraintOptions.push({ video: { width: { ideal: 640 }, height: { ideal: 480 } }, audio: false });
  constraintOptions.push({ video: true, audio: false });

  for (const constraints of constraintOptions) {
    try {
      stream = await navigator.mediaDevices.getUserMedia(constraints);
      if (stream) break;
    } catch (err) {
      lastErr = err;
    }
  }

  if (stream && video) {
    _webcamStream = stream;
    if (feedCanvas) feedCanvas.style.display = 'none';
    video.style.display = 'block';
    video.muted = true;
    video.playsInline = true;
    video.autoplay = true;
    video.setAttribute('playsinline', '');
    video.setAttribute('muted', '');
    video.setAttribute('autoplay', '');
    video.srcObject = stream;

    currentFeedMode = 'camera';
    updateFeedSourceBtnUI();

    const onFeedReady = () => {
      if (statusBanner) statusBanner.style.display = 'none';
      if (startBtn) startBtn.innerHTML = '🎥 Start / Allow Camera Feed';
      addLogLine('📷 <b>Camera active</b>: Streaming physical webcam frames to AI pipeline.');
      showToast('📷 Connected to Physical CCTV Webcam');
      _awaitingResponse = false;
      startFrameCapture();
      sendNextFrame();
      enumerateCameraDevices();
    };

    video.onloadedmetadata = () => {
      video.play().then(() => onFeedReady()).catch(e => console.warn('video.play metadata error:', e));
    };
    video.onplaying = () => {
      onFeedReady();
    };
    video.oncanplay = () => {
      video.play().catch(() => {});
    };

    try {
      await video.play();
      onFeedReady();
    } catch (playErr) {
      console.warn('Initial play promise rejected:', playErr);
    }

    startFrameCapture();

    // Safety timeout: if video has width, hide banner immediately
    setTimeout(() => {
      if (video.videoWidth > 0) {
        onFeedReady();
      }
    }, 300);

  } else {
    console.error('Camera access failed:', lastErr);
    if (startBtn) startBtn.innerHTML = '🎥 Start / Allow Camera Feed';
    let title = 'Camera Permission / Access Required';
    let msg = 'Please click <b>Start / Allow Camera Feed</b> below to enable your camera.';

    if (lastErr && (lastErr.name === 'NotAllowedError' || lastErr.name === 'PermissionDeniedError')) {
      title = 'Camera Permission Blocked';
      msg = 'Camera permission was denied in your browser.<br><span style="color:#ffaa33;">👉 Click the <b>camera/lock icon 🔒</b> in your address bar (top left), set Camera to <b>"Always allow"</b>, then click <b>Start / Allow Camera Feed</b> below.</span>';
    } else if (lastErr && (lastErr.name === 'NotFoundError' || lastErr.name === 'DevicesNotFoundError')) {
      title = 'No Physical Webcam Detected';
      msg = 'No physical webcam found on this system.<br><span style="color:var(--accent-cyan);">👉 Click <b>📼 Video Playback Mode</b> below to upload and monitor a recorded CCTV video file!</span>';
    } else if (lastErr && (lastErr.name === 'NotReadableError' || lastErr.name === 'TrackStartError')) {
      title = 'Camera In Use By Another App';
      msg = 'The webcam is currently locked by another application (FaceTime, Zoom, Teams, OBS). Please close other camera apps and click <b>Start / Allow Camera Feed</b>.';
    } else if (lastErr) {
      msg = `Camera note: ${lastErr.message || lastErr.name}<br><span style="color:var(--accent-cyan);">Click <b>Start / Allow Camera Feed</b> to retry, or switch to <b>📼 Video Playback</b>.</span>`;
    }

    if (statusTitle) statusTitle.innerText = title;
    if (statusText) statusText.innerHTML = msg;
    if (statusBanner) statusBanner.style.display = 'block';
    enumerateCameraDevices();
  }
}

/**
 * Handle user uploading a recorded CCTV video file or monitor snapshot image.
 */
function handleCCTVFileUpload(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;

  const video = document.getElementById('live-video');
  const feedCanvas = document.getElementById('feed-canvas');
  const statusBanner = document.getElementById('camera-status-banner');
  const fileNameElem = document.getElementById('video-file-name');
  const playPauseBtn = document.getElementById('btn-video-play-pause');

  if (_webcamStream) {
    _webcamStream.getTracks().forEach(t => t.stop());
    _webcamStream = null;
  }

  const isVideo = file.type.startsWith('video/') || /\.(mp4|webm|ogg|mov|mkv|avi)$/i.test(file.name);
  const fileUrl = URL.createObjectURL(file);
  _uploadedVideoFile = file;

  currentFeedMode = 'video';
  updateFeedSourceBtnUI();

  if (isVideo) {
    if (feedCanvas) feedCanvas.style.display = 'none';
    video.style.display = 'block';
    video.srcObject = null;
    video.src = fileUrl;
    video.loop = true;
    video.muted = true;
    video.playsInline = true;

    if (fileNameElem) {
      fileNameElem.innerText = file.name.length > 28 ? file.name.substring(0, 25) + '...' : file.name;
      fileNameElem.title = file.name;
    }

    video.onloadedmetadata = () => {
      updateVideoTimeDisplay();
      video.play().then(() => {
        if (playPauseBtn) playPauseBtn.innerHTML = '⏸ Pause';
        if (statusBanner) statusBanner.style.display = 'none';
        showToast(`📼 CCTV Video Playback: ${file.name}`);
        addLogLine(`📼 <b>CCTV Video Playback Started</b>: Streaming ${file.name} to AI pipeline.`);
        startFrameCapture();
      }).catch(err => {
        console.warn('Video auto-play prevented:', err);
        if (playPauseBtn) playPauseBtn.innerHTML = '▶ Play';
        startFrameCapture();
      });
    };

    video.ontimeupdate = () => {
      updateVideoTimeDisplay();
    };

    video.onplay = () => {
      if (playPauseBtn) playPauseBtn.innerHTML = '⏸ Pause';
    };

    video.onpause = () => {
      if (playPauseBtn) playPauseBtn.innerHTML = '▶ Play';
    };

    video.onended = () => {
      if (!video.loop && playPauseBtn) {
        playPauseBtn.innerHTML = '▶ Play';
      }
    };
  } else {
    // Single image file (snapshot)
    const img = new Image();
    img.onload = () => {
      if (video) video.style.display = 'none';
      if (feedCanvas) {
        feedCanvas.style.display = 'block';
        feedCanvas.width = img.naturalWidth || 1280;
        feedCanvas.height = img.naturalHeight || 720;
      }
      const ctx = feedCanvas.getContext('2d');
      ctx.drawImage(img, 0, 0, feedCanvas.width, feedCanvas.height);

      if (fileNameElem) {
        fileNameElem.innerText = `[Image] ${file.name}`;
      }
      if (statusBanner) statusBanner.style.display = 'none';
      showToast(`📁 Loaded CCTV Image: ${file.name}`);
      addLogLine(`📁 <b>CCTV Image Loaded</b>: ${file.name} sent to pipeline.`);
      startFrameCapture();
    };
    img.src = fileUrl;
  }
}

let _awaitingResponseTimeout = null;
let _lastFrameSentTime = 0;

function startFrameCapture() {
  if (_captureInterval) return;  // already running — prevent double-start

  // Create offscreen canvas for JPEG encoding
  _offscreenCanvas = document.createElement('canvas');
  _offscreenCtx = _offscreenCanvas.getContext('2d');

  // Kick off the first frame send
  sendNextFrame();

  // Robust streaming ticker & stale detection expiry
  _captureInterval = setInterval(() => {
    const video = document.getElementById('live-video');
    if (video && video.style.display !== 'none' && video.paused && _webcamStream) {
      video.play().catch(() => {});
    }

    // Refresh split grid sub-canvases at high FPS
    if (displayViewMode === 'SPLIT_GRID') {
      renderSplitGrid(_cachedLastPanes, _cachedLastPersons);
    }

    // Anti-stall watchdog: if awaiting response for more than 350ms, recover automatically
    if (_awaitingResponse && (Date.now() - _lastFrameSentTime > 350)) {
      _awaitingResponse = false;
      sendNextFrame();
    } else if (!_awaitingResponse) {
      sendNextFrame();
    }

    // Clear overlay bounding boxes if no response in 600ms
    if (_lastDetectionTime && (Date.now() - _lastDetectionTime > 600)) {
      const canvas = document.getElementById('detection-canvas');
      if (canvas) {
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
      }
      _lastDetectedPersons = [];
    }
  }, 100);
}

/**
 * Flow-controlled frame sender with automatic watchdog recovery.
 * Captures the CURRENT frame from active feed source (video or canvas) and sends it over WebSocket.
 */
function sendNextFrame() {
  if (_awaitingResponse) return;
  const src = getActiveFeedSource();
  if (!src) return;
  if (!_ws || _ws.readyState !== WebSocket.OPEN) {
    return;
  }

  // Sync offscreen canvas to current source dimensions
  if (_offscreenCanvas.width !== src.width || _offscreenCanvas.height !== src.height) {
    _offscreenCanvas.width = src.width;
    _offscreenCanvas.height = src.height;
  }

  // Also sync the detection overlay canvas
  const overlayCanvas = document.getElementById('detection-canvas');
  if (overlayCanvas && (overlayCanvas.width !== src.width || overlayCanvas.height !== src.height)) {
    overlayCanvas.width = src.width;
    overlayCanvas.height = src.height;
  }

  try {
    _offscreenCtx.drawImage(src.element, 0, 0);
  } catch (err) {
    return;
  }

  _awaitingResponse = true;
  _lastFrameSentTime = Date.now();

  if (_awaitingResponseTimeout) clearTimeout(_awaitingResponseTimeout);
  _awaitingResponseTimeout = setTimeout(() => {
    _awaitingResponse = false;
  }, 350);

  _offscreenCanvas.toBlob((blob) => {
    if (blob && _ws && _ws.readyState === WebSocket.OPEN) {
      blob.arrayBuffer().then(buf => {
        if (_ws && _ws.readyState === WebSocket.OPEN) {
          _ws.send(buf);
        } else {
          _awaitingResponse = false;
        }
      }).catch(() => {
        _awaitingResponse = false;
      });
    } else {
      _awaitingResponse = false;
    }
  }, 'image/jpeg', 0.70);
}

function initWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const wsUrl = `${proto}://${location.host}/ws/stream`;

  _ws = new WebSocket(wsUrl);
  _ws.binaryType = 'arraybuffer';

  _ws.onopen = () => {
    console.log('WebSocket connected to', wsUrl);
    _awaitingResponse = false;
    const dcBanner = document.getElementById('webcam-disconnected-banner');
    if (dcBanner) dcBanner.style.display = 'none';
    addLogLine('🔗 <b>WebSocket connected</b>: Server ready to process camera frames.');
    sendNextFrame();
  };

  _ws.onmessage = (event) => {
    _awaitingResponse = false;  // release flow control — ready for next frame
    if (_awaitingResponseTimeout) clearTimeout(_awaitingResponseTimeout);

    try {
      const data = JSON.parse(event.data);
      if (data.error) {
        console.warn('Server frame error:', data.error);
        sendNextFrame();
        return;
      }
      // Update UI from detection results
      _lastDetectedPersons = data.detected_persons || [];
      _lastPanes = data.panes || [];
      _lastMode = data.mode || 'DIRECT_ROOM_SURVEILLANCE';
      _cachedLastPanes = _lastPanes;
      _cachedLastPersons = _lastDetectedPersons;
      _cachedLastMode = _lastMode;
      _lastDetectionTime = Date.now();
      updateTelemetryFromWs(data);
      if (displayViewMode === 'SPLIT_GRID') {
        renderSplitGrid(_lastPanes, _lastDetectedPersons);
      } else {
        drawDetections(_lastDetectedPersons, _lastPanes, _lastMode);
      }
    } catch (e) {
      console.error('WS message parse error:', e);
    }
    // Immediately send the NEXT frame
    sendNextFrame();
  };

  _ws.onclose = () => {
    _awaitingResponse = false;
    console.log('WebSocket disconnected. Reconnecting in 2s...');
    const dcBanner = document.getElementById('webcam-disconnected-banner');
    if (dcBanner) dcBanner.style.display = 'flex';
    if (_wsReconnectTimer) clearTimeout(_wsReconnectTimer);
    _wsReconnectTimer = setTimeout(initWebSocket, 2000);
  };

  _ws.onerror = (err) => {
    _awaitingResponse = false;
    console.error('WebSocket error:', err);
    const dcBanner = document.getElementById('webcam-disconnected-banner');
    if (dcBanner) dcBanner.style.display = 'flex';
  };
}

function updateTelemetryFromWs(data) {
  // Telemetry metrics
  const fpsElem = document.getElementById('val-fps');
  if (fpsElem && data.metrics) fpsElem.innerText = (data.metrics.processing_fps || 0).toFixed(1);
  const latElem = document.getElementById('val-latency');
  if (latElem && data.metrics) latElem.innerText = `${Math.round(data.metrics.end_to_end_latency_ms || 0)} ms`;
  const confElem = document.getElementById('val-conf');
  if (confElem) confElem.innerText = (data.layout_confidence || 0).toFixed(2);
  const panesElem = document.getElementById('val-panes');
  if (panesElem) {
    if (data.mode === 'DIRECT_ROOM_SURVEILLANCE') {
      panesElem.innerText = `${(data.detected_persons || []).length} Persons`;
    } else {
      const cnt = data.pane_count || (data.panes ? data.panes.length : 0);
      panesElem.innerText = `${cnt} Panes`;
    }
  }

  const layoutTag = document.getElementById('layout-tag');
  if (layoutTag) {
    if (data.is_finalized || isGridLayoutFinalized) {
      isGridLayoutFinalized = true;
      layoutTag.innerText = `LOCKED (${data.pane_count || (data.panes ? data.panes.length : 4)} Panes)`;
      layoutTag.style.color = 'var(--accent-green)';
      layoutTag.style.borderColor = 'var(--accent-green)';
    } else {
      layoutTag.innerText = data.mode === 'DIRECT_ROOM_SURVEILLANCE' ? 'LIVE SURVEILLANCE' : (data.layout_id || 'SCANNING');
      layoutTag.style.color = 'var(--accent-cyan)';
      layoutTag.style.borderColor = 'var(--accent-cyan)';
    }
  }

  // Alert banner
  const banner = document.getElementById('alert-banner');
  const bannerText = document.getElementById('alert-text');
  if (banner && bannerText) {
    const alerts = (data.alerts || []).filter(a => !a.startsWith('WEBCAM'));
    if (alerts.length > 0) {
      banner.style.display = 'flex';
      bannerText.innerText = alerts.join(' | ');
    } else {
      banner.style.display = 'none';
    }
  }

  // Handle Structured Activity Events & Alarms
  if (data.events && data.events.length > 0) {
    window._lastAlarmTimes = window._lastAlarmTimes || {};
    data.events.forEach(ev => {
      if (ev.is_alarm || ev.severity === 'CRITICAL' || ev.severity === 'HIGH') {
        const lastT = window._lastAlarmTimes[ev.event_type] || 0;
        const nowMs = Date.now();
        if (nowMs - lastT > 3500) {
          window._lastAlarmTimes[ev.event_type] = nowMs;
          showToast(`🚨 ${ev.description}`, 'error');
          addLogLine(`<span style="color: var(--accent-red); font-weight: bold;">🚨 ${ev.event_type}</span>: ${ev.description}`);
          if (audioEnabled) playBuzzerBeep();
        }
      }
    });
  }

  if (data.should_announce_audio) {
    const lastAudioT = window._lastIntrusionAudioT || 0;
    if (Date.now() - lastAudioT > 4000) {
      window._lastIntrusionAudioT = Date.now();
      addLogLine('<span style="color: var(--accent-red); font-weight: bold;">🚨 ALERT TRIGGERED</span>: Unified audible buzzer & event active.');
      if (audioEnabled) playBuzzerBeep();
    }
  }

  // Sidebar — render based on pipeline mode
  const container = document.getElementById('panes-container');
  const titleText = document.getElementById('panel-title-text');
  if (container && titleText) {
    if (data.mode === 'DIRECT_ROOM_SURVEILLANCE') {
      titleText.innerText = 'Live Persons in Room';
      renderPersonCards(container, data.detected_persons || []);
    } else {
      // CCTV_WALL_MONITOR mode — Show all detected persons across all panes in the sidebar for tagging
      titleText.innerText = 'Detected Persons (All Panes)';
      renderPersonCards(container, data.detected_persons || []);
    }
  }

  // Layout change notification
  if (data.is_layout_changed) {
    addLogLine(`<span class="event-tag">LAYOUT_CHANGE</span>: Detected ${data.layout_id} (${data.pane_count} panes)`);
  }
}

function drawDetections(persons, panes, mode) {
  const src = getActiveFeedSource();
  const canvas = document.getElementById('detection-canvas');
  if (!canvas || !src || !src.width) return;

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const cw = canvas.width;
  const ch = canvas.height;

  // 1. Draw CCTV Grid Panes
  if (mode === 'CCTV_WALL_MONITOR') {
    if (!panes || panes.length === 0) {
      // Alert box overlay when 0 CCTV panes are detected
      const boxW = Math.min(cw * 0.85, 520);
      const boxH = 50;
      const boxX = (cw - boxW) / 2;
      const boxY = ch - boxH - 24;

      ctx.fillStyle = 'rgba(20, 14, 26, 0.90)';
      ctx.fillRect(boxX, boxY, boxW, boxH);

      ctx.strokeStyle = '#ffaa33';
      ctx.lineWidth = 2;
      ctx.shadowColor = '#ffaa33';
      ctx.shadowBlur = 10;
      ctx.strokeRect(boxX, boxY, boxW, boxH);
      ctx.shadowBlur = 0;

      ctx.font = 'bold 14px "Outfit", "JetBrains Mono", monospace';
      ctx.fillStyle = '#ffcc66';
      ctx.textAlign = 'center';
      ctx.fillText('⚠️ Could not detect CCTV panes in camera view', cw / 2, boxY + 30);
      ctx.textAlign = 'left';
    } else {
      panes.forEach((p, idx) => {
        let sx, sy, sw, sh;
        if (p.norm_bbox && p.norm_bbox.length === 4) {
          sx = p.norm_bbox[0] * cw;
          sy = p.norm_bbox[1] * ch;
          sw = p.norm_bbox[2] * cw;
          sh = p.norm_bbox[3] * ch;
        } else {
          const [x, y, w, h] = p.bbox;
          const scaleX = cw / (video.videoWidth || cw);
          const scaleY = ch / (video.videoHeight || ch);
          sx = x * scaleX; sy = y * scaleY; sw = w * scaleX; sh = h * scaleY;
        }

        const isStable = p.is_stable;
        const color = isStable ? '#00f0ff' : '#ffaa33';
        const fillColor = isStable ? 'rgba(0, 240, 255, 0.08)' : 'rgba(255, 170, 51, 0.08)';

        // Grid Cell Fill & Dashed Border
        ctx.fillStyle = fillColor;
        ctx.fillRect(sx, sy, sw, sh);

        ctx.strokeStyle = color;
        ctx.lineWidth = 2.5;
        ctx.setLineDash([8, 5]);
        ctx.strokeRect(sx, sy, sw, sh);
        ctx.setLineDash([]);

        // Cell Corner Accents
        const cs = Math.min(sw, sh) * 0.08;
        ctx.lineWidth = 3;
        [[sx, sy, cs, 0, 0, cs], [sx+sw, sy, -cs, 0, 0, cs], [sx, sy+sh, cs, 0, 0, -cs], [sx+sw, sy+sh, -cs, 0, 0, -cs]]
          .forEach(([ox, oy, dx1, dy1, dx2, dy2]) => {
            ctx.beginPath();
            ctx.moveTo(ox + dx1, oy + dy1);
            ctx.lineTo(ox, oy);
            ctx.lineTo(ox + dx2, oy + dy2);
            ctx.stroke();
          });

        // Pane Label Badge Tag
        const camLabel = p.camera_label || `CAM-${String(idx + 1).padStart(2, '0')}`;
        const badgeText = `📺 ${p.pane_id.toUpperCase()} | ${camLabel}`;
        ctx.font = 'bold 12px "JetBrains Mono", monospace';
        const tw = ctx.measureText(badgeText).width;
        const lx = sx + 8, ly = sy + 8;

        ctx.fillStyle = isStable ? 'rgba(10, 18, 30, 0.90)' : 'rgba(30, 20, 10, 0.90)';
        ctx.fillRect(lx, ly, tw + 14, 24);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.strokeRect(lx, ly, tw + 14, 24);

        ctx.fillStyle = color;
        ctx.fillText(badgeText, lx + 7, ly + 16);
      });
    }
  }

  // 2. Draw Defined Detection Zones (Doors, Entryways, Restricted ROIs)
  if (window._activeConfigZones) {
    let zonesToDraw = [];
    if (mode === 'DIRECT_ROOM_SURVEILLANCE') {
      zonesToDraw = window._activeConfigZones['LIVE_ROOM_CAM'] || window._activeConfigZones['Pane-01'] || window._activeConfigZones['P01'] || [];
      if (zonesToDraw.length === 0) {
        Object.values(window._activeConfigZones).forEach(zList => {
          if (Array.isArray(zList)) zonesToDraw.push(...zList);
        });
      }
    } else if (panes && panes.length > 0) {
      panes.forEach(p => {
        const pZones = window._activeConfigZones[p.pane_id] || window._activeConfigZones[p.camera_label] || [];
        pZones.forEach(z => {
          if (z.bbox && z.bbox.length === 4) {
            let px = 0, py = 0, pw = cw, ph = ch;
            if (p.norm_bbox && p.norm_bbox.length === 4) {
              px = p.norm_bbox[0] * cw;
              py = p.norm_bbox[1] * ch;
              pw = p.norm_bbox[2] * cw;
              ph = p.norm_bbox[3] * ch;
            }
            zonesToDraw.push({
              ...z,
              abs_x: px + z.bbox[0] * pw,
              abs_y: py + z.bbox[1] * ph,
              abs_w: z.bbox[2] * pw,
              abs_h: z.bbox[3] * ph,
            });
          }
        });
      });
    }

    zonesToDraw.forEach(z => {
      const zx = z.abs_x !== undefined ? z.abs_x : (z.bbox ? z.bbox[0] * cw : 0);
      const zy = z.abs_y !== undefined ? z.abs_y : (z.bbox ? z.bbox[1] * ch : 0);
      const zw = z.abs_w !== undefined ? z.abs_w : (z.bbox ? z.bbox[2] * cw : 0);
      const zh = z.abs_h !== undefined ? z.abs_h : (z.bbox ? z.bbox[3] * ch : 0);
      if (zw <= 0 || zh <= 0) return;

      const zTheme = getZoneColor(z.zone_type);
      ctx.strokeStyle = zTheme.stroke;
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.strokeRect(zx, zy, zw, zh);
      ctx.setLineDash([]);

      ctx.fillStyle = zTheme.fill;
      ctx.fillRect(zx, zy, zw, zh);

      const zTag = `${zTheme.icon} ${z.label || (z.zone_type || 'ZONE').toUpperCase()}`;
      ctx.font = 'bold 11px "JetBrains Mono", monospace';
      const zTagW = ctx.measureText(zTag).width;
      ctx.fillStyle = 'rgba(10, 14, 26, 0.88)';
      ctx.fillRect(zx, zy, Math.min(zw, zTagW + 10), 18);
      ctx.fillStyle = zTheme.stroke;
      ctx.fillText(zTag, zx + 4, zy + 13);
    });
  }

  // 3. Draw Persons
  (persons || []).forEach(p => {
    let sx, sy, sw, sh;
    if (p.norm_bbox && p.norm_bbox.length === 4) {
      sx = p.norm_bbox[0] * cw;
      sy = p.norm_bbox[1] * ch;
      sw = p.norm_bbox[2] * cw;
      sh = p.norm_bbox[3] * ch;
    } else {
      const [x, y, w, h] = p.bbox;
      const scaleX = cw / (video.videoWidth || cw);
      const scaleY = ch / (video.videoHeight || ch);
      sx = x * scaleX; sy = y * scaleY; sw = w * scaleX; sh = h * scaleY;
    }

    const isKnown = p.is_known;
    const color = isKnown ? '#00ff88' : '#ff3366';
    const label = isKnown ? `✓ ${p.person_name} [${p.tag}]` : `⚠ Unknown — ${(p.confidence * 100).toFixed(0)}%`;

    // Bounding box
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5;
    ctx.shadowColor = color;
    ctx.shadowBlur = 8;
    ctx.strokeRect(sx, sy, sw, sh);
    ctx.shadowBlur = 0;

    // Corner accents
    const cs = Math.min(sw, sh) * 0.15;
    ctx.lineWidth = 3;
    [[sx, sy, cs, 0, 0, cs], [sx+sw, sy, -cs, 0, 0, cs], [sx, sy+sh, cs, 0, 0, -cs], [sx+sw, sy+sh, -cs, 0, 0, -cs]]
      .forEach(([ox, oy, dx1, dy1, dx2, dy2]) => {
        ctx.beginPath();
        ctx.moveTo(ox + dx1, oy + dy1);
        ctx.lineTo(ox, oy);
        ctx.lineTo(ox + dx2, oy + dy2);
        ctx.stroke();
      });

    // Label background + text
    ctx.font = 'bold 13px "JetBrains Mono", monospace';
    const tw = ctx.measureText(label).width;
    const lx = sx, ly = sy > 24 ? sy - 8 : sy + sh + 20;
    ctx.fillStyle = isKnown ? 'rgba(0,255,136,0.18)' : 'rgba(255,51,102,0.18)';
    ctx.fillRect(lx - 2, ly - 16, tw + 10, 22);
    ctx.fillStyle = color;
    ctx.fillText(label, lx + 3, ly);
  });
}


function playBuzzerBeep() {
  try {
    if (!audioCtx) initAudio();
    const oscillator = audioCtx.createOscillator();
    const gainNode = audioCtx.createGain();
    oscillator.connect(gainNode);
    gainNode.connect(audioCtx.destination);
    oscillator.frequency.value = 880;
    oscillator.type = 'square';
    gainNode.gain.setValueAtTime(0.3, audioCtx.currentTime);
    gainNode.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.4);
    oscillator.start();
    oscillator.stop(audioCtx.currentTime + 0.4);
  } catch (e) {}
}

function renderPersonCards(container, persons) {
  container.innerHTML = '';
  if (persons.length === 0) {
    container.innerHTML = '<div style="font-size: 0.8rem; color: var(--text-muted); padding: 18px; text-align: center;">Scanning room... No persons currently in view.</div>';
    return;
  }
  persons.forEach((p, idx) => {
    const card = document.createElement('div');
    card.className = 'person-card';
    if (p.is_known) card.classList.add('known');
    const imgTag = p.snapshot_base64
      ? `<img class="person-thumb" src="data:image/jpeg;base64,${p.snapshot_base64}" alt="Face Crop">`
      : `<div class="person-thumb" style="display:flex;align-items:center;justify-content:center;background:#1e2430;">👤</div>`;
    const boxStr = (p.bbox && Array.isArray(p.bbox)) ? p.bbox.join(', ') : 'Track';
    const paneStr = p.pane_id ? `[${p.pane_id}]` : '';
    if (p.is_known) {
      card.innerHTML = `${imgTag}<div class="person-info"><div class="person-header"><span class="person-name" style="color: var(--accent-green);">AUTHORIZED: ${p.person_name}</span><span class="person-tag-badge" style="background: rgba(0, 255, 136, 0.2); color: var(--accent-green);">${p.tag}</span></div><div class="person-meta">${paneStr} Match Conf: ${p.confidence} | Box: [${boxStr}]</div><div style="margin-top: 4px;"><button class="btn" style="padding: 2px 8px; font-size: 0.7rem; color: #ff99b3; border-color: rgba(255,51,102,0.3);" onclick="untagPerson('${p.person_id}', '${p.person_name}')">🗑️ Untag</button></div></div>`;
    } else {
      card.innerHTML = `${imgTag}<div class="person-info"><div class="person-header"><span class="person-name" style="color: var(--accent-red);">🚨 UNKNOWN PERSON</span><span class="person-tag-badge" style="background: rgba(255, 51, 102, 0.2); color: #ff99b3;">INTRUDER</span></div><div class="person-meta">${paneStr} Conf: ${p.confidence} | Box: [${boxStr}]</div><div style="margin-top: 4px;"><button class="btn btn-green" style="padding: 4px 10px; font-size: 0.75rem; font-weight: 600;" id="tag-btn-${idx}">🏷️ Identify &amp; Tag Person</button></div></div>`;
      setTimeout(() => {
        const btn = document.getElementById(`tag-btn-${idx}`);
        if (btn) btn.onclick = () => openTagModal(p);
      }, 0);
    }
    container.appendChild(card);
  });
}

function renderPaneChips(container, panes) {
  container.innerHTML = '';
  if (!panes || panes.length === 0) {
    container.innerHTML = `
      <div style="background: rgba(255, 170, 51, 0.12); border: 1px solid var(--accent-amber); border-radius: 8px; padding: 20px 16px; text-align: center; color: #ffcc66; box-shadow: 0 4px 12px rgba(0,0,0,0.4);">
        <div style="font-size: 1.8rem; margin-bottom: 6px;">⚠️</div>
        <div style="font-weight: 700; font-size: 0.95rem; margin-bottom: 6px;">Could not detect CCTV panes</div>
        <div style="font-size: 0.78rem; color: var(--text-muted); line-height: 1.4;">Point camera at a CCTV monitor grid wall or adjust camera angle and lighting.</div>
      </div>
    `;
    return;
  }
  panes.forEach(p => {
    const chip = document.createElement('div');
    chip.className = 'pane-chip';
    chip.innerHTML = `
      <div class="pane-chip-header">
        <span>${p.pane_id}</span>
        <span class="${p.is_stable ? 'badge-stable' : 'badge-unstable'}">${p.is_stable ? 'STABLE' : 'STABILIZING'}</span>
      </div>
      <div class="pane-label">${p.camera_label}</div>
      <div class="pane-chip-meta">OCR: ${p.ocr_confidence} | Geom: ${p.geometry_confidence}</div>
    `;
    container.appendChild(chip);
  });
}

// Startup
window.addEventListener('DOMContentLoaded', () => {
  syncPipelineModeUI();
  syncDisplayViewModeDOM();
  updateFeedSourceBtnUI();
  loadActiveManualLayout();
  initWebSocket();
  // Connect to CCTV Live Camera detection by default
  initWebcam(false);
  // Enumerate cameras in background
  enumerateCameraDevices();
});

// --- AUDIO INITIALIZATION ---
function initAudio() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (audioCtx.state === 'suspended') {
    audioCtx.resume();
  }
}

window.addEventListener('click', initAudio, { once: true });
window.addEventListener('keydown', initAudio, { once: true });

function toggleAudio() {
  audioEnabled = !audioEnabled;
  const btn = document.getElementById('btn-audio-toggle');
  if (audioEnabled) {
    btn.innerText = '🔊 Alarm Audio: ON';
    btn.classList.add('active');
    initAudio();
    showToast('🔊 Audible alarm buzzer and voice enabled');
  } else {
    btn.innerText = '🔇 Alarm Audio: OFF';
    btn.classList.remove('active');
    showToast('🔇 Audible alarm muted');
  }
}

async function testAlarmManual() {
  try {
    await fetch('/api/test_alarm', { method: 'POST' });
    showToast('🚨 Buzzer & Voice Alarm Triggered');
    addLogLine('<span style="color: var(--accent-red); font-weight: bold;">🚨 TEST ALARM</span>: Single unified buzzer siren & voice announcement triggered.');
  } catch (err) {
    console.error("Test alarm error:", err);
  }
}

async function toggleView() {
  const newMode = currentViewMode === 'original' ? 'rectified' : 'original';
  try {
    const res = await fetch('/api/set_view_mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ view_mode: newMode })
    });
    if (res.ok) {
      currentViewMode = newMode;
      const btn = document.getElementById('btn-view-toggle');
      btn.innerText = `Perspective View: ${newMode === 'rectified' ? 'Rectified (Wall View)' : 'Original'}`;
      btn.classList.toggle('active', newMode === 'rectified');
      showToast(`Switched perspective view to ${newMode}`);
      addLogLine(`Switched camera display view to <b>${newMode}</b>.`);
    }
  } catch (err) {
    console.error("Failed to set view mode:", err);
  }
}
// --- MAIN TAB SWITCHING ---

// --- PIPELINE MODE TOGGLE (Auto / Live Room / CCTV Grid) ---
const _pipelineModes = [
  { key: 'AUTO',      label: '🔄 Mode: Auto-Detect',         color: 'rgba(0, 240, 255, 0.4)' },
  { key: 'ROOM_ONLY', label: '👤 Mode: Live Room Monitoring', color: 'rgba(0, 255, 136, 0.4)' },
  { key: 'CCTV_ONLY', label: '📺 Mode: CCTV Grid Monitor',   color: 'rgba(255, 170, 51, 0.4)' },
];

async function cyclePipelineMode() {
  const currentIdx = _pipelineModes.findIndex(m => m.key === currentPipelineMode);
  const nextIdx = (currentIdx + 1) % _pipelineModes.length;
  const next = _pipelineModes[nextIdx];
  await setPipelineModeFromConfig(next.key);
}

async function setPipelineModeFromConfig(modeKey) {
  try {
    const res = await fetch('/api/set_pipeline_mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: modeKey })
    });
    if (res.ok) {
      currentPipelineMode = modeKey;
      syncPipelineModeUI();
      const modeObj = _pipelineModes.find(m => m.key === modeKey);
      const cleanLabel = modeObj ? modeObj.label.replace(/^[^\s]+ /, '') : modeKey;
      showToast(`Switched pipeline to ${cleanLabel}`);
      addLogLine(`<b>🔄 PIPELINE MODE</b>: Active mode changed to <b>${cleanLabel}</b>.`);
    }
  } catch (err) {
    console.error('Failed to set pipeline mode:', err);
    showToast('❌ Failed to switch pipeline mode');
  }
}

async function rescanGridLayout() {
  const btn = document.getElementById('btn-rescan-grid');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '🔄 Scanning...';
  }
  try {
    const res = await fetch('/api/rescan_layout', { method: 'POST' });
    if (res.ok) {
      isGridLayoutFinalized = false;
      displayViewMode = 'COMPOSITE_WALL';
      syncDisplayViewModeDOM();
      showToast('🔄 Rescanning CCTV grid layout...');
      addLogLine('<b>🔄 RESCAN GRID</b>: Initiated fresh multi-pane grid & bezel discovery.');
      const tag = document.getElementById('layout-tag');
      if (tag) tag.innerText = 'RESCANNING...';
      drawDetections(_cachedLastPersons, _cachedLastPanes, _cachedLastMode);
    }
  } catch (err) {
    console.error('Failed to rescan layout:', err);
    showToast('❌ Failed to trigger grid rescan');
  } finally {
    setTimeout(() => {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '🔄 Rescan Grid';
      }
    }, 1200);
  }
}

function syncPipelineModeUI() {
  const modeObj = _pipelineModes.find(m => m.key === currentPipelineMode) || _pipelineModes[2];
  const btn = document.getElementById('btn-pipeline-mode');
  if (btn) {
    btn.innerText = modeObj.label;
    btn.style.borderColor = modeObj.color;
    btn.classList.add('active');
  }

  const toggleCctv = document.getElementById('cfg-mode-cctv');
  const toggleRoom = document.getElementById('cfg-mode-room');
  const toggleAuto = document.getElementById('cfg-mode-auto');

  if (toggleCctv) toggleCctv.checked = (currentPipelineMode === 'CCTV_ONLY');
  if (toggleRoom) toggleRoom.checked = (currentPipelineMode === 'ROOM_ONLY');
  if (toggleAuto) toggleAuto.checked = (currentPipelineMode === 'AUTO');
}

function switchMainTab(tabName) {
  const viewMonitor = document.getElementById('view-monitor');
  const viewConfig = document.getElementById('view-config');
  const tabBtnMonitor = document.getElementById('tab-btn-monitor');
  const tabBtnConfig = document.getElementById('tab-btn-config');

  if (tabName === 'monitor') {
    viewMonitor.style.display = 'grid';
    viewConfig.style.display = 'none';
    tabBtnMonitor.classList.add('active');
    tabBtnConfig.classList.remove('active');
  } else if (tabName === 'config') {
    // Navigation now handled by window.location.href in HTML
  }
}

// --- TOAST NOTIFICATIONS ---
function showToast(message, duration = 3000) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.innerHTML = message;
  toast.classList.add('show');
  setTimeout(() => {
    toast.classList.remove('show');
  }, duration);
}

// --- KNOWN PERSON REGISTRY MODAL ---
// --- KNOWN PERSON REGISTRY MODAL ---
function openIdentitiesModal() {
  document.getElementById('identities-modal').style.display = 'flex';
  loadIdentities();
}

function closeIdentitiesModal() {
  document.getElementById('identities-modal').style.display = 'none';
}

async function loadIdentities() {
  const container = document.getElementById('identities-list-container');
  try {
    const res = await fetch('/api/known_persons');
    const data = await res.json();
    const persons = data.persons || [];

    document.getElementById('identities-modal-count').innerText = `(${persons.length} Active Profiles in DB)`;

    if (persons.length === 0) {
      container.innerHTML = '<div style="padding: 30px; text-align: center; color: var(--text-muted);">No authorized persons registered yet. Tag faces from Live Monitoring or click "➕ Register Person Manually".</div>';
      return;
    }

    container.innerHTML = '';
    persons.forEach(p => {
      const card = document.createElement('div');
      card.className = 'event-card-item';
      const dt = p.registered_at ? new Date(p.registered_at * 1000).toLocaleString() : 'Active';
      const sampleCount = p.sample_count || 1;

      card.innerHTML = `
        <img class="event-thumb" src="data:image/jpeg;base64,${p.snapshot_base64}" alt="Face Snapshot" onclick="openLightbox('data:image/jpeg;base64,${p.snapshot_base64}', '${p.name} [${p.tag}]')">
        <div style="flex: 1; display: flex; flex-direction: column; gap: 4px;">
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <span style="font-weight: 700; font-size: 0.95rem; color: var(--accent-green);">👤 ${p.name}</span>
            <span class="person-tag-badge" style="background: rgba(0,255,136,0.15); color: var(--accent-green);">${p.tag}</span>
          </div>
          <div style="display: flex; gap: 8px; align-items: center;">
            <span style="font-size: 0.75rem; background: rgba(0,229,255,0.12); color: var(--accent-cyan); padding: 2px 6px; border-radius: 4px; font-weight: 600;">
              🎯 ${sampleCount}/50 Samples Enrolled
            </span>
            <span style="font-size: 0.78rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace;">ID: ${p.person_id}</span>
          </div>
          <div style="font-size: 0.74rem; color: var(--text-muted);">Registered: ${dt}</div>
        </div>
        <button class="btn" style="padding: 6px 12px; font-size: 0.78rem; color: var(--accent-red); border-color: rgba(255,51,102,0.3);" onclick="untagPerson('${p.person_id}', '${p.name}')">🗑️ Remove</button>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    console.error("Failed to load identities:", err);
  }
}

async function clearAllIdentities() {
  if (!confirm("Are you sure you want to clear ALL registered person profiles from the biometric database?")) return;
  try {
    const res = await fetch('/api/known_persons/clear', { method: 'POST' });
    if (res.ok) {
      showToast('🗑️ All registered identities cleared');
      loadIdentities();
      pollStatus();
    }
  } catch (e) {
    console.error("Clear all identities error:", e);
  }
}

// --- TAGGING & MULTI-FRAME TRACK BUFFER CONTROLS ---
function openTagModal(person) {
  activeTagPerson = person;
  const modal = document.getElementById('tag-modal');
  const img = document.getElementById('modal-person-img');
  const boxInfo = document.getElementById('modal-box-info');
  const trackInfo = document.getElementById('modal-track-info');
  const nameInput = document.getElementById('tag-person-name');
  const untagBtn = document.getElementById('btn-untag-person');

  let cleanB64 = (person && person.snapshot_base64) ? person.snapshot_base64 : '';
  if (cleanB64.startsWith('data:image')) {
    cleanB64 = cleanB64.replace(/^data:image\/[a-z]+;base64,/, '');
  }

  if (img) {
    if (cleanB64) {
      img.src = `data:image/jpeg;base64,${cleanB64}`;
      img.style.display = 'block';
    } else {
      img.style.display = 'none';
    }
  }

  if (boxInfo) {
    const bboxText = (person && person.bbox && Array.isArray(person.bbox)) ? person.bbox.join(', ') : 'Direct Capture';
    const confText = (person && person.confidence !== undefined) ? `${Math.round(person.confidence * 100)}%` : '100%';
    boxInfo.innerText = `Box: [${bboxText}] | Confidence: ${confText}`;
  }

  if (trackInfo) {
    const paneStr = (person && person.pane_id) ? ` (Pane: ${person.pane_id})` : '';
    trackInfo.innerText = (person && person.person_id)
      ? `⚡ Enrolling with identity track: ${person.person_id}${paneStr}`
      : `⚡ Direct Pane Snapshot Enrollment${paneStr}`;
  }

  if (nameInput) {
    nameInput.value = (person && person.person_name && person.person_name !== 'Unidentified Person') ? person.person_name : '';
  }

  // Show Untag button only if the person is already known/authorized
  if (untagBtn) {
    untagBtn.style.display = (person && person.is_known) ? 'inline-flex' : 'none';
  }

  if (modal) {
    modal.style.display = 'flex';
  }
  if (nameInput) {
    setTimeout(() => nameInput.focus(), 60);
  }
}

function closeTagModal() {
  document.getElementById('tag-modal').style.display = 'none';
  activeTagPerson = null;
}

async function submitTagPerson() {
  if (!activeTagPerson) return;
  const nameInput = document.getElementById('tag-person-name');
  const roleInput = document.getElementById('tag-person-role');
  const name = nameInput ? nameInput.value.trim() : '';
  const role = (roleInput && roleInput.value.trim()) ? roleInput.value.trim() : 'Authorized';

  if (!name) {
    alert('Please enter a name for the person.');
    if (nameInput) nameInput.focus();
    return;
  }

  let cleanB64 = (activeTagPerson && activeTagPerson.snapshot_base64) ? activeTagPerson.snapshot_base64 : '';
  if (cleanB64.startsWith('data:image')) {
    cleanB64 = cleanB64.replace(/^data:image\/[a-z]+;base64,/, '');
  }

  const trackId = (activeTagPerson && activeTagPerson.person_id) ? activeTagPerson.person_id : `trk_manual_${Date.now().toString(36)}`;

  try {
    const res = await fetch('/api/register_person', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name,
        tag: role,
        snapshot_base64: cleanB64,
        person_id: trackId,
        track_id: trackId
      })
    });

    if (res.ok) {
      showToast(`✅ ${name} registered as ${role}!`);
      addLogLine(`<b>✅ IDENTIFIED & AUTHORIZED</b>: <span style="color: var(--accent-green);">${name}</span> (${role}) registered.`);
      closeTagModal();
      pollStatus();
      loadIdentities();
    } else {
      const err = await res.json();
      alert(`Failed to register person identity: ${err.detail || 'Unknown error'}`);
    }
  } catch (e) {
    console.error("Register person error:", e);
    alert('Network error while registering person. Please try again.');
  }
}

// --- MANUAL PERSON REGISTRATION CONTROLS ---
let manualPhotosList = [];

function openManualRegisterModal() {
  manualPhotosList = [];
  document.getElementById('manual-person-name').value = '';
  document.getElementById('manual-person-role').value = 'Authorized';
  renderManualPhotosPreview();
  document.getElementById('manual-register-modal').style.display = 'flex';
  document.getElementById('manual-person-name').focus();
}

function closeManualRegisterModal() {
  document.getElementById('manual-register-modal').style.display = 'none';
  manualPhotosList = [];
}

function renderManualPhotosPreview() {
  const container = document.getElementById('manual-photos-preview');
  if (manualPhotosList.length === 0) {
    container.innerHTML = '<div style="color: var(--text-muted); font-size: 0.78rem; text-align: center; width: 100%; padding: 12px;">No photos selected. Upload face photos or click "Snapshot Live Feed".</div>';
    return;
  }
  container.innerHTML = '';
  manualPhotosList.forEach((b64, idx) => {
    const wrapper = document.createElement('div');
    wrapper.style.position = 'relative';
    wrapper.style.display = 'inline-block';

    wrapper.innerHTML = `
      <img src="data:image/jpeg;base64,${b64}" style="width: 54px; height: 54px; object-fit: cover; border-radius: 4px; border: 1px solid var(--accent-cyan);">
      <button onclick="removeManualPhoto(${idx})" style="position: absolute; top: -4px; right: -4px; background: var(--accent-red); color: #fff; border: none; border-radius: 50%; width: 16px; height: 16px; font-size: 10px; cursor: pointer; display: flex; align-items: center; justify-content: center;">✕</button>
    `;
    container.appendChild(wrapper);
  });
}

function removeManualPhoto(idx) {
  manualPhotosList.splice(idx, 1);
  renderManualPhotosPreview();
}

function handleManualPhotoFiles(files) {
  if (!files || files.length === 0) return;
  Array.from(files).forEach(file => {
    const reader = new FileReader();
    reader.onload = (e) => {
      const b64 = e.target.result.split(',')[1];
      if (b64 && manualPhotosList.length < 50) {
        manualPhotosList.push(b64);
        renderManualPhotosPreview();
      }
    };
    reader.readAsDataURL(file);
  });
}

function captureLiveWebcamSample() {
  const src = getActiveFeedSource();
  if (!src || !src.width) {
    showToast('⚠️ Live camera feed not available for snapshot');
    return;
  }
  const canvas = document.createElement('canvas');
  canvas.width = src.width;
  canvas.height = src.height;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(src.element, 0, 0);
  const dataUrl = canvas.toDataURL('image/jpeg', 0.9);
  const b64 = dataUrl.split(',')[1];
  if (b64 && manualPhotosList.length < 50) {
    manualPhotosList.push(b64);
    renderManualPhotosPreview();
    showToast(`📷 Captured frame (${manualPhotosList.length} photos ready)`);
  }
}

async function submitManualRegister() {
  const name = document.getElementById('manual-person-name').value.trim();
  const role = document.getElementById('manual-person-role').value.trim() || 'Authorized';

  if (!name) {
    alert('Please enter a name or identifier for the person.');
    return;
  }
  if (manualPhotosList.length === 0) {
    alert('Please upload at least 1 face photo or snapshot the live camera feed.');
    return;
  }

  const submitBtn = document.getElementById('manual-register-submit-btn');
  submitBtn.disabled = true;
  submitBtn.innerText = '⏳ Enrolling Biometrics...';

  try {
    const res = await fetch('/api/register_person_manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name,
        tag: role,
        images_base64: manualPhotosList
      })
    });

    if (res.ok) {
      const data = await res.json();
      showToast(`✅ ${name} manually enrolled with multi-frame gallery!`);
      addLogLine(`<b>✅ MANUALLY ENROLLED</b>: <span style="color: var(--accent-green);">${name}</span> (${role}) registered.`);
      closeManualRegisterModal();
      loadIdentities();
      pollStatus();
    } else {
      const err = await res.json();
      alert(`Registration failed: ${err.detail || 'Could not extract faces from uploaded images'}`);
    }
  } catch (e) {
    console.error("Manual register error:", e);
    alert('Failed to connect to server for manual registration.');
  } finally {
    submitBtn.disabled = false;
    submitBtn.innerText = '💾 Enroll Identity in DB';
  }
}

async function untagPerson(personId, name) {
  if (!confirm(`Are you sure you want to remove authorization for ${name}?`)) return;
  try {
    const res = await fetch(`/api/known_persons/${personId}`, { method: 'DELETE' });
    if (res.ok) {
      showToast(`🗑️ Removed authorization for ${name}`);
      addLogLine(`<b>🗑️ REVOKED IDENTITY</b>: ${name} marked unauthorized.`);
      loadIdentities();
      pollStatus();
    }
  } catch (e) {
    console.error("Untag error:", e);
  }
}

function addLogLine(htmlContent) {
  const log = document.getElementById('event-log');
  if (!log) return;
  const timeStr = new Date().toLocaleTimeString();
  const line = document.createElement('div');
  line.className = 'event-line';
  line.innerHTML = `<span class="event-time">[${timeStr}]</span> ${htmlContent}`;
  log.prepend(line);

  while (log.children.length > 50) {
    log.removeChild(log.lastChild);
  }
}

// --- ACTIVITY LOG & UNUSUAL FRAMES FUNCTIONS ---
function openEventsModal() {
  document.getElementById('events-modal').style.display = 'flex';
  loadEventsData();
}

function closeEventsModal() {
  document.getElementById('events-modal').style.display = 'none';
}

function setEventFilter(filter) {
  activeEventFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  if (filter === 'all') document.getElementById('filter-all').classList.add('active');
  if (filter === 'CRITICAL') document.getElementById('filter-crit').classList.add('active');
  if (filter === 'LAYOUT_CHANGED') document.getElementById('filter-layout').classList.add('active');
  if (filter === 'WARNING') document.getElementById('filter-warn').classList.add('active');
  loadEventsData();
}

async function loadEventsData() {
  const container = document.getElementById('events-list-container');
  try {
    let url = '/api/events?limit=60';
    if (activeEventFilter === 'CRITICAL' || activeEventFilter === 'WARNING') {
      url += `&severity=${activeEventFilter}`;
    } else if (activeEventFilter === 'LAYOUT_CHANGED') {
      url += `&event_type=${activeEventFilter}`;
    }

    const res = await fetch(url);
    const data = await res.json();
    const events = data.events || [];

    document.getElementById('events-modal-count').innerText = `(${events.length} Events in DB)`;
    const badge = document.getElementById('event-badge-count');
    if (badge) badge.innerText = events.length;

    if (events.length === 0) {
      container.innerHTML = '<div style="padding: 30px; text-align: center; color: var(--text-muted);">No unusual frames recorded yet. Security activity will appear here automatically.</div>';
      return;
    }

    container.innerHTML = '';
    events.forEach(ev => {
      const card = document.createElement('div');
      card.className = 'event-card-item';

      let badgeClass = 'event-badge-info';
      if (ev.severity === 'CRITICAL') badgeClass = 'event-badge-crit';
      if (ev.severity === 'WARNING') badgeClass = 'event-badge-warn';

      const dt = new Date(ev.timestamp);
      const timeStr = dt.toLocaleString();

      card.innerHTML = `
        <img class="event-thumb" src="${ev.thumbnail_base64 || '/api/events/' + ev.event_id + '/snapshot'}" alt="Snapshot" onclick="openLightbox('${ev.thumbnail_base64 || '/api/events/' + ev.event_id + '/snapshot'}', '${ev.description} [${timeStr}]')">
        <div style="flex: 1; display: flex; flex-direction: column; gap: 4px;">
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <div style="display: flex; gap: 8px; align-items: center;">
              <span class="person-tag-badge ${badgeClass}">${ev.severity}</span>
              <span style="font-weight: 700; font-size: 0.88rem; color: var(--text-main);">${ev.event_type}</span>
            </div>
            <span style="font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; color: var(--text-muted);">${timeStr}</span>
          </div>
          <div style="font-size: 0.82rem; color: #ccd6e0;">${ev.description}</div>
          <div style="font-size: 0.74rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace;">Mode: ${ev.mode} | Event ID: ${ev.event_id}</div>
        </div>
        <button class="btn" style="padding: 4px 8px; font-size: 0.72rem; color: var(--accent-red); border-color: rgba(255,51,102,0.3);" onclick="deleteSingleEvent('${ev.event_id}')">🗑️</button>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    console.error("Failed to load events:", err);
  }
}

async function deleteSingleEvent(eventId) {
  try {
    const res = await fetch(`/api/events/${eventId}`, { method: 'DELETE' });
    if (res.ok) {
      loadEventsData();
      updateEventBadge();
    }
  } catch (e) {
    console.error("Delete event error:", e);
  }
}

async function clearAllEventsLog() {
  if (!confirm("Are you sure you want to clear all unusual frame records and snapshots from database?")) return;
  try {
    const res = await fetch('/api/events/clear', { method: 'POST' });
    if (res.ok) {
      loadEventsData();
      updateEventBadge();
      addLogLine("Cleared all activity events from database.");
    }
  } catch (e) {
    console.error("Clear events error:", e);
  }
}

function openLightbox(src, caption) {
  const modal = document.getElementById('lightbox-modal');
  const img = document.getElementById('lightbox-img');
  const cap = document.getElementById('lightbox-caption');
  img.src = src;
  cap.innerText = caption || 'Unusual Frame Snapshot';
  modal.style.display = 'flex';
}

function closeLightbox() {
  document.getElementById('lightbox-modal').style.display = 'none';
}

async function pollStatus() {
  // Light poll for secondary UI only (pane sidebar when WS not yet delivering)
  // Main telemetry now comes from WebSocket responses (updateTelemetryFromWs)
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    // Only update sidebar from REST if WebSocket is not delivering results yet
    if (_lastDetectedPersons.length === 0 && data.detected_persons) {
      const container = document.getElementById('panes-container');
      const titleText = document.getElementById('panel-title-text');
      if (container && titleText && data.mode === 'DIRECT_ROOM_SURVEILLANCE') {
        titleText.innerText = 'Live Persons in Room';
        renderPersonCards(container, data.detected_persons || []);
      }
    }

    if (data.is_layout_changed) {
      addLogLine(`<span class="event-tag">LAYOUT_CHANGE</span>: Automatically detected ${data.layout_id} (${data.pane_count} panes)`);
    }
  } catch (err) {
    console.error('Poll error:', err);
  }
}

async function updateEventBadge() {
  try {
    const res = await fetch('/api/events?limit=1');
    const data = await res.json();
    const badge = document.getElementById('event-badge-count');
    if (badge && data.count !== undefined) {
      badge.innerText = data.count;
    }
  } catch (e) {}
}

// =============================================================================
// 4-Corner Physical Display Monitor Calibration Tool
// =============================================================================
let calibSnapshotImg = null;
let activeDragCornerIdx = -1;
let calibSourceWidth = 1280;
let calibSourceHeight = 720;

function openCalibrationModal() {
  const src = getActiveFeedSource();
  const modal = document.getElementById('calibration-modal');
  const canvas = document.getElementById('calibration-canvas');
  if (!modal || !canvas) return;

  // Capture frozen snapshot from active feed (live camera or video playback)
  if (src && src.width > 0 && src.height > 0) {
    calibSourceWidth = src.width;
    calibSourceHeight = src.height;
    const off = document.createElement('canvas');
    off.width = calibSourceWidth;
    off.height = calibSourceHeight;
    const ctx = off.getContext('2d');
    ctx.drawImage(src.element, 0, 0, calibSourceWidth, calibSourceHeight);
    calibSnapshotImg = off;
  }

  modal.style.display = 'flex';
  initCalibrationCanvas();
  loadSavedCalibration();
}

function closeCalibrationModal() {
  const modal = document.getElementById('calibration-modal');
  if (modal) modal.style.display = 'none';
}

async function loadSavedCalibration() {
  try {
    const res = await fetch('/api/get_calibration');
    const json = await res.json();
    if (json.is_calibrated && json.data && json.data.normalized_corners) {
      const nc = json.data.normalized_corners;
      for (let i = 0; i < 4; i++) {
        calibCorners[i].x = nc[i][0];
        calibCorners[i].y = nc[i][1];
      }
    }
  } catch (e) {
    console.warn('Could not load saved calibration:', e);
  }
  drawCalibrationCanvas();
}

function initCalibrationCanvas() {
  const canvas = document.getElementById('calibration-canvas');
  if (!canvas) return;

  canvas.width = calibSourceWidth;
  canvas.height = calibSourceHeight;

  canvas.onmousedown = (e) => onCalibPointerDown(e);
  canvas.onmousemove = (e) => onCalibPointerMove(e);
  canvas.onmouseup = () => onCalibPointerUp();
  canvas.ontouchstart = (e) => onCalibPointerDown(e.touches[0]);
  canvas.ontouchmove = (e) => onCalibPointerMove(e.touches[0]);
  canvas.ontouchend = () => onCalibPointerUp();

  drawCalibrationCanvas();
}

function getCalibCanvasCoords(e) {
  const canvas = document.getElementById('calibration-canvas');
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  return {
    x: (e.clientX - rect.left) * scaleX,
    y: (e.clientY - rect.top) * scaleY,
    normX: (e.clientX - rect.left) / rect.width,
    normY: (e.clientY - rect.top) / rect.height,
  };
}

function onCalibPointerDown(e) {
  const canvas = document.getElementById('calibration-canvas');
  if (!canvas) return;
  const pos = getCalibCanvasCoords(e);
  const hitRadius = canvas.width * 0.05;
  activeDragCornerIdx = -1;

  for (let i = 0; i < 4; i++) {
    const cx = calibCorners[i].x * canvas.width;
    const cy = calibCorners[i].y * canvas.height;
    const dist = Math.hypot(pos.x - cx, pos.y - cy);
    if (dist <= hitRadius) {
      activeDragCornerIdx = i;
      break;
    }
  }

  if (activeDragCornerIdx === -1) {
    let closestIdx = 0;
    let minDist = Infinity;
    for (let i = 0; i < 4; i++) {
      const cx = calibCorners[i].x * canvas.width;
      const cy = calibCorners[i].y * canvas.height;
      const dist = Math.hypot(pos.x - cx, pos.y - cy);
      if (dist < minDist) {
        minDist = dist;
        closestIdx = i;
      }
    }
    calibCorners[closestIdx].x = Math.max(0, Math.min(1, pos.normX));
    calibCorners[closestIdx].y = Math.max(0, Math.min(1, pos.normY));
    activeDragCornerIdx = closestIdx;
    drawCalibrationCanvas();
  }
}

function onCalibPointerMove(e) {
  if (activeDragCornerIdx >= 0) {
    const pos = getCalibCanvasCoords(e);
    calibCorners[activeDragCornerIdx].x = Math.max(0, Math.min(1, pos.normX));
    calibCorners[activeDragCornerIdx].y = Math.max(0, Math.min(1, pos.normY));
    drawCalibrationCanvas();
  }
}

function onCalibPointerUp() {
  activeDragCornerIdx = -1;
}

function drawCalibrationCanvas() {
  const canvas = document.getElementById('calibration-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;

  ctx.clearRect(0, 0, w, h);

  if (calibSnapshotImg) {
    ctx.drawImage(calibSnapshotImg, 0, 0, w, h);
  } else {
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, w, h);
  }

  ctx.fillStyle = 'rgba(0, 0, 0, 0.35)';
  ctx.fillRect(0, 0, w, h);

  const pts = calibCorners.map(c => ({ x: c.x * w, y: c.y * h }));

  ctx.beginPath();
  ctx.moveTo(pts[0].x, pts[0].y);
  ctx.lineTo(pts[1].x, pts[1].y);
  ctx.lineTo(pts[2].x, pts[2].y);
  ctx.lineTo(pts[3].x, pts[3].y);
  ctx.closePath();

  ctx.fillStyle = 'rgba(0, 240, 255, 0.15)';
  ctx.fill();
  ctx.lineWidth = 3;
  ctx.strokeStyle = '#00f0ff';
  ctx.stroke();

  pts.forEach((pt, i) => {
    const c = calibCorners[i];
    ctx.beginPath();
    ctx.arc(pt.x, pt.y, 14, 0, Math.PI * 2);
    ctx.fillStyle = c.color;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#ffffff';
    ctx.stroke();

    ctx.fillStyle = '#000000';
    ctx.font = 'bold 11px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(c.label, pt.x, pt.y);
  });
}

function autoDetectCalibrationCorners() {
  calibCorners = [
    { x: 0.05, y: 0.05, label: 'TL', color: '#00f0ff' },
    { x: 0.95, y: 0.05, label: 'TR', color: '#00ff88' },
    { x: 0.95, y: 0.95, label: 'BR', color: '#ffaa33' },
    { x: 0.05, y: 0.95, label: 'BL', color: '#ff3366' },
  ];
  drawCalibrationCanvas();
  showToast('⚡ Snapped corners to screen borders');
}

async function saveCalibrationCorners() {
  const canvas = document.getElementById('calibration-canvas');
  if (!canvas) return;

  const corners = calibCorners.map(c => [
    Math.round(c.x * calibSourceWidth),
    Math.round(c.y * calibSourceHeight),
  ]);

  try {
    const res = await fetch('/api/calibrate_display', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        corners: corners,
        width: calibSourceWidth,
        height: calibSourceHeight,
      }),
    });
    if (res.ok) {
      showToast('✅ Display calibration saved & applied!');
      addLogLine('<b>🎯 DISPLAY CALIBRATION</b>: Calibrated 4 corners applied for physical display monitor.');
      closeCalibrationModal();
    }
  } catch (err) {
    console.error('Calibration save error:', err);
    showToast('❌ Failed to save calibration');
  }
}

async function resetCalibration() {
  try {
    const res = await fetch('/api/reset_calibration', { method: 'POST' });
    if (res.ok) {
      showToast('Display calibration reset to auto-detection');
      addLogLine('<b>🎯 DISPLAY CALIBRATION</b>: Reset to automatic display detection.');
      closeCalibrationModal();
    }
  } catch (err) {
    console.error('Calibration reset error:', err);
    showToast('❌ Failed to reset calibration');
  }
}

// ----------------------------------------------------------------------------
// INTERACTIVE CCTV GRID PANE READJUSTER (Direct Mesh & Vertex Readjuster)
// ----------------------------------------------------------------------------

let editorRows = 2;
let editorCols = 2;
let editorSnapshotImg = null;
let editorSourceWidth = 1280;
let editorSourceHeight = 720;

const PRESET_DIVIDER_MAP = {
  '2X2': { rows: 2, cols: 2 },
  '1X1': { rows: 1, cols: 1 },
  '1X2': { rows: 1, cols: 2 },
  '2X1': { rows: 2, cols: 1 },
  '2X3': { rows: 2, cols: 3 },
  '3X2': { rows: 3, cols: 2 },
  '3X3': { rows: 3, cols: 3 },
  '3X4': { rows: 3, cols: 4 },
  '4X3': { rows: 4, cols: 3 },
  '4X4': { rows: 4, cols: 4 },
};

let editorActivePreset = '2X2';
let editorActivePaneIndex = 0;
let editorPanes = [];
let editorPaneLabels = {};
let editorMesh = []; // 2D array of (rows + 1) x (cols + 1) normalized [x, y] coordinates
let activeDragState = null;

const AI_SUGGESTED_PANE_LABELS = [
  'CAM-01 (Main Entrance)',
  'CAM-02 (Reception Lobby)',
  'CAM-03 (East Corridor)',
  'CAM-04 (Elevator Bay)',
  'CAM-05 (North Perimeter)',
  'CAM-06 (South Parking)',
  'CAM-07 (Security Office)',
  'CAM-08 (Emergency Exit)',
  'CAM-09 (Server Room)',
  'CAM-10 (Loading Dock)',
  'CAM-11 (West Wing Gate)',
  'CAM-12 (Central Stairwell)',
  'CAM-13 (Roof Terrace)',
  'CAM-14 (Basement Utilities)',
  'CAM-15 (Visitor Parking)',
  'CAM-16 (Perimeter Gate B)'
];

/**
 * Initializes a uniform (rows + 1) x (cols + 1) mesh in normalized 0.0-1.0 coordinate space.
 */
function initMeshFromGrid(rows, cols) {
  editorMesh = [];
  for (let r = 0; r <= rows; r++) {
    const rowPts = [];
    for (let c = 0; c <= cols; c++) {
      rowPts.push([
        roundCoord(c / cols),
        roundCoord(r / rows)
      ]);
    }
    editorMesh.push(rowPts);
  }
}

/**
 * Generates pane geometries and quads from the 2D mesh vertices.
 */
function buildEditorPanesFromMesh() {
  editorPanes = [];
  if (!editorMesh || editorMesh.length !== editorRows + 1) {
    initMeshFromGrid(editorRows, editorCols);
  }

  let idx = 0;
  for (let r = 0; r < editorRows; r++) {
    for (let c = 0; c < editorCols; c++) {
      idx++;
      const pid = `Pane-${String(idx).padStart(2, '0')}`;
      const existingLbl = editorPaneLabels[pid] || (AI_SUGGESTED_PANE_LABELS[idx - 1] || `CAM-${String(idx).padStart(2, '0')}`);
      editorPaneLabels[pid] = existingLbl;

      const c_tl = editorMesh[r][c];
      const c_tr = editorMesh[r][c + 1];
      const c_br = editorMesh[r + 1][c + 1];
      const c_bl = editorMesh[r + 1][c];

      const xs = [c_tl[0], c_tr[0], c_br[0], c_bl[0]];
      const ys = [c_tl[1], c_tr[1], c_br[1], c_bl[1]];
      const minX = Math.min(...xs);
      const maxX = Math.max(...xs);
      const minY = Math.min(...ys);
      const maxY = Math.max(...ys);

      editorPanes.push({
        id: pid,
        index: idx - 1,
        row: r,
        col: c,
        x: roundCoord(minX),
        y: roundCoord(minY),
        w: roundCoord(Math.max(0.01, maxX - minX)),
        h: roundCoord(Math.max(0.01, maxY - minY)),
        corners: [c_tl, c_tr, c_br, c_bl],
        flat_corners: [
          [roundCoord(c / editorCols), roundCoord(r / editorRows)],
          [roundCoord((c + 1) / editorCols), roundCoord(r / editorRows)],
          [roundCoord((c + 1) / editorCols), roundCoord((r + 1) / editorRows)],
          [roundCoord(c / editorCols), roundCoord((r + 1) / editorRows)],
        ],
        label: existingLbl,
      });
    }
  }
}

/**
 * Single Navbar dropdown change handler:
 * Only two options: 'AUTO' (Dynamic AI Scan) or 'MANUAL' (Manual Layout Detection)
 */
async function onLayoutModeDropdownChange(mode) {
  if (mode === 'AUTO') {
    try {
      const res = await fetch('/api/reset_manual_layout', { method: 'POST' });
      if (res.ok) {
        isGridLayoutFinalized = false;
        displayViewMode = 'COMPOSITE_WALL';
        syncDisplayViewModeDOM();
        showToast('🤖 Switched to Automatic AI Grid Scanning');
        addLogLine('<b>🤖 LAYOUT MODE</b>: Automatic AI grid scanning enabled.');
        rescanGridLayout();
      }
    } catch (e) {
      console.error('Failed to reset to AUTO mode:', e);
      showToast('❌ Error switching to AUTO mode');
    }
  } else if (mode === 'MANUAL') {
    openGridEditorModal();
  }
}

function onModalGridPresetChange(preset) {
  editorActivePreset = preset;
  if (PRESET_DIVIDER_MAP[preset]) {
    const p = PRESET_DIVIDER_MAP[preset];
    editorRows = p.rows;
    editorCols = p.cols;
  }
  initMeshFromGrid(editorRows, editorCols);
  buildEditorPanesFromMesh();
  editorActivePaneIndex = 0;
  updateEditorControlsDOM();
  drawGridEditorCanvas();
}

async function loadActiveManualLayout() {
  try {
    const res = await fetch('/api/get_manual_layout');
    const json = await res.json();
    const layoutModeSelect = document.getElementById('select-layout-mode');

    if (json.status === 'ok' && json.config) {
      const cfg = json.config;
      if (cfg) {
        window._activeConfigZones = cfg.zones || {};
      }
      if (cfg.is_active) {
        if (layoutModeSelect) layoutModeSelect.value = 'MANUAL';
        editorActivePreset = cfg.preset || '2X2';
        editorRows = cfg.rows || 2;
        editorCols = cfg.cols || 2;

        if (cfg.mesh && Array.isArray(cfg.mesh) && cfg.mesh.length === editorRows + 1) {
          editorMesh = cfg.mesh;
        } else {
          initMeshFromGrid(editorRows, editorCols);
        }

        if (cfg.panes_metadata && cfg.panes_metadata.length > 0) {
          cfg.panes_metadata.forEach(pm => {
            if (pm.id && pm.label) editorPaneLabels[pm.id] = pm.label;
          });
        }

        buildEditorPanesFromMesh();

        if (cfg.is_finalized) {
          isGridLayoutFinalized = true;
          displayViewMode = 'SPLIT_GRID';
        } else {
          isGridLayoutFinalized = false;
          displayViewMode = 'COMPOSITE_WALL';
        }
      } else {
        if (layoutModeSelect) layoutModeSelect.value = 'AUTO';
        isGridLayoutFinalized = false;
        displayViewMode = 'COMPOSITE_WALL';
      }
      syncDisplayViewModeDOM();
    }
  } catch (e) {
    console.warn('Could not load manual layout config:', e);
  }
}

function openGridEditorModal() {
  const src = getActiveFeedSource();
  const modal = document.getElementById('grid-editor-modal');
  const canvas = document.getElementById('grid-editor-canvas');
  if (!modal || !canvas) return;

  // 1. Capture freeze frame snapshot
  if (src && src.width > 0 && src.height > 0) {
    editorSourceWidth = src.width;
    editorSourceHeight = src.height;
    const off = document.createElement('canvas');
    off.width = editorSourceWidth;
    off.height = editorSourceHeight;
    const ctx = off.getContext('2d');
    ctx.drawImage(src.element, 0, 0, editorSourceWidth, editorSourceHeight);
    editorSnapshotImg = off;
  }

  // 2. Default preset if not set
  if (!editorActivePreset) editorActivePreset = '2X2';
  const presetDropdown = document.getElementById('modal-grid-preset-select');
  if (presetDropdown) presetDropdown.value = editorActivePreset;

  editorRows = PRESET_DIVIDER_MAP[editorActivePreset] ? PRESET_DIVIDER_MAP[editorActivePreset].rows : 2;
  editorCols = PRESET_DIVIDER_MAP[editorActivePreset] ? PRESET_DIVIDER_MAP[editorActivePreset].cols : 2;

  if (!editorMesh || editorMesh.length !== editorRows + 1) {
    initMeshFromGrid(editorRows, editorCols);
  }
  buildEditorPanesFromMesh();
  editorActivePaneIndex = 0;

  modal.style.display = 'flex';
  initGridEditorCanvas();
  updateEditorControlsDOM();
  drawGridEditorCanvas();
}

function closeGridEditorModal() {
  const modal = document.getElementById('grid-editor-modal');
  if (modal) modal.style.display = 'none';

  const layoutModeSelect = document.getElementById('select-layout-mode');
  if (layoutModeSelect) {
    layoutModeSelect.value = isGridLayoutFinalized ? 'MANUAL' : 'AUTO';
  }
}

function updateEditorControlsDOM() {
  const rEl = document.getElementById('editor-row-count');
  const cEl = document.getElementById('editor-col-count');
  const badge = document.getElementById('grid-editor-status-badge');
  const activeBadge = document.getElementById('editor-active-pane-badge');
  const labelInput = document.getElementById('editor-pane-label-input');

  if (rEl) rEl.textContent = editorRows;
  if (cEl) cEl.textContent = editorCols;
  if (badge) badge.textContent = `${editorRows}x${editorCols} (${editorPanes.length} Panes)`;

  const curPane = editorPanes[editorActivePaneIndex];
  if (curPane) {
    if (activeBadge) activeBadge.textContent = curPane.id;
    if (labelInput) labelInput.value = curPane.label || editorPaneLabels[curPane.id] || '';
  }
}

function changeEditorRows(delta) {
  const newR = Math.max(1, Math.min(8, editorRows + delta));
  if (newR !== editorRows) {
    editorRows = newR;
    editorActivePreset = 'CUSTOM';
    const presetDropdown = document.getElementById('modal-grid-preset-select');
    if (presetDropdown) presetDropdown.value = 'CUSTOM';
    initMeshFromGrid(editorRows, editorCols);
    buildEditorPanesFromMesh();
    updateEditorControlsDOM();
    drawGridEditorCanvas();
  }
}

function changeEditorCols(delta) {
  const newC = Math.max(1, Math.min(8, editorCols + delta));
  if (newC !== editorCols) {
    editorCols = newC;
    editorActivePreset = 'CUSTOM';
    const presetDropdown = document.getElementById('modal-grid-preset-select');
    if (presetDropdown) presetDropdown.value = 'CUSTOM';
    initMeshFromGrid(editorRows, editorCols);
    buildEditorPanesFromMesh();
    updateEditorControlsDOM();
    drawGridEditorCanvas();
  }
}

function distributeEditorEvenly() {
  initMeshFromGrid(editorRows, editorCols);
  buildEditorPanesFromMesh();
  drawGridEditorCanvas();
  showToast('⚖️ Grid mesh equalized evenly');
}

// Pane Labeling: AI Suggestion & Manual Override
function suggestActivePaneLabel() {
  if (!editorPanes[editorActivePaneIndex]) return;
  const curPane = editorPanes[editorActivePaneIndex];
  const suggested = AI_SUGGESTED_PANE_LABELS[curPane.index % AI_SUGGESTED_PANE_LABELS.length];
  curPane.label = suggested;
  editorPaneLabels[curPane.id] = suggested;

  const labelInput = document.getElementById('editor-pane-label-input');
  if (labelInput) labelInput.value = suggested;

  drawGridEditorCanvas();
  showToast(`✨ AI suggested label: ${suggested}`);
}

function suggestAllPanesLabelsAI() {
  editorPanes.forEach((p, i) => {
    const suggested = AI_SUGGESTED_PANE_LABELS[i % AI_SUGGESTED_PANE_LABELS.length];
    p.label = suggested;
    editorPaneLabels[p.id] = suggested;
  });
  updateEditorControlsDOM();
  drawGridEditorCanvas();
  showToast(`✨ Auto-assigned smart AI labels to all ${editorPanes.length} panes`);
}

function onActivePaneLabelInput(val) {
  if (!editorPanes[editorActivePaneIndex]) return;
  const curPane = editorPanes[editorActivePaneIndex];
  curPane.label = val;
  editorPaneLabels[curPane.id] = val;
  drawGridEditorCanvas();
}

function initGridEditorCanvas() {
  const canvas = document.getElementById('grid-editor-canvas');
  if (!canvas) return;

  canvas.width = editorSourceWidth;
  canvas.height = editorSourceHeight;

  canvas.onmousedown = (e) => onEditorPointerDown(e);
  canvas.onmousemove = (e) => onEditorPointerMove(e);
  canvas.onmouseup = () => onEditorPointerUp();
  canvas.onmouseleave = () => onEditorPointerUp();
  canvas.ontouchstart = (e) => onEditorPointerDown(e.touches[0]);
  canvas.ontouchmove = (e) => onEditorPointerMove(e.touches[0]);
  canvas.ontouchend = () => onEditorPointerUp();

  drawGridEditorCanvas();
}

function getEditorCanvasCoords(e) {
  const canvas = document.getElementById('grid-editor-canvas');
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  const pixelX = (e.clientX - rect.left) * scaleX;
  const pixelY = (e.clientY - rect.top) * scaleY;
  return {
    pixelX: pixelX,
    pixelY: pixelY,
    normX: Math.max(0.001, Math.min(0.999, pixelX / canvas.width)),
    normY: Math.max(0.001, Math.min(0.999, pixelY / canvas.height)),
  };
}

function findHitMeshTarget(coords) {
  const canvas = document.getElementById('grid-editor-canvas');
  if (!canvas) return null;
  const W = canvas.width;
  const H = canvas.height;

  // 1. Check Vertex handles (radius 18px)
  for (let r = 0; r <= editorRows; r++) {
    for (let c = 0; c <= editorCols; c++) {
      const pt = editorMesh[r][c];
      const vx = pt[0] * W;
      const vy = pt[1] * H;
      if (Math.hypot(coords.pixelX - vx, coords.pixelY - vy) <= 18) {
        return { type: 'vertex', r, c };
      }
    }
  }

  // 2. Check Horizontal Edge midpoints and lines
  for (let r = 0; r <= editorRows; r++) {
    for (let c = 0; c < editorCols; c++) {
      const p0 = editorMesh[r][c];
      const p1 = editorMesh[r][c + 1];
      const mx = ((p0[0] + p1[0]) / 2) * W;
      const my = ((p0[1] + p1[1]) / 2) * H;
      if (Math.hypot(coords.pixelX - mx, coords.pixelY - my) <= 20) {
        return { type: 'h-edge', r, c, startY: coords.normY };
      }
    }
  }

  // 3. Check Vertical Edge midpoints and lines
  for (let r = 0; r < editorRows; r++) {
    for (let c = 0; c <= editorCols; c++) {
      const p0 = editorMesh[r][c];
      const p1 = editorMesh[r + 1][c];
      const mx = ((p0[0] + p1[0]) / 2) * W;
      const my = ((p0[1] + p1[1]) / 2) * H;
      if (Math.hypot(coords.pixelX - mx, coords.pixelY - my) <= 20) {
        return { type: 'v-edge', r, c, startX: coords.normX };
      }
    }
  }

  // 4. Check Panes (click inside pane body)
  for (let i = editorPanes.length - 1; i >= 0; i--) {
    const p = editorPanes[i];
    const px = p.x * W;
    const py = p.y * H;
    const pw = p.w * W;
    const ph = p.h * H;
    if (coords.pixelX >= px && coords.pixelX <= px + pw && coords.pixelY >= py && coords.pixelY <= py + ph) {
      return { type: 'pane-body', paneIndex: i, startX: coords.normX, startY: coords.normY };
    }
  }

  return null;
}

function onEditorPointerDown(e) {
  const coords = getEditorCanvasCoords(e);
  const target = findHitMeshTarget(coords);

  if (target) {
    activeDragState = target;
    if (target.paneIndex !== undefined) {
      editorActivePaneIndex = target.paneIndex;
      updateEditorControlsDOM();
    }
    drawGridEditorCanvas();
  }
}

function onEditorPointerMove(e) {
  const canvas = document.getElementById('grid-editor-canvas');
  if (!canvas) return;
  const coords = getEditorCanvasCoords(e);

  if (activeDragState) {
    if (activeDragState.type === 'vertex') {
      const { r, c } = activeDragState;
      editorMesh[r][c] = [roundCoord(coords.normX), roundCoord(coords.normY)];
      buildEditorPanesFromMesh();
      canvas.style.cursor = 'move';
      drawGridEditorCanvas();
      return;
    } else if (activeDragState.type === 'h-edge') {
      const { r, c, startY } = activeDragState;
      const dy = coords.normY - startY;
      activeDragState.startY = coords.normY;

      editorMesh[r][c][1] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r][c][1] + dy)));
      editorMesh[r][c + 1][1] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r][c + 1][1] + dy)));

      buildEditorPanesFromMesh();
      canvas.style.cursor = 'ns-resize';
      drawGridEditorCanvas();
      return;
    } else if (activeDragState.type === 'v-edge') {
      const { r, c, startX } = activeDragState;
      const dx = coords.normX - startX;
      activeDragState.startX = coords.normX;

      editorMesh[r][c][0] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r][c][0] + dx)));
      editorMesh[r + 1][c][0] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r + 1][c][0] + dx)));

      buildEditorPanesFromMesh();
      canvas.style.cursor = 'ew-resize';
      drawGridEditorCanvas();
      return;
    } else if (activeDragState.type === 'pane-body') {
      const { paneIndex, startX, startY } = activeDragState;
      const dx = coords.normX - startX;
      const dy = coords.normY - startY;
      activeDragState.startX = coords.normX;
      activeDragState.startY = coords.normY;

      const p = editorPanes[paneIndex];
      const r = p.row;
      const c = p.col;

      editorMesh[r][c][0] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r][c][0] + dx)));
      editorMesh[r][c][1] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r][c][1] + dy)));

      editorMesh[r][c + 1][0] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r][c + 1][0] + dx)));
      editorMesh[r][c + 1][1] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r][c + 1][1] + dy)));

      editorMesh[r + 1][c + 1][0] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r + 1][c + 1][0] + dx)));
      editorMesh[r + 1][c + 1][1] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r + 1][c + 1][1] + dy)));

      editorMesh[r + 1][c][0] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r + 1][c][0] + dx)));
      editorMesh[r + 1][c][1] = roundCoord(Math.max(0.001, Math.min(0.999, editorMesh[r + 1][c][1] + dy)));

      buildEditorPanesFromMesh();
      canvas.style.cursor = 'move';
      drawGridEditorCanvas();
      return;
    }
  }

  // Hover detection
  const target = findHitMeshTarget(coords);
  if (target) {
    if (target.type === 'vertex') canvas.style.cursor = 'move';
    else if (target.type === 'h-edge') canvas.style.cursor = 'ns-resize';
    else if (target.type === 'v-edge') canvas.style.cursor = 'ew-resize';
    else if (target.type === 'pane-body') canvas.style.cursor = 'pointer';
  } else {
    canvas.style.cursor = 'crosshair';
  }
}

function onEditorPointerUp() {
  activeDragState = null;
  drawGridEditorCanvas();
}

function drawGridEditorCanvas() {
  const canvas = document.getElementById('grid-editor-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width;
  const H = canvas.height;

  ctx.clearRect(0, 0, W, H);

  // Background frame
  if (editorSnapshotImg) {
    ctx.drawImage(editorSnapshotImg, 0, 0, W, H);
    ctx.fillStyle = 'rgba(10, 14, 26, 0.35)';
    ctx.fillRect(0, 0, W, H);
  } else {
    ctx.fillStyle = '#080c14';
    ctx.fillRect(0, 0, W, H);
  }

  // 1. Draw Panes
  editorPanes.forEach((p, idx) => {
    const isSelected = idx === editorActivePaneIndex;
    const c_tl = p.corners[0];
    const c_tr = p.corners[1];
    const c_br = p.corners[2];
    const c_bl = p.corners[3];

    ctx.beginPath();
    ctx.moveTo(c_tl[0] * W, c_tl[1] * H);
    ctx.lineTo(c_tr[0] * W, c_tr[1] * H);
    ctx.lineTo(c_br[0] * W, c_br[1] * H);
    ctx.lineTo(c_bl[0] * W, c_bl[1] * H);
    ctx.closePath();

    // Fill
    ctx.fillStyle = isSelected ? 'rgba(0, 240, 255, 0.22)' : ((idx % 2 === 0) ? 'rgba(0, 240, 255, 0.05)' : 'rgba(0, 255, 136, 0.04)');
    ctx.fill();

    // Border
    if (isSelected) {
      ctx.strokeStyle = '#00f0ff';
      ctx.lineWidth = 3;
      ctx.shadowColor = '#00f0ff';
      ctx.shadowBlur = 12;
      ctx.stroke();
      ctx.shadowBlur = 0;
    } else {
      ctx.strokeStyle = 'rgba(0, 240, 255, 0.4)';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    // Centered Pane Badge
    const cx = ((c_tl[0] + c_tr[0] + c_br[0] + c_bl[0]) / 4) * W;
    const cy = ((c_tl[1] + c_tr[1] + c_br[1] + c_bl[1]) / 4) * H;
    const lbl = p.label || editorPaneLabels[p.id] || p.id;
    const displayTitle = `${p.id}: ${lbl}`;

    ctx.font = 'bold 12px "JetBrains Mono", monospace';
    const textMetrics = ctx.measureText(displayTitle);
    const badgeW = Math.max(90, textMetrics.width + 20);
    const badgeH = 26;

    ctx.fillStyle = isSelected ? 'rgba(0, 20, 36, 0.95)' : 'rgba(10, 14, 26, 0.88)';
    ctx.fillRect(cx - badgeW / 2, cy - badgeH / 2, badgeW, badgeH);

    ctx.strokeStyle = isSelected ? '#00f0ff' : 'rgba(0, 240, 255, 0.5)';
    ctx.lineWidth = isSelected ? 2 : 1;
    ctx.strokeRect(cx - badgeW / 2, cy - badgeH / 2, badgeW, badgeH);

    ctx.fillStyle = isSelected ? '#00f0ff' : '#a0e8ff';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(displayTitle, cx, cy);

    // Resolution subtext
    ctx.fillStyle = 'rgba(255, 255, 255, 0.7)';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.fillText(`${Math.round(p.w * W)}x${Math.round(p.h * H)} px`, cx, cy + 18);
  });

  // 2. Midpoint Grab Pills on Horizontal Grid Lines
  for (let r = 0; r <= editorRows; r++) {
    for (let c = 0; c < editorCols; c++) {
      const p0 = editorMesh[r][c];
      const p1 = editorMesh[r][c + 1];
      const mx = ((p0[0] + p1[0]) / 2) * W;
      const my = ((p0[1] + p1[1]) / 2) * H;
      const hw = 24, hh = 8;

      ctx.fillStyle = 'rgba(10, 26, 46, 0.9)';
      ctx.fillRect(mx - hw / 2, my - hh / 2, hw, hh);
      ctx.strokeStyle = 'rgba(0, 240, 255, 0.7)';
      ctx.lineWidth = 1;
      ctx.strokeRect(mx - hw / 2, my - hh / 2, hw, hh);
    }
  }

  // 3. Midpoint Grab Pills on Vertical Grid Lines
  for (let r = 0; r < editorRows; r++) {
    for (let c = 0; c <= editorCols; c++) {
      const p0 = editorMesh[r][c];
      const p1 = editorMesh[r + 1][c];
      const mx = ((p0[0] + p1[0]) / 2) * W;
      const my = ((p0[1] + p1[1]) / 2) * H;
      const hw = 8, hh = 24;

      ctx.fillStyle = 'rgba(10, 26, 46, 0.9)';
      ctx.fillRect(mx - hw / 2, my - hh / 2, hw, hh);
      ctx.strokeStyle = 'rgba(0, 240, 255, 0.7)';
      ctx.lineWidth = 1;
      ctx.strokeRect(mx - hw / 2, my - hh / 2, hw, hh);
    }
  }

  // 4. Mesh Corner Vertex Handles
  for (let r = 0; r <= editorRows; r++) {
    for (let c = 0; c <= editorCols; c++) {
      const pt = editorMesh[r][c];
      const vx = pt[0] * W;
      const vy = pt[1] * H;
      const isDrag = (activeDragState && activeDragState.type === 'vertex' && activeDragState.r === r && activeDragState.c === c);

      ctx.beginPath();
      ctx.arc(vx, vy, isDrag ? 9 : 6, 0, Math.PI * 2);
      ctx.fillStyle = isDrag ? '#00ff88' : '#00f0ff';
      ctx.shadowColor = isDrag ? '#00ff88' : '#00f0ff';
      ctx.shadowBlur = isDrag ? 12 : 6;
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.lineWidth = 2;
      ctx.strokeStyle = '#ffffff';
      ctx.stroke();
    }
  }
}

/**
 * Finalize Grid: locks layout, persists mesh and metadata,
 * triggers center-splitting animation and activates real-time multi-pane detection.
 */
async function saveManualGridLayout(finalize = false) {
  try {
    const endpoint = finalize ? '/api/finalize_grid' : '/api/set_manual_layout';

    // Compute average x_dividers and y_dividers for legacy consumer endpoints
    const x_divs = [];
    for (let c = 1; c < editorCols; c++) {
      let sumX = 0;
      for (let r = 0; r <= editorRows; r++) {
        sumX += editorMesh[r][c][0];
      }
      x_divs.push(roundCoord(sumX / (editorRows + 1)));
    }

    const y_divs = [];
    for (let r = 1; r < editorRows; r++) {
      let sumY = 0;
      for (let c = 0; c <= editorCols; c++) {
        sumY += editorMesh[r][c][1];
      }
      y_divs.push(roundCoord(sumY / (editorCols + 1)));
    }

    const payload = {
      preset: editorActivePreset || 'CUSTOM',
      rows: editorRows,
      cols: editorCols,
      x_dividers: x_divs,
      y_dividers: y_divs,
      panes_metadata: editorPanes.map(p => ({
        id: p.id,
        row: p.row,
        col: p.col,
        corners: p.corners,
        flat_corners: p.flat_corners,
        x: p.x,
        y: p.y,
        width: p.w,
        height: p.h,
        label: p.label || editorPaneLabels[p.id],
      })),
      mesh: editorMesh,
      pane_labels: editorPaneLabels,
    };

    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (res.ok) {
      closeGridEditorModal();

      if (finalize) {
        isGridLayoutFinalized = true;
        displayViewMode = 'SPLIT_GRID';
        syncDisplayViewModeDOM();

        const modeSelect = document.getElementById('select-layout-mode');
        if (modeSelect) modeSelect.value = 'MANUAL';

        const src = getActiveFeedSource();
        const srcW = src ? src.width : 1280;
        const srcH = src ? src.height : 720;

        _cachedLastPanes = editorPanes.map((p, idx) => ({
          pane_id: p.id,
          index: idx,
          row: p.row,
          col: p.col,
          camera_label: p.label || editorPaneLabels[p.id] || `CAM-${String(idx + 1).padStart(2, '0')}`,
          corners: p.corners,
          norm_bbox: [p.x, p.y, p.w, p.h],
          bbox: [p.x * srcW, p.y * srcH, p.w * srcW, p.h * srcH],
          ocr_confidence: 0.98,
          geometry_confidence: 1.0,
          is_stable: true,
        }));

        // Smooth Center-Splitting Animation Trigger
        const splitContainer = document.getElementById('split-grid-container');
        if (splitContainer) {
          splitContainer.classList.remove('splitting-from-center');
          void splitContainer.offsetWidth; // Reflow to reset animation
          splitContainer.classList.add('splitting-from-center');
          setTimeout(() => {
            splitContainer.classList.remove('splitting-from-center');
          }, 850);
        }

        renderSplitGrid(_cachedLastPanes, _cachedLastPersons);
        showToast(`🔒 Layout Finalized (${editorRows}x${editorCols})! Split view activated.`);
        addLogLine(`<b>🔒 GRID FINALIZED</b>: ${editorRows}x${editorCols} grid locked.`);
      } else {
        showToast(`✅ ${editorRows}x${editorCols} Custom Grid preview updated.`);
      }
    } else {
      showToast('❌ Failed to save grid layout');
    }
  } catch (err) {
    console.error('Save manual layout error:', err);
    showToast('❌ Error saving grid layout');
  }
}


function roundCoord(val) {
  return Math.round(val * 10000) / 10000;
}

async function resetManualGridLayout() {
  try {
    const res = await fetch('/api/reset_manual_layout', { method: 'POST' });
    if (res.ok) {
      isGridLayoutFinalized = false;
      displayViewMode = 'COMPOSITE_WALL';
      syncDisplayViewModeDOM();
      closeGridEditorModal();

      const modeSelect = document.getElementById('select-layout-mode');
      if (modeSelect) modeSelect.value = 'AUTO';

      showToast('✨ Reverted to Automatic AI Grid Scanning');
      addLogLine('<b>📐 GRID LAYOUT</b>: Reverted to automatic dynamic discovery.');
      rescanGridLayout();
    }
  } catch (err) {
    console.error('Reset manual layout error:', err);
    showToast('❌ Error resetting grid layout');
  }
}

// Periodic secondary polls (event badge every 3s, status fallback every 2s)
setInterval(updateEventBadge, 3000);
setInterval(pollStatus, 2000);
updateEventBadge();
loadActiveManualLayout();
addLogLine('Dashboard initialized. Opening browser webcam and connecting to AI server…');

// --- Zone Editor State & Logic ---
let zoneEditorSnapshotImg = null;
let zoneEditorState = {
  activePaneId: null,
  zones: [], // Array of { zone_id, label, zone_type, bbox: [x,y,w,h], authorized_persons: [] }
  selectedZoneIndex: -1,
  isDrawing: false,
  startPos: { x: 0, y: 0 },
  currentRect: { x: 0, y: 0, w: 0, h: 0 },
};

function captureZoneEditorSnapshot(paneId, manualPanesMeta) {
  const src = getActiveFeedSource();
  let sourceEl = null;
  let srcW = 0, srcH = 0;

  if (src && src.width > 0 && src.height > 0) {
    sourceEl = src.element;
    srcW = src.width;
    srcH = src.height;
  } else if (editorSnapshotImg && editorSnapshotImg.width > 0) {
    sourceEl = editorSnapshotImg;
    srcW = editorSnapshotImg.width;
    srcH = editorSnapshotImg.height;
  }

  if (!sourceEl || srcW === 0 || srcH === 0) {
    zoneEditorSnapshotImg = null;
    return;
  }

  // Determine normalized bounding box [normX, normY, normW, normH] for paneId
  let normX = 0, normY = 0, normW = 1.0, normH = 1.0;
  let matched = false;

  // 1. Check passed manualPanesMeta
  if (Array.isArray(manualPanesMeta)) {
    const p = manualPanesMeta.find(item => item.id === paneId || item.pane_id === paneId);
    if (p && (p.width > 0 || (p.norm_bbox && p.norm_bbox[2] > 0))) {
      normX = (p.x !== undefined) ? p.x : (p.norm_bbox ? p.norm_bbox[0] : 0);
      normY = (p.y !== undefined) ? p.y : (p.norm_bbox ? p.norm_bbox[1] : 0);
      normW = (p.width !== undefined) ? p.width : (p.w !== undefined ? p.w : (p.norm_bbox ? p.norm_bbox[2] : 1.0));
      normH = (p.height !== undefined) ? p.height : (p.h !== undefined ? p.h : (p.norm_bbox ? p.norm_bbox[3] : 1.0));
      matched = true;
    }
  }

  // 2. Check editorPanes
  if (!matched && Array.isArray(editorPanes) && editorPanes.length > 0) {
    const p = editorPanes.find(item => item.id === paneId);
    if (p && p.w > 0 && p.h > 0) {
      normX = p.x;
      normY = p.y;
      normW = p.w;
      normH = p.h;
      matched = true;
    }
  }

  // 3. Check _cachedLastPanes
  if (!matched && Array.isArray(_cachedLastPanes) && _cachedLastPanes.length > 0) {
    const p = _cachedLastPanes.find(item => item.pane_id === paneId);
    if (p) {
      if (p.norm_bbox && p.norm_bbox[2] > 0) {
        [normX, normY, normW, normH] = p.norm_bbox;
        matched = true;
      } else if (p.bbox && p.bbox[2] > 0) {
        normX = p.bbox[0] / srcW;
        normY = p.bbox[1] / srcH;
        normW = p.bbox[2] / srcW;
        normH = p.bbox[3] / srcH;
        matched = true;
      }
    }
  }

  // 4. Fallback for Pane-01, Pane-02 ... in grid
  if (!matched && paneId && /^Pane-\d+$/i.test(paneId)) {
    const pNum = parseInt(paneId.replace(/\D/g, ''), 10);
    if (pNum >= 1 && pNum <= 16) {
      const idx = pNum - 1;
      const rows = (editorRows > 0) ? editorRows : 2;
      const cols = (editorCols > 0) ? editorCols : 2;
      const r = Math.floor(idx / cols) % rows;
      const c = idx % cols;
      normX = c / cols;
      normY = r / rows;
      normW = 1.0 / cols;
      normH = 1.0 / rows;
      matched = true;
    }
  }

  const off = document.createElement('canvas');
  off.width = 640;
  off.height = 360;
  const ctx = off.getContext('2d');

  const sx = Math.max(0, Math.floor(normX * srcW));
  const sy = Math.max(0, Math.floor(normY * srcH));
  const sw = Math.min(srcW - sx, Math.ceil(normW * srcW));
  const sh = Math.min(srcH - sy, Math.ceil(normH * srcH));

  if (sw > 0 && sh > 0) {
    ctx.drawImage(sourceEl, sx, sy, sw, sh, 0, 0, off.width, off.height);
  } else {
    ctx.drawImage(sourceEl, 0, 0, srcW, srcH, 0, 0, off.width, off.height);
  }

  zoneEditorSnapshotImg = off;
}

function refreshZoneEditorSnapshot() {
  captureZoneEditorSnapshot(zoneEditorState.activePaneId);
  renderZones();
  showToast(`📸 Frame refreshed for ${zoneEditorState.activePaneId || 'selected pane'}`);
}

function openZoneEditorModal(paneId) {
  if (!paneId) {
    const curPane = (editorPanes && editorPanes[editorActivePaneIndex]) || (_cachedLastPanes && _cachedLastPanes[0]);
    paneId = (curPane && (curPane.id || curPane.pane_id)) ? (curPane.id || curPane.pane_id) : 'Pane-01';
  }
  zoneEditorState.activePaneId = paneId;
  const badge = document.getElementById('zone-editor-pane-badge');
  if (badge) badge.textContent = paneId;

  // Immediate frame capture from current feed
  captureZoneEditorSnapshot(paneId);

  // Load existing zones & configuration for this pane
  fetch('/api/get_manual_layout')
    .then(res => res.json())
    .then(data => {
      const cfg = data.config || {};
      if (cfg.panes_metadata) {
        captureZoneEditorSnapshot(paneId, cfg.panes_metadata);
      }

      const paneZones = cfg.zones ? cfg.zones[paneId] || [] : [];
      zoneEditorState.zones = paneZones.map((z, idx) => ({
        ...z,
        zone_id: z.zone_id || `zone_${idx + 1}`,
      }));

      // Load rules for this pane
      currentPaneRules = cfg.rules ? cfg.rules[paneId] || {} : {};
      const intrCb = document.getElementById('rule-intrusion-enabled');
      if (intrCb) intrCb.checked = currentPaneRules.intrusion_enabled ?? true;
      const actCb = document.getElementById('rule-activity-enabled');
      if (actCb) actCb.checked = currentPaneRules.activity_enabled ?? true;

      initZoneEditorCanvas();
      renderZones();
      document.getElementById('zone-editor-modal').style.display = 'flex';
    })
    .catch(err => {
      console.error('Failed to load zones:', err);
      zoneEditorState.zones = [];
      initZoneEditorCanvas();
      renderZones();
      document.getElementById('zone-editor-modal').style.display = 'flex';
    });
}

function closeZoneEditorModal() {
  document.getElementById('zone-editor-modal').style.display = 'none';
  zoneEditorState.selectedZoneIndex = -1;
  zoneEditorState.isDrawing = false;
}

function initZoneEditorCanvas() {
  const canvas = document.getElementById('zone-editor-canvas');
  if (!canvas) return;

  canvas.width = 640;
  canvas.height = 360;

  canvas.onmousedown = (e) => {
    const rect = canvas.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (canvas.width / rect.width);
    const y = (e.clientY - rect.top) * (canvas.height / rect.height);

    let foundIdx = -1;
    zoneEditorState.zones.forEach((z, idx) => {
      const [zx, zy, zw, zh] = z.bbox;
      const px = zx * canvas.width;
      const py = zy * canvas.height;
      const pw = zw * canvas.width;
      const ph = zh * canvas.height;
      if (x >= px && x <= px + pw && y >= py && y <= py + ph) {
        foundIdx = idx;
      }
    });

    if (foundIdx !== -1) {
      zoneEditorState.selectedZoneIndex = foundIdx;
      showZoneProperties(foundIdx);
    } else {
      zoneEditorState.selectedZoneIndex = -1;
      zoneEditorState.isDrawing = true;
      zoneEditorState.startPos = { x, y };
      showZonePropsEmpty();
    }
    renderZones();
  };

  canvas.onmousemove = (e) => {
    if (!zoneEditorState.isDrawing) return;
    const rect = canvas.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (canvas.width / rect.width);
    const y = (e.clientY - rect.top) * (canvas.height / rect.height);

    zoneEditorState.currentRect = {
      x: Math.min(x, zoneEditorState.startPos.x),
      y: Math.min(y, zoneEditorState.startPos.y),
      w: Math.abs(x - zoneEditorState.startPos.x),
      h: Math.abs(y - zoneEditorState.startPos.y),
    };
    renderZones();
  };

  canvas.onmouseup = () => {
    if (!zoneEditorState.isDrawing) return;
    zoneEditorState.isDrawing = false;

    const { x, y, w, h } = zoneEditorState.currentRect;
    if (w > 5 && h > 5) {
      const newZone = {
        zone_id: `zone_${zoneEditorState.zones.length + 1}`,
        label: `Zone ${zoneEditorState.zones.length + 1}`,
        zone_type: 'general',
        bbox: [x / canvas.width, y / canvas.height, w / canvas.width, h / canvas.height],
        authorized_persons: [],
      };
      zoneEditorState.zones.push(newZone);
      zoneEditorState.selectedZoneIndex = zoneEditorState.zones.length - 1;
      showZoneProperties(zoneEditorState.selectedZoneIndex);
    }
    renderZones();
  };
}

function getZoneColor(type) {
  const t = (type || '').toLowerCase();
  if (t === 'door' || t === 'entry' || t === 'exit') return { stroke: '#00f0ff', fill: 'rgba(0, 240, 255, 0.15)', icon: '🚪' };
  if (t === 'object_box' || t === 'box') return { stroke: '#ffaa00', fill: 'rgba(255, 170, 0, 0.18)', icon: '📦' };
  if (t === 'restricted') return { stroke: '#ff3366', fill: 'rgba(255, 51, 102, 0.20)', icon: '⛔' };
  if (t === 'workstation' || t === 'computer' || t === 'desk') return { stroke: '#b366ff', fill: 'rgba(179, 102, 255, 0.15)', icon: '💻' };
  if (t === 'waiting') return { stroke: '#ffea00', fill: 'rgba(255, 234, 0, 0.15)', icon: '⏳' };
  return { stroke: '#00ff88', fill: 'rgba(0, 255, 136, 0.12)', icon: '🌐' };
}

function renderZones() {
  const canvas = document.getElementById('zone-editor-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width;
  const H = canvas.height;

  ctx.clearRect(0, 0, W, H);

  // 1. Draw freeze frame snapshot if available
  if (zoneEditorSnapshotImg) {
    ctx.drawImage(zoneEditorSnapshotImg, 0, 0, W, H);
    // Overlay subtle dark tint for zone contrast
    ctx.fillStyle = 'rgba(10, 14, 26, 0.30)';
    ctx.fillRect(0, 0, W, H);
  } else {
    ctx.fillStyle = '#080c14';
    ctx.fillRect(0, 0, W, H);
  }

  // 2. Subtle grid lines overlay
  ctx.strokeStyle = zoneEditorSnapshotImg ? 'rgba(255, 255, 255, 0.10)' : 'rgba(255, 255, 255, 0.05)';
  ctx.lineWidth = 1;
  for(let i=0; i<W; i+=40) { ctx.beginPath(); ctx.moveTo(i,0); ctx.lineTo(i,H); ctx.stroke(); }
  for(let i=0; i<H; i+=40) { ctx.beginPath(); ctx.moveTo(0,i); ctx.lineTo(W,i); ctx.stroke(); }

  // 3. Render all defined zones
  zoneEditorState.zones.forEach((z, idx) => {
    const [zx, zy, zw, zh] = z.bbox;
    const px = zx * W;
    const py = zy * H;
    const pw = zw * W;
    const ph = zh * H;

    const zTheme = getZoneColor(z.zone_type);
    const isSel = (idx === zoneEditorState.selectedZoneIndex);

    ctx.strokeStyle = isSel ? '#ffffff' : zTheme.stroke;
    ctx.lineWidth = isSel ? 3.5 : 2;
    if (isSel) {
      ctx.shadowColor = zTheme.stroke;
      ctx.shadowBlur = 8;
    }
    ctx.strokeRect(px, py, pw, ph);
    ctx.shadowBlur = 0;

    ctx.fillStyle = isSel ? 'rgba(255, 255, 255, 0.25)' : zTheme.fill;
    ctx.fillRect(px, py, pw, ph);

    // Label Header
    const tag = `${zTheme.icon} ${z.label || z.zone_type.toUpperCase()}`;
    ctx.font = 'bold 11px "JetBrains Mono", monospace';
    const tagW = ctx.measureText(tag).width;
    ctx.fillStyle = 'rgba(10, 14, 26, 0.85)';
    ctx.fillRect(px, py, Math.min(pw, tagW + 10), 18);
    ctx.fillStyle = isSel ? '#ffffff' : zTheme.stroke;
    ctx.fillText(tag, px + 4, py + 13);
  });

  // 4. Render actively dragged / drawn rectangle
  if (zoneEditorState.isDrawing) {
    const { x, y, w, h } = zoneEditorState.currentRect;
    ctx.strokeStyle = '#00f0ff';
    ctx.lineWidth = 2;
    ctx.setLineDash([5, 5]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);
  }
}

function showZoneProperties(idx) {
  const zone = zoneEditorState.zones[idx];
  if (!zone) return;

  document.getElementById('zone-props-empty').style.display = 'none';
  const form = document.getElementById('zone-props-form');
  form.style.display = 'flex';

  document.getElementById('zone-label-input').value = zone.label;
  document.getElementById('zone-type-select').value = zone.zone_type || 'general';

  const entryCb = document.getElementById('zone-alarm-entry-cb');
  if (entryCb) entryCb.checked = zone.alarm_on_entry !== false;

  const liftCb = document.getElementById('zone-alarm-lift-cb');
  if (liftCb) liftCb.checked = zone.alarm_on_lift !== false;

  const loiterInput = document.getElementById('zone-loiter-sec-input');
  if (loiterInput) loiterInput.value = zone.loiter_threshold_sec || 10;

  renderAuthChips(zone.authorized_persons);
}

function showZonePropsEmpty() {
  document.getElementById('zone-props-empty').style.display = 'block';
  document.getElementById('zone-props-form').style.display = 'none';
}

function renderAuthChips(persons) {
  const container = document.getElementById('zone-auth-list');
  container.innerHTML = '';

  if (!persons || persons.length === 0) {
    container.innerHTML = '<div style="color:var(--text-muted); font-size:0.7rem; text-align:center; padding:4px;">No authorized persons (Alarms active for everyone)</div>';
    return;
  }

  persons.forEach(pId => {
    const chip = document.createElement('div');
    chip.style.cssText = 'display:flex; justify-content:space-between; align-items:center; background:rgba(0,240,255,0.15); border:1px solid rgba(0,240,255,0.4); padding:2px 8px; border-radius:4px; font-size:0.72rem; color:#fff; font-family:"JetBrains Mono",monospace;';
    chip.innerHTML = `<span>👤 ${pId}</span><span style="cursor:pointer; color:var(--accent-red); margin-left:8px;" onclick="removeAuthPerson('${pId}')">✕</span>`;
    container.appendChild(chip);
  });
}

function updateZoneProperties() {
  const idx = zoneEditorState.selectedZoneIndex;
  if (idx === -1) return;

  const zone = zoneEditorState.zones[idx];
  zone.label = document.getElementById('zone-label-input').value;
  zone.zone_type = document.getElementById('zone-type-select').value;

  const entryCb = document.getElementById('zone-alarm-entry-cb');
  if (entryCb) zone.alarm_on_entry = entryCb.checked;

  const liftCb = document.getElementById('zone-alarm-lift-cb');
  if (liftCb) zone.alarm_on_lift = liftCb.checked;

  const loiterInput = document.getElementById('zone-loiter-sec-input');
  if (loiterInput) zone.loiter_threshold_sec = parseFloat(loiterInput.value) || 10.0;

  renderZones();
}

function deleteCurrentZone() {
  const idx = zoneEditorState.selectedZoneIndex;
  if (idx === -1) return;

  zoneEditorState.zones.splice(idx, 1);
  zoneEditorState.selectedZoneIndex = -1;
  showZonePropsEmpty();
  renderZones();
}

async function saveZonesToConfig() {
  try {
    const payload = {
      pane_id: zoneEditorState.activePaneId,
      zones: zoneEditorState.zones,
    };
    const res = await fetch('/api/save_zones', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      window._activeConfigZones = window._activeConfigZones || {};
      window._activeConfigZones[zoneEditorState.activePaneId] = JSON.parse(JSON.stringify(zoneEditorState.zones));
      showToast('Zones saved successfully!', 'success');
      closeZoneEditorModal();
    } else {
      showToast('Failed to save zones', 'error');
    }
  } catch (err) {
    console.error('Error saving zones:', err);
    showToast('Error saving zones', 'error');
  }
}


function openAuthPersonSelector() {
  const pId = prompt('Enter Person ID (e.g. P001) to authorize:');
  if (pId) {
    const idx = zoneEditorState.selectedZoneIndex;
    if (idx !== -1) {
      const zone = zoneEditorState.zones[idx];
      if (!zone.authorized_persons.includes(pId)) {
        zone.authorized_persons.push(pId);
        renderAuthChips(zone.authorized_persons);
      }
    }
  }
}

function removeAuthPerson(pId) {
  const idx = zoneEditorState.selectedZoneIndex;
  if (idx === -1) return;

  const zone = zoneEditorState.zones[idx];
  zone.authorized_persons = zone.authorized_persons.filter(id => id !== pId);
  renderAuthChips(zone.authorized_persons);
}

let currentPaneRules = {};

function updateRuleSetting(ruleKey, value) {
  currentPaneRules[ruleKey] = value;
}

async function savePaneRules() {
  try {
    const payload = {
      pane_id: zoneEditorState.activePaneId,
      rules: currentPaneRules,
    };
    const res = await fetch('/api/save_rules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      showToast('Rules saved successfully!', 'success');
    } else {
      showToast('Failed to save rules', 'error');
    }
  } catch (err) {
    console.error('Error saving rules:', err);
    showToast('Error saving rules', 'error');
  }
}

async function searchPOI() {
  const input = document.getElementById('poi-search-input');
  const resultsContainer = document.getElementById('poi-search-results');
  const query = input.value.trim();

  if (!query) {
    resultsContainer.style.display = 'none';
    return;
  }

  try {
    const res = await fetch(`/api/persons/search?q=${encodeURIComponent(query)}`);
    const data = await res.json();

    resultsContainer.style.display = 'block';
    if (data.error) {
      resultsContainer.innerHTML = `<div style="color: var(--accent-red);">❌ ${data.error}</div>`;
      return;
    }

    const p = data.person;
    if (!p) {
      resultsContainer.innerHTML = `<div style="color: var(--text-muted);">👤 No Person of Interest found matching "${query}".</div>`;
      return;
    }

    const lastSeen = data.last_seen || {};
    const frameB64 = lastSeen.frame_base64;
    const paneId = lastSeen.pane_id || 'Unknown';
    const timestamp = lastSeen.timestamp ? new Date(lastSeen.timestamp * 1000).toLocaleString() : 'Unknown';

    resultsContainer.innerHTML = `
      <div style="display: flex; gap: 12px; align-items: center;">
        ${frameB64 ? `<img src="data:image/jpeg;base64,${frameB64}" style="width: 60px; height: 60px; object-fit: cover; border: 1px solid var(--accent-cyan); border-radius: 4px;">` : '<div style="width:60px;height:60px;background:rgba(255,255,255,0.1);border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:1.5rem;">👤</div>'}
        <div style="flex: 1;">
          <div style="font-weight: 700; color: var(--accent-green);">${p.name} <span style="font-weight: normal; color: var(--text-muted); font-size: 0.75rem;">(${p.tag})</span></div>
          <div style="font-size: 0.75rem; color: var(--text-secondary);">
            Last Seen: <b style="color: var(--accent-cyan);">${paneId}</b> at ${timestamp}
          </div>
        </div>
      </div>
      ${frameB64 ? `<button class="btn" style="margin-top: 8px; width: 100%; padding: 4px; font-size: 0.7rem;" onclick="openLightbox('data:image/jpeg;base64,${frameB64}', '${p.name} - Last Seen in ${paneId}')">🔍 Expand Frame</button>` : ''}
    `;
  } catch (err) {
    console.error('POI search error:', err);
    resultsContainer.style.display = 'block';
    resultsContainer.innerHTML = `<div style="color: var(--accent-red);">❌ Network error during POI search.</div>`;
  }
}


