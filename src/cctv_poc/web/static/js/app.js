/**
 * CCTV Physical Display Monitoring - Standalone Frontend Application
 * Framework-agnostic client for real-time video, telemetry, alerts, identity management, and system configuration.
 */

// Global client state
let currentViewMode = 'original';
let audioEnabled = true;
let audioCtx = null;
let activeTagPerson = null;
let activeEventFilter = 'all';
let currentConfig = null;

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
    viewMonitor.style.display = 'none';
    viewConfig.style.display = 'block';
    tabBtnMonitor.classList.remove('active');
    tabBtnConfig.classList.add('active');
    loadConfiguration();
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

// --- CONFIGURATION MANAGEMENT ---
function updateRangeLabel(id, value) {
  const badge = document.getElementById(`val-${id}`);
  if (badge) {
    badge.innerText = value;
  }
}

async function loadConfiguration() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    if (data.status === 'ok' && data.config) {
      currentConfig = data.config;
      populateConfigForm(data.config);
    }
  } catch (err) {
    console.error("Failed to load configuration:", err);
    showToast('⚠️ Could not load configuration');
  }
}

function populateConfigForm(cfg) {
  // Biometrics
  if (cfg.biometrics) {
    document.getElementById('cfg-biometrics-threshold').value = cfg.biometrics.match_threshold || 0.48;
    updateRangeLabel('biometrics-threshold', cfg.biometrics.match_threshold || 0.48);
    document.getElementById('cfg-biometrics-model').value = cfg.biometrics.model_name || 'InsightFace ArcFace ResNet-50 (512-d)';
    document.getElementById('cfg-biometrics-alignment').checked = cfg.biometrics.strict_landmark_alignment !== false;
  }

  // Detection
  if (cfg.detection) {
    document.getElementById('cfg-detection-score').value = cfg.detection.score_threshold || 0.70;
    updateRangeLabel('detection-score', cfg.detection.score_threshold || 0.70);
    document.getElementById('cfg-detection-nms').value = cfg.detection.nms_threshold || 0.30;
    updateRangeLabel('detection-nms', cfg.detection.nms_threshold || 0.30);
    document.getElementById('cfg-detection-minsize').value = cfg.detection.min_face_size || 35;
    updateRangeLabel('detection-minsize', (cfg.detection.min_face_size || 35) + ' px');
  }

  // Alert
  if (cfg.alert) {
    document.getElementById('cfg-alert-frames').value = cfg.alert.min_consecutive_frames || 5;
    updateRangeLabel('alert-frames', (cfg.alert.min_consecutive_frames || 5) + ' frames');
    document.getElementById('cfg-alert-cooldown').value = cfg.alert.clear_cooldown_seconds || 6.0;
    updateRangeLabel('alert-cooldown', (cfg.alert.clear_cooldown_seconds || 6.0) + 's');
    document.getElementById('cfg-alert-voice').checked = cfg.alert.voice_enabled !== false;
    document.getElementById('cfg-alert-buzzer').checked = cfg.alert.buzzer_enabled !== false;
    document.getElementById('cfg-alert-text').value = cfg.alert.announcement_text || 'Unknown person detected!';
  }

  // Display
  if (cfg.display) {
    document.getElementById('cfg-display-autodetect').checked = cfg.display.auto_detect !== false;
    document.getElementById('cfg-display-minarea').value = cfg.display.min_area_fraction || 0.12;
    updateRangeLabel('display-minarea', Math.round((cfg.display.min_area_fraction || 0.12) * 100) + '%');
    document.getElementById('cfg-display-calibmode').value = cfg.display.calibration_mode || 'assisted';
    const targetRes = `${cfg.display.target_width || 3840}x${cfg.display.target_height || 2160}`;
    const resElem = document.getElementById('cfg-display-targetres');
    if (resElem) {
      if (resElem.querySelector(`option[value="${targetRes}"]`)) {
        resElem.value = targetRes;
      }
    }
  }

  // Layout
  if (cfg.layout) {
    document.getElementById('cfg-layout-dynamic').checked = cfg.layout.dynamic !== false;
    document.getElementById('cfg-layout-stability').value = cfg.layout.stability_frames || 10;
    updateRangeLabel('layout-stability', (cfg.layout.stability_frames || 10) + ' frames');
    document.getElementById('cfg-layout-aspect').value = cfg.layout.expected_aspect_ratio || 'auto';
  }

  // Video
  if (cfg.video) {
    document.getElementById('cfg-video-source').value = cfg.video.source_type || 'webcam';
    document.getElementById('cfg-video-device').value = cfg.video.device !== undefined ? cfg.video.device : '0';
    document.getElementById('cfg-video-fps').value = cfg.video.requested_fps || 30;
    updateRangeLabel('video-fps', (cfg.video.requested_fps || 30) + ' FPS');
  }

  // OCR & VLM
  if (cfg.ocr) {
    document.getElementById('cfg-ocr-enabled').checked = cfg.ocr.enabled !== false;
    document.getElementById('cfg-ocr-interval').value = cfg.ocr.sample_every_n_frames || 15;
    updateRangeLabel('ocr-interval', (cfg.ocr.sample_every_n_frames || 15) + ' frames');
  }
  if (cfg.vlm) {
    document.getElementById('cfg-vlm-provider').value = cfg.vlm.provider || 'mock';
  }

  // Output
  if (cfg.output) {
    document.getElementById('cfg-output-snapshots').checked = cfg.output.save_snapshots !== false;
    document.getElementById('cfg-output-dir').value = cfg.output.output_directory || 'data/output';
    document.getElementById('cfg-output-jsonl').value = cfg.output.jsonl || 'data/output/results.jsonl';
  }
}

