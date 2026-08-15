"""Stress test for the office-dispatch hot path.

Fires N simulated office pastes in parallel eventlet greenlets against
a fleet of M seeded drivers. Measures per-message wall-clock latency,
Postgres query count, and Redis call count. Outbound WhatsApp + FCM are
stubbed to no-ops so nothing real leaves the box.

Usage:
  .venv/bin/python -m ops.stress_office --messages 50 --drivers 300

Runs entirely local — SQLite + fakeredis, isolated from prod.
"""
from __future__ import annotations

# Monkey-patch FIRST — anything socket/thread aware in the imports below must
# see the green primitives, otherwise eventlet.spawn_n will silently run
# things on the OS thread and Redis pipelines will block the world.
import eventlet
eventlet.monkey_patch()

import argparse
import os
import random
import statistics
import time


def _seed(app, db, drivers_n: int):
    from app.models.driver import Driver

    # Randomize positions across a ~10km box centred on Benha (lat 30.46, lng 31.18).
    for i in range(1, drivers_n + 1):
        d = Driver(
            name=f"D{i}",
            wa_id=f"20100000{i:04d}",
            password_hash="stub",
            is_active=True,
            fcm_token=f"tok_{i}",
            latitude=30.46 + (random.random() - 0.5) * 0.09,
            longitude=31.18 + (random.random() - 0.5) * 0.09,
            service_kind="private",
        )
        d.approval_status = "approved"
        db.session.add(d)
    db.session.commit()


def _ensure_office(db):
    """201050084115 gets seeded by boot; only insert if missing."""
    from app.models.office import OfficeNumber
    if OfficeNumber.query.filter_by(wa_id="201050084115").first():
        return
    row = OfficeNumber(wa_id="201050084115", label="stress-test", is_active=True)
    db.session.add(row)
    db.session.commit()


def _stub_outbound():
    """Replace every outbound WhatsApp / FCM send with a counter."""
    from app.services import whatsapp
    from app.services import push_notifications as pn

    counts = {"whatsapp": 0, "fcm": 0}

    def _fake_send_text(*_a, **_k):
        counts["whatsapp"] += 1
        return {"messages": [{"id": "stub"}]}

    def _fake_send(*_a, **_k):
        counts["fcm"] += 1
        return True

    def _fake_send_to_drivers(driver_ids, **_k):
        counts["fcm"] += len(driver_ids or [])
        return len(driver_ids or [])

    whatsapp.send_text = _fake_send_text
    pn._send = _fake_send
    pn.send_to_customer = _fake_send
    pn.send_to_driver = _fake_send
    pn.send_to_drivers = _fake_send_to_drivers
    return counts


def _instrument_db_calls(db):
    """Count SQLAlchemy queries across the run using SQL events."""
    from sqlalchemy import event
    counter = {"n": 0}

    @event.listens_for(db.engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, ctx, executemany):
        counter["n"] += 1

    return counter


def _instrument_matching():
    """Stub _office_round to skip the accept wait so the ladder cycles
    through all rings fast — otherwise a single message would block for
    3.5 minutes waiting for a captain that will never accept."""
    from app.services import matching

    def _no_winner(ride, picked, tried, r, window_s):
        tried.update(picked)
        return None

    matching._office_round = _no_winner


def _one_message(app, idx: int) -> tuple[int, float, bool]:
    """Fire one office paste. Returns (idx, seconds, ok)."""
    from app.services import office_dispatch
    body = f"01050084{115 + idx:03d} كفر سعد"
    t0 = time.time()
    try:
        with app.app_context():
            office_dispatch.handle_office_message("201050084115", body)
        return idx, time.time() - t0, True
    except Exception as e:  # noqa: BLE001
        print(f"  message #{idx} failed: {e!r}")
        return idx, time.time() - t0, False


def run(messages: int, drivers: int) -> None:
    os.environ["DATABASE_URL"] = "sqlite:////tmp/wassalny_stress.db"
    if os.path.exists("/tmp/wassalny_stress.db"):
        os.remove("/tmp/wassalny_stress.db")
    # Force fakeredis so this never touches a real Redis by accident.
    os.environ.pop("REDIS_URL", None)

    from app import create_app, db
    app = create_app()
    with app.app_context():
        _seed(app, db, drivers)
        _ensure_office(db)

    counts = _stub_outbound()
    _instrument_matching()
    with app.app_context():
        db_counter = _instrument_db_calls(db)

    print(f"\n=== Firing {messages} concurrent office pastes at {drivers} drivers ===\n")
    t0 = time.time()
    pool = eventlet.GreenPool(size=messages)
    results = list(pool.imap(lambda i: _one_message(app, i), range(messages)))
    wall = time.time() - t0

    ok = [r for r in results if r[2]]
    fail = [r for r in results if not r[2]]
    latencies = sorted(r[1] for r in ok)

    print(f"Wall-clock (all {messages} messages): {wall:.2f}s")
    print(f"Succeeded: {len(ok)}  Failed: {len(fail)}")
    if latencies:
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]
        print(f"Per-message latency: min={latencies[0]:.3f}s  "
              f"p50={p50:.3f}s  p95={p95:.3f}s  p99={p99:.3f}s  "
              f"max={latencies[-1]:.3f}s")
    print()
    print(f"Postgres queries executed : {db_counter['n']}  "
          f"({db_counter['n'] / max(messages, 1):.1f} per message)")
    print(f"WhatsApp sends (stubbed)  : {counts['whatsapp']}")
    print(f"FCM sends (stubbed)       : {counts['fcm']}  "
          f"({counts['fcm'] / max(messages, 1):.1f} per message)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=50)
    ap.add_argument("--drivers", type=int, default=300)
    args = ap.parse_args()
    run(args.messages, args.drivers)
