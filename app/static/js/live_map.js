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
(function () {
  const { benhaCenter } = window.WASSALNY || { benhaCenter: [31.1836, 30.4560] };

  // -------- state --------
  const markers = new Map();   // driver_id -> maplibregl.Marker
  const captains = new Map();  // driver_id -> {lat, lng, available, on_trip_ride_id, name}
  const rides = new Map();     // ride_id -> ride payload

  // -------- map --------
  // Use raster OSM tiles — free, no API key, no HTTP-referrer allowlist to
  // maintain. Same reasoning as the Flutter captain app: reliability > fancy
  // vector tiles.
  const map = new maplibregl.Map({
    container: 'live-map',
    style: {
      version: 8,
      sources: {
        osm: {
          type: 'raster',
          tiles: [
            'https://a.tile.openstreetmap.org/{z}/{x}/{y}.png',
            'https://b.tile.openstreetmap.org/{z}/{x}/{y}.png',
            'https://c.tile.openstreetmap.org/{z}/{x}/{y}.png',
          ],
          tileSize: 256,
          minzoom: 0,
          maxzoom: 19,
          attribution: '© OpenStreetMap contributors',
        },
      },
      layers: [{ id: 'osm', type: 'raster', source: 'osm', minzoom: 0, maxzoom: 22 }],
    },
    center: benhaCenter,
    zoom: 12,
    attributionControl: true,
  });

  map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), 'top-left');

  // -------- helpers --------
  function _capKey(cap) {
    // Accept either shape — /live-map/data snapshot emits `driver_id`
    // (post-fix) but old cached responses / other sockets may still send
    // `id`. Fall back so nothing gets keyed by undefined.
    return cap.driver_id != null ? cap.driver_id : cap.id;
  }

  function _stateClass(cap) {
    // Priority: on-trip > offline-but-known-position > online-but-unavailable > available.
    // "offline-known" means Redis has a GEO coord for them but presence.online is
    // false — usually a captain that opened the app + granted GPS but hasn't
    // tapped "Go Online" yet. Rendering them in red makes the mismatch obvious.
    if (cap.on_trip_ride_id) return 'busy';
    if (cap.online === false) return 'offline';
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
      const wrap = existing.getElement();
      wrap.title = titleText;
      wrap.querySelector('.captain-label').textContent = nameText;
      const dot = wrap.querySelector('.captain-dot');
      dot.className = 'captain-dot ' + stateCls;
      existing.setLngLat([merged.lng, merged.lat]);
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
    const marker = new maplibregl.Marker({ element: wrap, anchor: 'bottom' })
      .setLngLat([merged.lng, merged.lat])
      .addTo(map);
    // Clicking a captain: fly to them AND open the "assign a pending trip"
    // modal so the admin can dispatch in one flow.
    wrap.addEventListener('click', () => {
      map.flyTo({ center: [merged.lng, merged.lat], zoom: 15, duration: 600 });
      openAssignAlertModal(id);
    });
    markers.set(id, marker);
    renderCaptainList();
  }

  function removeMarker(driverId) {
    const m = markers.get(driverId);
    if (m) { m.remove(); markers.delete(driverId); }
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
        if (cap) {
          map.flyTo({ center: [cap.lng, cap.lat], zoom: 15, duration: 800 });
        }
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
        map.jumpTo({ center: [caps[0].lng, caps[0].lat], zoom: 14 });
      } else if (caps.length > 1) {
        const bounds = caps.reduce(
          (b, c) => b.extend([c.lng, c.lat]),
          new maplibregl.LngLatBounds([caps[0].lng, caps[0].lat], [caps[0].lng, caps[0].lat]),
        );
        map.fitBounds(bounds, { padding: 60, duration: 0, maxZoom: 14 });
      }
    })
    .catch((e) => console.error('live-map snapshot failed', e));

  // -------- socket --------
  const socket = io('/inbox');

  socket.on('driver_position_update', (data) => {
    upsertMarker(data);
    updateCounts();
  });

  socket.on('driver_position_removed', (data) => {
    removeMarker(data.driver_id);
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
        map.flyTo({ center: [p.lng, p.lat], zoom: 16, duration: 700 });
        if (_searchMarker) _searchMarker.remove();
        const el = document.createElement('div');
        el.style.cssText = 'width:22px;height:22px;border-radius:50%;background:#ef4444;border:3px solid #fff;box-shadow:0 0 0 2px rgba(0,0,0,0.4);';
        _searchMarker = new maplibregl.Marker({ element: el, anchor: 'center' })
          .setLngLat([p.lng, p.lat]).addTo(map);
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
        : (c.online === false ? 'مش شغال' : (c.available ? 'متاح' : 'مشغول'));
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
        if (cap) map.flyTo({ center: [cap.lng, cap.lat], zoom: 15, duration: 600 });
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
        alert('فشل: ' + (err.error || resp.status));
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
})();