async function saveConfiguration() {
  if (!currentConfig) currentConfig = {};

  const targetResStr = document.getElementById('cfg-display-targetres').value.split('x');
  const targetW = parseInt(targetResStr[0]) || 3840;
  const targetH = parseInt(targetResStr[1]) || 2160;

  const newConfig = {
    application: currentConfig.application || { name: "cctv-monitor-poc", log_level: "INFO" },
    video: {
      source_type: document.getElementById('cfg-video-source').value,
      device: isNaN(document.getElementById('cfg-video-device').value) ? document.getElementById('cfg-video-device').value : parseInt(document.getElementById('cfg-video-device').value),
      requested_width: targetW,
      requested_height: targetH,
      requested_fps: parseInt(document.getElementById('cfg-video-fps').value) || 30,
      buffer_size: 2
    },
    display: {
      auto_detect: document.getElementById('cfg-display-autodetect').checked,
      calibration_mode: document.getElementById('cfg-display-calibmode').value,
      calibration_file: "config/calibration.json",
      min_area_fraction: parseFloat(document.getElementById('cfg-display-minarea').value) || 0.12,
      target_width: targetW,
      target_height: targetH
    },
    layout: {
      dynamic: document.getElementById('cfg-layout-dynamic').checked,
      min_panes: 1,
      max_panes: 64,
      stability_frames: parseInt(document.getElementById('cfg-layout-stability').value) || 10,
      expected_aspect_ratio: document.getElementById('cfg-layout-aspect').value,
      edge_threshold: 30,
      gutter_min_width: 2
    },
    detection: {
      score_threshold: parseFloat(document.getElementById('cfg-detection-score').value) || 0.70,
      nms_threshold: parseFloat(document.getElementById('cfg-detection-nms').value) || 0.30,
      min_face_size: parseInt(document.getElementById('cfg-detection-minsize').value) || 35
    },
    biometrics: {
      match_threshold: parseFloat(document.getElementById('cfg-biometrics-threshold').value) || 0.48,
      model_name: document.getElementById('cfg-biometrics-model').value,
      strict_landmark_alignment: document.getElementById('cfg-biometrics-alignment').checked
    },
    alert: {
      min_consecutive_frames: parseInt(document.getElementById('cfg-alert-frames').value) || 5,
      clear_cooldown_seconds: parseFloat(document.getElementById('cfg-alert-cooldown').value) || 6.0,
      voice_enabled: document.getElementById('cfg-alert-voice').checked,
      buzzer_enabled: document.getElementById('cfg-alert-buzzer').checked,
      announcement_text: document.getElementById('cfg-alert-text').value.trim() || "Unknown person detected!"
    },
    ocr: {
      enabled: document.getElementById('cfg-ocr-enabled').checked,
      sample_every_n_frames: parseInt(document.getElementById('cfg-ocr-interval').value) || 15,
      confidence_threshold: 0.60,
      engine: "auto"
    },
    vlm: {
      enabled: true,
      mode: "validation",
      sample_interval_seconds: 3.0,
      trigger_on_low_ocr_confidence: true,
      trigger_on_layout_change: true,
      timeout_seconds: 2.0,
      provider: document.getElementById('cfg-vlm-provider').value
    },
    stabilization: currentConfig.stabilization || {
      label_observations_required: 5,
      layout_frames_required: 10,
      change_confirmation_frames: 10
    },
    output: {
      display: true,
      save_snapshots: document.getElementById('cfg-output-snapshots').checked,
      output_directory: document.getElementById('cfg-output-dir').value.trim() || "data/output",
      jsonl: document.getElementById('cfg-output-jsonl').value.trim() || "data/output/results.jsonl",
      metrics_log_interval_frames: 30
    }
  };

  try {
    const res = await fetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(newConfig)
    });

    if (res.ok) {
      currentConfig = newConfig;
      showToast('✅ Configuration saved & applied live to pipeline!');
      addLogLine('<b>⚙️ CONFIG APPLIED</b>: Parameters updated and saved to poc.yaml.');
    } else {
      const err = await res.json();
      showToast(`❌ Error saving config: ${err.detail || 'Validation error'}`);
    }
  } catch (e) {
    console.error("Save config error:", e);
    showToast('❌ Network error saving configuration');
  }
}

