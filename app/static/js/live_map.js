// Live map — see every online captain in real time.
//
// Data flow:
//   1. On page load, GET /live-map/data for the initial snapshot.
//   2. Open a Socket.IO connection to /inbox.
//   3. Handle three server events:
//        driver_position_update    — upsert a marker at new coords
//        driver_position_removed   — remove a marker
//        ride_lifecycle_update     — update the sidebar rides list
//
// State lives in two Maps (driverId → marker, rideId → row) so upserts are O(1).
//
// Entry point is window.initLiveMap, invoked by the Google Maps loader's
// &callback= once the SDK is ready — nothing below can run before that.
window.initLiveMap = function () {
  const { benhaCenter, mapId } = window.WASSALNY || {};
  const center = benhaCenter || { lat: 30.4560, lng: 31.1836 };

  // -------- state --------
  const markers = new Map();   // driver_id -> AdvancedMarkerElement
  const captains = new Map();  // driver_id -> {lat, lng, available, on_trip_ride_id, name}
  const rides = new Map();     // ride_id -> ride payload

  // -------- map --------
  // mapId is required: AdvancedMarkerElement silently refuses to render
  // without one, and every marker on this page is custom HTML. It also
  // carries the dark style, so there is no `styles` array here.
  const AdvancedMarker = google.maps.marker.AdvancedMarkerElement;
  const map = new google.maps.Map(document.getElementById('live-map'), {
    center,
    zoom: 12,
    mapId: mapId || undefined,
    mapTypeControl: false,
    streetViewControl: false,
    // The "+ رحلة جديدة" FAB sits top-right, where the fullscreen button
    // would otherwise land.
    fullscreenControl: false,
    zoomControl: true,
    // One-finger / no-modifier panning — an ops console is dragged constantly.
    gestureHandling: 'greedy',
    clickableIcons: false,
  });

  // -------- camera helpers --------
  function panTo(lat, lng, zoom) {
    if (lat == null || lng == null) return;
    map.panTo({ lat: lat, lng: lng });
    if (zoom != null) map.setZoom(zoom);
  }

  // -------- helpers --------
  function _capKey(cap) {
    // Accept either shape — /live-map/data snapshot emits `driver_id`
    // (post-fix) but old cached responses / other sockets may still send
    // `id`. Fall back so nothing gets keyed by undefined.
    return cap.driver_id != null ? cap.driver_id : cap.id;
  }

  function _stateClass(cap) {
    // Priority ordering matters — a captain on a trip is "busy" even
    // if their heartbeat lapsed briefly. Beyond that:
    //   busy     — currently on an active ride (orange)
    //   offline  — Redis has GPS but presence.online is false (red)
    //   ghost    — online but heartbeat is stale (dashed grey) — app
    //              probably force-quit; NOT safe to dispatch
    //   available — green, actually reachable
    //   unavailable — online + fresh but toggled busy (grey)
    if (cap.on_trip_ride_id) return 'busy';
    if (cap.online === false) return 'offline';
    if (cap.is_live === false) return 'ghost';
    return cap.available ? 'available' : 'unavailable';
  }

  function upsertMarker(cap) {
    const id = _capKey(cap);
    if (id == null) return;
    // Preserve any earlier fields (e.g. socket had name but this update doesn't)
    const merged = Object.assign({}, captains.get(id) || {}, cap);
    captains.set(id, merged);

    const stateCls = _stateClass(merged);
    const nameText = merged.name || `#${id}`;
    const titleText = `${nameText}${merged.on_trip_ride_id ? ' · على رحلة' : ''}`;

    const existing = markers.get(id);
    if (existing) {
      const wrap = existing.content;
      wrap.title = titleText;
      wrap.querySelector('.captain-label').textContent = nameText;
      const dot = wrap.querySelector('.captain-dot');
      dot.className = 'captain-dot ' + stateCls;
      existing.position = { lat: merged.lat, lng: merged.lng };
      renderCaptainList();
      return;
    }

    const wrap = document.createElement('div');
    wrap.className = 'captain-marker-wrap';
    wrap.title = titleText;
    wrap.innerHTML = `
      <div class="captain-label">${nameText}</div>
      <div class="captain-dot ${stateCls}"></div>
    `;
    const marker = new AdvancedMarker({
      map: map,
      position: { lat: merged.lat, lng: merged.lng },
      content: wrap,
    });
    // Clicking a captain: pan to them AND open the "assign a pending trip"
    // modal so the admin can dispatch in one flow. Read the position from
    // `captains` rather than the closure — `merged` is replaced on every
    // position update, so closing over it would pan to a stale point.
    wrap.addEventListener('click', (ev) => {
      // Without this the click also reaches the map, which would consume it
      // as a pickup/dropoff pin while the create-ride modal is in click mode.
      ev.stopPropagation();
      const cur = captains.get(id);
      if (cur) panTo(cur.lat, cur.lng, 15);
      openAssignAlertModal(id);
    });
    markers.set(id, marker);
    renderCaptainList();
  }

  function removeMarker(driverId) {
    const m = markers.get(driverId);
    if (m) { m.map = null; markers.delete(driverId); }
    captains.delete(driverId);
    updateCounts();
    renderCaptainList();
  }

  function updateCounts() {
    const capCount = document.getElementById('count-captains');
    const rideCount = document.getElementById('count-rides');
    if (capCount) capCount.textContent = captains.size;
    if (rideCount) {
      const inFlight = [...rides.values()].filter(
        (r) => ['broadcasting', 'assigned', 'started'].includes(r.status)
      ).length;
      rideCount.textContent = inFlight;
    }
  }

  function renderRideList() {
    const list = document.getElementById('ride-list');
    if (!list) return;
    const inFlight = [...rides.values()]
      .filter((r) => ['broadcasting', 'assigned', 'started'].includes(r.status))
      .sort((a, b) => b.id - a.id);
    if (inFlight.length === 0) {
      list.innerHTML = '<div class="empty-note">مفيش رحلات شغالة دلوقتي.</div>';
      return;
    }
    list.innerHTML = inFlight.map((r) => {
      const statusAr = {
        broadcasting: 'بندور على كابتن',
        assigned: 'الكابتن جاي',
        started: 'في الرحلة',
      }[r.status] || r.status;
      const statusClass = `status-${r.status}`;
      const route = `${r.from_zone_ar || '—'} ← ${r.to_zone_ar || '—'}`;
      const driverBit = r.driver_name ? ` · ${r.driver_name}` : '';
      return `
        <button class="ride-row" data-driver-id="${r.driver_id || ''}">
          <div class="route">#${r.id} · ${route}
            <span class="status-pill ${statusClass}">${statusAr}</span>
          </div>
          <div class="meta">${r.source === 'whatsapp' ? '📱 واتساب' : 'تطبيق'}${driverBit}</div>
        </button>`;
    }).join('');
    list.querySelectorAll('.ride-row').forEach((btn) => {
      btn.addEventListener('click', () => {
        const did = parseInt(btn.getAttribute('data-driver-id'), 10);
        const cap = captains.get(did);
        if (cap) panTo(cap.lat, cap.lng, 15);
      });
    });
  }

  // -------- initial snapshot --------
  fetch('/live-map/data', { credentials: 'same-origin' })
    .then((r) => r.json())
    .then((data) => {
      const caps = data.captains || [];
      caps.forEach(upsertMarker);
      (data.rides || []).forEach((r) => rides.set(r.id, r));
      updateCounts();
      renderRideList();
      // Snap to captain(s) so the marker is actually in view — otherwise
      // a single captain outside Benha centre looks like "nobody online."
      if (caps.length === 1) {
        map.setCenter({ lat: caps[0].lat, lng: caps[0].lng });
        map.setZoom(14);
      } else if (caps.length > 1) {
        const bounds = new google.maps.LatLngBounds();
        caps.forEach((c) => bounds.extend({ lat: c.lat, lng: c.lng }));
        map.fitBounds(bounds, 60);
        // Google has no maxZoom option on fitBounds. Two captains parked next
        // to each other would otherwise zoom to street level and hide the rest
        // of the city, so clamp once the camera settles.
        google.maps.event.addListenerOnce(map, 'idle', () => {
          if (map.getZoom() > 14) map.setZoom(14);
        });
      }
    })
    .catch((e) => console.error('live-map snapshot failed', e));

  // -------- socket --------
  // websocket-only — see inbox.js: a polling handshake + upgrade can land
  // on different gunicorn workers and engineio rejects the sid.
  const socket = io('/inbox', { transports: ['websocket'] });

  socket.on('driver_position_update', (data) => {
    upsertMarker(data);
    updateCounts();
  });

  socket.on('driver_position_removed', (data) => {
    removeMarker(data.driver_id);
  });

  // Toggle events (Go Online / Go Offline / Available / Busy) come in
  // separately from the position stream so a stationary captain who
  // flips availability still updates the marker colour + sidebar row
  // in real time without a page refresh.
  socket.on('driver_presence_update', (data) => {
    // If lat/lng missing (e.g. Go Offline cleared the GEO index), keep
    // any existing marker position — the position_removed event will
    // handle the actual removal.
    const existing = captains.get(data.driver_id);
    if (existing && (data.lat == null || data.lng == null)) {
      data.lat = existing.lat;
      data.lng = existing.lng;
    }
    if (data.lat != null && data.lng != null) {
      upsertMarker(data);
    } else {
      // Nothing to draw and no prior state — just merge into captain map
      // so the sidebar counts stay accurate.
      const merged = Object.assign({}, existing || {}, data);
      captains.set(data.driver_id, merged);
      renderCaptainList();
    }
    updateCounts();
  });

  socket.on('ride_lifecycle_update', (payload) => {
    const ride = payload.ride;
    if (!ride) return;
    // Enrich with the driver_name we shipped alongside the ride
    if (payload.driver_name && !ride.driver_name) ride.driver_name = payload.driver_name;

    if (['completed', 'cancelled', 'cancelled_no_show'].includes(ride.status)) {
      rides.delete(ride.id);
    } else {
      rides.set(ride.id, ride);
    }
    renderRideList();
    updateCounts();

    // If a captain's on-trip status flipped, refresh their marker colour.
    if (ride.driver_id) {
      const cap = captains.get(ride.driver_id);
      if (cap) {
        cap.on_trip_ride_id = ['assigned', 'started'].includes(ride.status) ? ride.id : null;
        upsertMarker(cap);
      }
    }
  });

  socket.on('connect', () => console.log('live-map socket connected'));
  socket.on('disconnect', () => console.log('live-map socket disconnected'));

  // ============================================================
  // Create-ride modal (walk-in / phone-in)
  // ============================================================
  const crModal = document.getElementById('create-ride-modal');
  const crMapMode = document.getElementById('cr-map-mode');
  const crPickupPreview = document.getElementById('cr-pickup-preview');
  const crDropoffPreview = document.getElementById('cr-dropoff-preview');
  const crPriceHint = document.getElementById('cr-price-hint');
  const crNearestList = document.getElementById('cr-nearest-list');

  // In-modal state
  const crState = {
    pickup: null,   // {lat, lng, label}
    dropoff: null,
    clickMode: null,  // 'pickup' | 'dropoff' | null
    pickupMarker: null,
    dropoffMarker: null,
    quoteTimer: null,
    nearestTimer: null,
  };

  window.openCreateRideModal = function () {
    crModal.classList.add('open');
  };
  window.closeCreateRideModal = function () {
    crModal.classList.remove('open');
    setCrClickMode(null);
    // Keep the entered form + pins around in case admin reopens.
  };

  window.setCrClickMode = function (mode) {
    crState.clickMode = mode;
    if (mode === 'pickup') {
      crMapMode.textContent = 'دوس على الخريطة لتحديد نقطة الاستلام';
      crMapMode.classList.add('open');
      crModal.classList.remove('open');   // let admin see the map
    } else if (mode === 'dropoff') {
      crMapMode.textContent = 'دوس على الخريطة لتحديد الوجهة';
      crMapMode.classList.add('open');
      crModal.classList.remove('open');
    } else {
      crMapMode.classList.remove('open');
    }
  };

  // Map click handler: only active when user tapped one of the "دوس على الخريطة" buttons
  map.addListener('click', async (e) => {
    if (!crState.clickMode) return;
    if (!e.latLng) return;
    const lat = e.latLng.lat();
    const lng = e.latLng.lng();
    const label = await _reverseGeocode(lat, lng);
    _setLocation(crState.clickMode, { lat, lng, label });
    setCrClickMode(null);
    crModal.classList.add('open');
  });

  async function _reverseGeocode(lat, lng) {
    // Best-effort — if it fails, show coords as label.
    try {
      const r = await fetch(`/live-map/place-name?lat=${lat}&lng=${lng}`, {
        credentials: 'same-origin',
      });
      if (r.ok) {
        const j = await r.json();
        if (j && j.label) return j.label;
      }
    } catch (_) {}
    return `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
  }

  function _setLocation(kind, loc) {
    crState[kind] = loc;
    const el = kind === 'pickup' ? crPickupPreview : crDropoffPreview;
    el.classList.add('set');
    el.innerHTML = `<span>📍 ${escapeHtml(loc.label)}</span>
                    <span style="color:#9aa0aa;font-size:11px;">${loc.lat.toFixed(4)}, ${loc.lng.toFixed(4)}</span>`;
    _updatePin(kind);
    _refreshQuote();
    if (kind === 'pickup') _refreshNearest();
  }

  function _updatePin(kind) {
    const loc = crState[kind];
    if (!loc) return;
    const existing = kind === 'pickup' ? crState.pickupMarker : crState.dropoffMarker;
    if (existing) existing.map = null;
    const el = document.createElement('div');
    el.className = 'center-pin';
    el.style.cssText = `width:20px;height:20px;border-radius:50%;background:${kind === 'pickup' ? '#22c55e' : '#ef4444'};border:3px solid #fff;box-shadow:0 0 0 2px rgba(0,0,0,0.4);`;
    const marker = new AdvancedMarker({
      map: map,
      position: { lat: loc.lat, lng: loc.lng },
      content: el,
    });
    if (kind === 'pickup') crState.pickupMarker = marker;
    else crState.dropoffMarker = marker;
  }

  function _refreshQuote() {
    clearTimeout(crState.quoteTimer);
    if (!crState.pickup || !crState.dropoff) { crPriceHint.textContent = ''; return; }
    crState.quoteTimer = setTimeout(async () => {
      try {
        const { pickup: p, dropoff: d } = crState;
        const r = await fetch(
          `/live-map/quote?pickup_lat=${p.lat}&pickup_lng=${p.lng}&dropoff_lat=${d.lat}&dropoff_lng=${d.lng}`,
          { credentials: 'same-origin' },
        );
        if (!r.ok) { crPriceHint.textContent = ''; return; }
        const j = await r.json();
        crPriceHint.textContent = `السعر المقترح: ${Math.round(j.price_egp)} ج.م (المسافة ${j.distance_km.toFixed(1)} كم)`;
      } catch (_) { crPriceHint.textContent = ''; }
    }, 400);
  }

  function _refreshNearest() {
    clearTimeout(crState.nearestTimer);
    if (!crState.pickup) {
      crNearestList.innerHTML = '<div class="empty-note" style="padding:12px 0;">حدد نقطة الاستلام الأول.</div>';
      return;
    }
    crNearestList.innerHTML = '<div class="empty-note" style="padding:12px 0;">بنبحث…</div>';
    crState.nearestTimer = setTimeout(async () => {
      try {
        const { lat, lng } = crState.pickup;
        const r = await fetch(`/live-map/nearest-captains?lat=${lat}&lng=${lng}&limit=6`, { credentials: 'same-origin' });
        const rows = await r.json();
        _renderNearest(rows);
      } catch (e) {
        crNearestList.innerHTML = '<div class="empty-note" style="padding:12px 0;">حصل خطأ في البحث.</div>';
      }
    }, 300);
  }

  function _renderNearest(rows) {
    if (!rows || rows.length === 0) {
      crNearestList.innerHTML = '<div class="empty-note" style="padding:12px 0;">مفيش كباتن متاحين في المنطقة دي دلوقتي.</div>';
      return;
    }
    crNearestList.innerHTML = rows.map((c) => {
      const car = [c.car_model, c.car_color].filter(Boolean).join(' · ') || '—';
      return `
        <div class="cr-nearest-row">
          <div class="info">
            <div class="n">🟢 ${escapeHtml(c.name || '#' + c.driver_id)} · ${c.distance_km.toFixed(1)} كم</div>
            <div class="m">${escapeHtml(car)}${c.car_plate ? ' · ' + escapeHtml(c.car_plate) : ''}</div>
          </div>
          <button class="lm-btn" data-driver="${c.driver_id}">اسند</button>
        </div>`;
    }).join('');
    crNearestList.querySelectorAll('.lm-btn').forEach((btn) => {
      btn.addEventListener('click', () => submitCreateRide(parseInt(btn.getAttribute('data-driver'), 10), btn));
    });
  }

  async function submitCreateRide(driverId, btn) {
    const waId = document.getElementById('cr-wa-id').value.trim();
    if (!waId) { alert('اكتب رقم العميل الأول'); return; }
    if (!crState.pickup || !crState.dropoff) { alert('حدد نقطة الاستلام والوجهة الأول'); return; }
    const priceRaw = document.getElementById('cr-price').value.trim();
    const price = priceRaw ? parseFloat(priceRaw) : null;
    const name = document.getElementById('cr-name').value.trim() || null;

    btn.disabled = true; btn.textContent = 'جاري…';
    try {
      const resp = await fetch('/live-map/create-ride', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          customer_wa_id: waId,
          customer_name: name,
          pickup_lat: crState.pickup.lat,
          pickup_lng: crState.pickup.lng,
          dropoff_lat: crState.dropoff.lat,
          dropoff_lng: crState.dropoff.lng,
          price_egp: price,
          driver_id: driverId,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        alert(data.message || ('فشل: ' + (data.error || resp.status)));
        btn.disabled = false; btn.textContent = 'اسند';
        return;
      }
      const okMsg = data.customer_notified
        ? 'الرحلة اتسندت والعميل اتبعتله واتساب.'
        : 'الرحلة اتسندت للكابتن (مقدرناش نبعت واتساب للعميل).';
      alert(okMsg);
      // Clear the form + pins for the next walk-in.
      _resetCreateRideForm();
      closeCreateRideModal();
    } catch (e) {
      alert('حصل خطأ: ' + e);
      btn.disabled = false; btn.textContent = 'اسند';
    }
  }

  function _resetCreateRideForm() {
    document.getElementById('cr-wa-id').value = '';
    document.getElementById('cr-name').value = '';
    document.getElementById('cr-price').value = '';
    document.getElementById('cr-pickup-search').value = '';
    document.getElementById('cr-dropoff-search').value = '';
    crPriceHint.textContent = '';
    crPickupPreview.classList.remove('set');
    crPickupPreview.textContent = '— لسه محدد —';
    crDropoffPreview.classList.remove('set');
    crDropoffPreview.textContent = '— لسه محدد —';
    if (crState.pickupMarker) { crState.pickupMarker.map = null; crState.pickupMarker = null; }
    if (crState.dropoffMarker) { crState.dropoffMarker.map = null; crState.dropoffMarker = null; }
    crState.pickup = null;
    crState.dropoff = null;
    crNearestList.innerHTML = '<div class="empty-note" style="padding:12px 0;">حدد نقطة الاستلام الأول.</div>';
  }

  // Nominatim search inside the create-ride modal (mirrors the map-top
  // search but writes into the pickup/dropoff fields instead of pinning).
  ['pickup', 'dropoff'].forEach((kind) => {
    const input = document.getElementById(`cr-${kind}-search`);
    const results = document.getElementById(`cr-${kind}-results`);
    let t = null;
    input.addEventListener('input', () => {
      clearTimeout(t);
      const q = input.value.trim();
      if (q.length < 3) { results.classList.remove('open'); return; }
      t = setTimeout(async () => {
        try {
          const r = await fetch(`/live-map/search-places?q=${encodeURIComponent(q)}`, { credentials: 'same-origin' });
          const list = await r.json();
          _renderCrSearch(kind, results, list);
        } catch (_) { results.classList.remove('open'); }
      }, 300);
    });
  });

  function _renderCrSearch(kind, results, list) {
    if (!list || list.length === 0) {
      results.innerHTML = '<div class="row" style="color:#9aa0aa;">مفيش نتايج</div>';
      results.classList.add('open');
      return;
    }
    results.innerHTML = list.map((p, i) =>
      `<div class="row" data-i="${i}">${escapeHtml(p.label)}</div>`
    ).join('');
    results.classList.add('open');
    results.querySelectorAll('.row').forEach((row, i) => {
      row.addEventListener('click', () => {
        const p = list[i];
        _setLocation(kind, { lat: p.lat, lng: p.lng, label: p.label });
        results.classList.remove('open');
        document.getElementById(`cr-${kind}-search`).value = p.label;
      });
    });
  }

  // ============================================================
  // Place search — floating input top-left of the map. Debounced
  // 350ms → hits /live-map/search-places (admin-session-auth proxy
  // over Nominatim). Selecting a result drops a temporary red pin
  // and flies to it.
  // ============================================================
  let _searchTimer = null;
  let _searchMarker = null;
  const searchInput = document.getElementById('place-search-input');
  const searchResults = document.getElementById('place-results');

  searchInput.addEventListener('input', () => {
    const q = searchInput.value.trim();
    clearTimeout(_searchTimer);
    if (q.length < 3) { searchResults.classList.remove('open'); searchResults.innerHTML = ''; return; }
    _searchTimer = setTimeout(() => runPlaceSearch(q), 350);
  });

  async function runPlaceSearch(q) {
    try {
      const r = await fetch(`/live-map/search-places?q=${encodeURIComponent(q)}`, { credentials: 'same-origin' });
      const results = await r.json();
      renderPlaceResults(results);
    } catch (e) {
      searchResults.innerHTML = '<div class="place-result-row">حصل خطأ في البحث</div>';
      searchResults.classList.add('open');
    }
  }

  function renderPlaceResults(results) {
    if (!results || results.length === 0) {
      searchResults.innerHTML = '<div class="place-result-row" style="color:#9aa0aa">مفيش نتايج</div>';
      searchResults.classList.add('open');
      return;
    }
    searchResults.innerHTML = results.map((p, i) =>
      `<div class="place-result-row" data-i="${i}">${escapeHtml(p.label)}</div>`
    ).join('');
    searchResults.classList.add('open');
    searchResults.querySelectorAll('.place-result-row').forEach((row, i) => {
      row.addEventListener('click', () => {
        const p = results[i];
        panTo(p.lat, p.lng, 16);
        if (_searchMarker) _searchMarker.map = null;
        const el = document.createElement('div');
        el.className = 'center-pin';
        el.style.cssText = 'width:22px;height:22px;border-radius:50%;background:#ef4444;border:3px solid #fff;box-shadow:0 0 0 2px rgba(0,0,0,0.4);';
        _searchMarker = new AdvancedMarker({
          map: map,
          position: { lat: p.lat, lng: p.lng },
          content: el,
        });
        searchResults.classList.remove('open');
        searchInput.value = p.label;
      });
    });
  }

  function escapeHtml(s) {
    return (s || '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // Close place-results when clicking elsewhere.
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.place-search-wrap')) {
      searchResults.classList.remove('open');
    }
  });

  // ============================================================
  // Captain list + filter in the sidebar
  // ============================================================
  const captainListEl = document.getElementById('captain-list');
  const captainCountEl = document.getElementById('captains-shown');
  const captainSearchInput = document.getElementById('captain-search-input');
  let _captainQuery = '';

  captainSearchInput.addEventListener('input', () => {
    _captainQuery = captainSearchInput.value.trim().toLowerCase();
    renderCaptainList();
  });

  function renderCaptainList() {
    if (!captainListEl) return;
    const all = [...captains.values()].filter((c) => c && (c.lat != null));
    const filtered = _captainQuery
      ? all.filter((c) => {
          const hay = `${c.name || ''} ${c.wa_id || ''}`.toLowerCase();
          return hay.includes(_captainQuery);
        })
      : all;
    filtered.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    captainCountEl.textContent = filtered.length;
    if (filtered.length === 0) {
      captainListEl.innerHTML = '<div class="empty-note">مفيش كباتن مطابقين.</div>';
      return;
    }
    captainListEl.innerHTML = filtered.map((c) => {
      const stateCls = _stateClass(c);
      const stateLabel = c.on_trip_ride_id ? 'على رحلة'
        : (c.online === false ? 'مش شغال'
          : (c.is_live === false ? 'مش متصل'
          : (c.available ? 'متاح' : 'مشغول')));
      const id = _capKey(c);
      return `
        <button class="captain-row" data-id="${id}">
          <div>
            <div class="name">${escapeHtml(c.name || `#${id}`)}</div>
            <div class="meta">${escapeHtml(c.wa_id || '')} · ${stateLabel}</div>
          </div>
          <div class="state-dot ${stateCls}"></div>
        </button>`;
    }).join('');
    captainListEl.querySelectorAll('.captain-row').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = parseInt(btn.getAttribute('data-id'), 10);
        const cap = captains.get(id);
        if (cap) panTo(cap.lat, cap.lng, 15);
        openAssignAlertModal(id);
      });
    });
  }

  // ============================================================
  // Assign a pending no_driver alert to a specific captain
  // ============================================================
  const assignModal = document.getElementById('assign-alert-modal');
  const assignAlertList = document.getElementById('assign-alert-list');
  const assignAlertTitle = document.getElementById('assign-alert-title');
  let _assignTargetDriverId = null;

  window.openAssignAlertModal = async function (driverId) {
    _assignTargetDriverId = driverId;
    const cap = captains.get(driverId);
    assignAlertTitle.textContent = `اسند رحلة لـ ${cap?.name || '#' + driverId}`;
    assignAlertList.innerHTML = '<div class="empty-note">بنحمل الرحلات المعلقة…</div>';
    assignModal.classList.add('open');
    try {
      const r = await fetch('/live-map/pending-alerts', { credentials: 'same-origin' });
      const alerts = await r.json();
      renderAlertPicker(alerts);
    } catch (e) {
      assignAlertList.innerHTML = '<div class="empty-note">حصل خطأ. جرب تاني.</div>';
    }
  };

  window.closeAssignAlertModal = function () {
    assignModal.classList.remove('open');
    _assignTargetDriverId = null;
  };

  function renderAlertPicker(alerts) {
    if (!alerts || alerts.length === 0) {
      assignAlertList.innerHTML = '<div class="empty-note">مفيش رحلات معلقة دلوقتي.</div>';
      return;
    }
    assignAlertList.innerHTML = alerts.map((a) => {
      const route = `${a.pickup_address || a.from_zone_ar || '—'} ← ${a.dropoff_address || a.to_zone_ar || '—'}`;
      const cust = a.customer_name ? `${escapeHtml(a.customer_name)}` : '';
      const price = a.price_egp ? `${a.price_egp.toFixed(0)} ج.م` : 'السعر بعد الوصول';
      return `
        <div class="alert-row">
          <div style="flex:1;">
            <div class="route">#${a.ride_id} · ${escapeHtml(route)}</div>
            <div class="meta">${cust} · ${price}</div>
          </div>
          <button class="lm-btn" data-alert="${a.alert_id}"
                  data-from="${a.from_zone_id || ''}"
                  data-to="${a.to_zone_id || ''}"
                  data-price="${a.price_egp || ''}">
            اسند
          </button>
        </div>`;
    }).join('');
    assignAlertList.querySelectorAll('.lm-btn').forEach((btn) => {
      btn.addEventListener('click', () => submitAlertAssign(btn));
    });
  }

  async function submitAlertAssign(btn) {
    if (!_assignTargetDriverId) return;
    const alertId = parseInt(btn.getAttribute('data-alert'), 10);
    const from = parseInt(btn.getAttribute('data-from'), 10);
    const toRaw = btn.getAttribute('data-to');
    const to = toRaw ? parseInt(toRaw, 10) : null;
    const priceRaw = btn.getAttribute('data-price');
    const price = priceRaw ? parseFloat(priceRaw) : null;
    btn.disabled = true; btn.textContent = 'جاري…';
    try {
      const resp = await fetch(`/alerts/${alertId}/assign`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          from_zone_id: from,
          to_zone_id: to,
          price_egp: price,
          driver_id: _assignTargetDriverId,
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        alert(err.message || ('فشل: ' + (err.error || resp.status)));
        btn.disabled = false; btn.textContent = 'اسند';
        return;
      }
      const data = await resp.json().catch(() => ({}));
      closeAssignAlertModal();
      if (data.customer_notified === false) {
        alert('اتسند بنجاح، بس مقدرناش نبعت واتساب للعميل. اتواصل معاه يدوياً.');
      } else {
        alert('اتسندت الرحلة والعميل اتبعتله واتساب بمعلومات الكابتن.');
      }
    } catch (e) {
      alert('حصل خطأ: ' + e);
      btn.disabled = false; btn.textContent = 'اسند';
    }
  }
};
