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
  const { maptilerKey, benhaCenter } = window.WASSALNY || {};

  // -------- state --------
  const markers = new Map();   // driver_id -> maplibregl.Marker
  const captains = new Map();  // driver_id -> {lat, lng, available, on_trip_ride_id, name}
  const rides = new Map();     // ride_id -> ride payload

  // -------- map --------
  const map = new maplibregl.Map({
    container: 'live-map',
    style: maptilerKey
      ? `https://api.maptiler.com/maps/streets-v2/style.json?key=${maptilerKey}`
      : {
          version: 8,
          sources: {
            osm: {
              type: 'raster',
              tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
              tileSize: 256,
              attribution: '© OpenStreetMap contributors',
            },
          },
          layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
        },
    center: benhaCenter,
    zoom: 12,
    attributionControl: true,
  });

  map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), 'top-left');

  // -------- helpers --------
  function upsertMarker(cap) {
    captains.set(cap.driver_id, cap);
    const existing = markers.get(cap.driver_id);
    const el = document.createElement('div');
    el.className = 'captain-marker ' + (
      cap.on_trip_ride_id ? 'busy' : (cap.available ? 'available' : 'unavailable')
    );
    el.title = `${cap.name || cap.driver_id}` + (cap.on_trip_ride_id ? ' · على رحلة' : '');

    if (existing) {
      existing.getElement().className = el.className;
      existing.getElement().title = el.title;
      existing.setLngLat([cap.lng, cap.lat]);
      return;
    }
    const marker = new maplibregl.Marker({ element: el })
      .setLngLat([cap.lng, cap.lat])
      .addTo(map);
    marker.getElement().addEventListener('click', () => {
      map.flyTo({ center: [cap.lng, cap.lat], zoom: 15, duration: 800 });
    });
    markers.set(cap.driver_id, marker);
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
      (data.captains || []).forEach(upsertMarker);
      (data.rides || []).forEach((r) => rides.set(r.id, r));
      updateCounts();
      renderRideList();
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