async function resetConfiguration() {
  if (!confirm("Reset all pipeline, biometrics, detection, and display settings to factory defaults?")) return;
  try {
    const res = await fetch('/api/config/reset', { method: 'POST' });
    if (res.ok) {
      const data = await res.json();
      currentConfig = data.config;
      populateConfigForm(data.config);
      showToast('🔄 Configuration reset to factory defaults');
      addLogLine('<b>🔄 CONFIG RESET</b>: Restored factory default settings.');
    }
  } catch (e) {
    console.error("Reset config error:", e);
  }
}

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
    const res = await fetch('/api/known_persons');
    const data = await res.json();
    for (const p of data.persons || []) {
      await fetch(`/api/known_persons/${p.person_id}`, { method: 'DELETE' });
    }
    showToast('🗑️ All registered identities cleared');
    loadIdentities();
    pollStatus();
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

  img.src = `data:image/jpeg;base64,${person.snapshot_base64}`;
  boxInfo.innerText = `Box: [${person.bbox.join(', ')}] | Confidence: ${person.confidence}`;
  if (trackInfo) {
    trackInfo.innerText = `⚡ Enrolling with rolling 50-frame buffer (Track: ${person.person_id})`;
  }
  nameInput.value = '';
  modal.style.display = 'flex';
  nameInput.focus();
}

function closeTagModal() {
  document.getElementById('tag-modal').style.display = 'none';
  activeTagPerson = null;
}

