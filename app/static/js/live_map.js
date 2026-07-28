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
    return cap.on_trip_ride_id
      ? 'busy'
      : cap.available ? 'available' : 'unavailable';
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
    wrap.addEventListener('click', () => {
      map.flyTo({ center: [merged.lng, merged.lat], zoom: 15, duration: 800 });
    });
    markers.set(id, marker);
  }

  function removeMarker(driverId) {
    const m = markers.get(driverId);
    if (m) { m.remove(); markers.delete(driverId); }
    captains.delete(driverId);
    updateCounts();
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
})();