async function submitTagPerson() {
  if (!activeTagPerson) return;
  const name = document.getElementById('tag-person-name').value.trim();
  const role = document.getElementById('tag-person-role').value.trim() || 'Authorized';

  if (!name) {
    alert('Please enter a name for the person.');
    return;
  }

  try {
    const res = await fetch('/api/register_person', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name,
        tag: role,
        snapshot_base64: activeTagPerson.snapshot_base64,
        person_id: activeTagPerson.person_id,
        track_id: activeTagPerson.person_id
      })
    });

    if (res.ok) {
      showToast(`✅ ${name} registered as ${role}!`);
      addLogLine(`<b>✅ IDENTIFIED & AUTHORIZED</b>: <span style="color: var(--accent-green);">${name}</span> (${role}) multi-frame enrolled.`);
      closeTagModal();
      pollStatus();
      loadIdentities();
    } else {
      const err = await res.json();
      alert(`Failed to register person identity: ${err.detail || 'Unknown error'}`);
    }
  } catch (e) {
    console.error("Register person error:", e);
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
  // Capture current live frame from the video stream canvas or image
  const imgElement = document.getElementById('live-stream-img');
  if (!imgElement || !imgElement.complete || imgElement.naturalWidth === 0) {
    showToast('⚠️ Live camera feed not available for snapshot');
    return;
  }
  const canvas = document.createElement('canvas');
  canvas.width = imgElement.naturalWidth;
  canvas.height = imgElement.naturalHeight;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(imgElement, 0, 0);
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

// --- POLLING FOR STATUS ---
async function pollStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    // Telemetry
    const fpsElem = document.getElementById('val-fps');
    if (fpsElem) fpsElem.innerText = data.metrics ? data.metrics.processing_fps.toFixed(1) : '0.0';
    const latElem = document.getElementById('val-latency');
    if (latElem) latElem.innerText = data.metrics ? `${data.metrics.end_to_end_latency_ms.toFixed(0)} ms` : '0 ms';
    const confElem = document.getElementById('val-conf');
    if (confElem) confElem.innerText = data.layout_confidence ? data.layout_confidence.toFixed(2) : '0.00';
    const panesElem = document.getElementById('val-panes');
    if (panesElem) panesElem.innerText = data.mode === 'DIRECT_ROOM_SURVEILLANCE' ? `${data.detected_persons ? data.detected_persons.length : 0} Persons` : data.pane_count;

    const layoutTag = document.getElementById('layout-tag');
    if (layoutTag) layoutTag.innerText = data.mode === 'DIRECT_ROOM_SURVEILLANCE' ? 'LIVE SURVEILLANCE' : data.layout_id;

    // Alert Banner
    const banner = document.getElementById('alert-banner');
    const bannerText = document.getElementById('alert-text');
    if (banner && bannerText) {
      if (data.alerts && data.alerts.length > 0) {
        banner.style.display = 'flex';
        bannerText.innerText = data.alerts.join(' | ');
      } else {
        banner.style.display = 'none';
      }
    }

    // Intrusion announcement trigger
    if (data.should_announce_audio) {
      addLogLine('<span style="color: var(--accent-red); font-weight: bold;">🚨 INTRUSION ALERT</span>: Unknown person confirmed after multi-frame analysis.');
    }

    // Sidebar items
    const container = document.getElementById('panes-container');
    const titleText = document.getElementById('panel-title-text');
    if (container && titleText) {
      container.innerHTML = '';

      if (data.mode === 'DIRECT_ROOM_SURVEILLANCE') {
        titleText.innerText = 'Live Persons in Room';

        if (data.detected_persons && data.detected_persons.length > 0) {
          data.detected_persons.forEach((p, idx) => {
            const card = document.createElement('div');
            card.className = 'person-card';
            if (p.is_known) card.classList.add('known');

            const imgTag = p.snapshot_base64 
              ? `<img class="person-thumb" src="data:image/jpeg;base64,${p.snapshot_base64}" alt="Face Crop">`
              : `<div class="person-thumb" style="display:flex;align-items:center;justify-content:center;background:#1e2430;">👤</div>`;

            if (p.is_known) {
              card.innerHTML = `
                ${imgTag}
                <div class="person-info">
                  <div class="person-header">
                    <span class="person-name" style="color: var(--accent-green);">AUTHORIZED: ${p.person_name}</span>
                    <span class="person-tag-badge" style="background: rgba(0, 255, 136, 0.2); color: var(--accent-green);">${p.tag}</span>
                  </div>
                  <div class="person-meta">Match Conf: ${p.confidence} | Box: [${p.bbox.join(', ')}]</div>
                  <div style="margin-top: 4px;">
                    <button class="btn" style="padding: 2px 8px; font-size: 0.7rem; color: #ff99b3; border-color: rgba(255,51,102,0.3);" onclick="untagPerson('${p.person_id}', '${p.person_name}')">🗑️ Untag</button>
                  </div>
                </div>
              `;
            } else {
              card.innerHTML = `
                ${imgTag}
                <div class="person-info">
                  <div class="person-header">
                    <span class="person-name" style="color: var(--accent-red);">🚨 UNKNOWN PERSON</span>
                    <span class="person-tag-badge" style="background: rgba(255, 51, 102, 0.2); color: #ff99b3;">INTRUDER</span>
                  </div>
                  <div class="person-meta">Conf: ${p.confidence} | Box: [${p.bbox.join(', ')}]</div>
                  <div style="margin-top: 4px;">
                    <button class="btn btn-green" style="padding: 4px 10px; font-size: 0.75rem; font-weight: 600;" id="tag-btn-${idx}">🏷️ Identify & Tag Person</button>
                  </div>
                </div>
              `;
              setTimeout(() => {
                const btn = document.getElementById(`tag-btn-${idx}`);
                if (btn) btn.onclick = () => openTagModal(p);
              }, 0);
            }

            container.appendChild(card);
          });
        } else {
          container.innerHTML = '<div style="font-size: 0.8rem; color: var(--text-muted); padding: 18px; text-align: center;">Scanning room... No persons currently in view.</div>';
        }
      } else {
        titleText.innerText = 'Discovered Panes';
        (data.panes || []).forEach(p => {
          const chip = document.createElement('div');
          chip.className = 'pane-chip';
          chip.innerHTML = `
            <div class="pane-chip-header">
              <span>${p.pane_id}</span>
              <span class="${p.is_stable ? 'badge-stable' : 'badge-unstable'}">${p.is_stable ? 'STABLE' : 'STABILIZING'}</span>
            </div>
            <div class="pane-label">${p.camera_label}</div>
            <div class="pane-chip-meta">OCR Conf: ${p.ocr_confidence} | Geom: ${p.geometry_confidence}</div>
          `;
          container.appendChild(chip);
        });
      }
    }

    if (data.is_layout_changed) {
      addLogLine(`<span class="event-tag">LAYOUT_CHANGE</span>: Automatically detected ${data.layout_id} (${data.pane_count} panes)`);
    }
  } catch (err) {
    console.error("Poll error:", err);
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

// Periodic update intervals
setInterval(pollStatus, 500);
setInterval(updateEventBadge, 2500);
updateEventBadge();
addLogLine("Dashboard initialized with Decoupled Frontend & Live Configuration Engine.");
