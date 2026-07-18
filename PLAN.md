# Wassalny (Waslni Banha) — Full Product & Engineering Plan

**Owner:** Ibrahim Fakhry
**Location:** Banha, Qalyubia, Egypt
**Business type:** 24/7 ride-hailing platform
**Scale target:** 500+ captains, 10,000+ customers
**Timeline:** 2–3 months to full production launch
**Last updated:** 2026-07-18 (v4 — exact design-system colors extracted from PNGs into §3.5; all 59 design images saved locally in `design_reference/`; state machine + §8 ordering fixed)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Business Context](#2-business-context)
3. [UI Design Reference](#3-ui-design-reference)
4. [Confirmed Product Decisions](#4-confirmed-product-decisions)
5. [Tech Stack](#5-tech-stack)
6. [High-Level Architecture](#6-high-level-architecture)
7. [Data Model](#7-data-model)
8. [Core Systems](#8-core-systems)
9. [Booking Flows (End-to-End)](#9-booking-flows-end-to-end)
10. [Trip State Machine](#10-trip-state-machine)
11. [Matching Algorithm](#11-matching-algorithm)
12. [WhatsApp AI Flow (Gemini)](#12-whatsapp-ai-flow-gemini)
13. [Admin Dashboard](#13-admin-dashboard)
14. [Mobile Apps (Flutter)](#14-mobile-apps-flutter)
15. [Non-Functional Requirements](#15-non-functional-requirements)
16. [Failure Scenarios & Handling](#16-failure-scenarios--handling)
17. [12-Week Timeline](#17-12-week-timeline)
18. [Cost Breakdown](#18-cost-breakdown)
19. [Risks & Mitigations](#19-risks--mitigations)
20. [Success Metrics / KPIs](#20-success-metrics--kpis)
21. [Open Questions](#21-open-questions)
22. [Post-Launch Roadmap](#22-post-launch-roadmap)

---

## 1. Executive Summary

Wassalny is a ride-hailing platform for Benha (Qalyubia governorate, Egypt) built around **two booking channels feeding one backend**:

1. **WhatsApp channel** — customer sends an Arabic message like _"عايز عربية في الرملة"_ → Google Gemini AI parses pickup + destination → backend matches a captain → confirmation sticker sent back.
2. **Mobile app channel** — customer opens the Flutter app, picks pickup and destination from a **typed neighborhood list (no maps)** → backend runs the same matching logic.

Captains use a Flutter app to toggle availability, report their current zone, accept trip broadcasts, and mark trip states. The admin dashboard is the operations control room (live monitoring, alerts, manual intervention when the AI can't parse, and full CRUD for zones/prices/team).

Non-negotiable requirements: **sub-200 ms interactions, atomic driver reservation to prevent double-booking, and reliability during rush hour**.

---

## 2. Business Context

- **Business name:** Wassalny (Waslni Banha)
- **Tagline:** "أقرب كابتن هيكلمك" — the closest captain will contact you
- **Operating area:** Benha (Al Qalyubia governorate), Egypt
- **Hours:** 24/7
- **Payment:** Cash only at launch
- **Commission model:** Wassalny takes 15 % of every fare; captain keeps 85 %
- **Reference pricing** (from designs): 15 EGP flag + 4.5 EGP/km — but we're moving to a **zone-to-zone fixed price table** instead of distance because we don't use maps
- **Coverage:** Fixed list of Benha neighborhoods — **5 zones at launch** (Ramla, Downtown, University, Sarayat, Damanhour Road), expand to full ~30–50 list before real public launch. See [Appendix C](#appendix-c--seed-data-phase-1-starter)
- **Contact number:** 01029188887

### Why "no maps"

The founder explicitly chose a **type-your-place** experience rather than a map-based one. This is unusual for ride-hailing but it fits the local reality of Benha:

- Everyone already knows the local neighborhood names by heart
- Faster booking (no waiting for maps to load, no pin-dragging)
- Works on cheap phones with weak connections
- Removes Google Maps API costs
- Removes GPS accuracy issues in narrow streets

This choice pushes complexity into the **zone/pricing configuration** — but that's a one-time setup by the admin.

---

## 3. UI Design Reference

The full UI/UX design specification is published at:

> **🎨 https://ibrahimfakhrey.github.io/wasalny/**

The design covers **57 screens across 4 sections**. All backend endpoints and admin flows in this plan are built to feed the exact screens shown at that URL. Any developer working on the mobile apps must open the URL first as the source of truth.

### Section A — Customer App (14 screens)

| # | Screen | Backend endpoint(s) needed |
|---|---|---|
| 1 | Splash (orange logo on navy-black) | — |
| 2–4 | 3-slide onboarding | — |
| 5 | Login with +20 phone | `POST /api/v1/customer/login` |
| 6 | ~~OTP verification~~ (skipped — phone-only per decision) | — |
| 7 | Home (interactive area, no map) | `GET /api/v1/zones` |
| 8 | Pickup neighborhood picker | `GET /api/v1/zones` |
| 9 | Destination neighborhood picker | `GET /api/v1/zones` |
| 10 | Trip summary (fixed price, ETA estimate) | `POST /api/v1/rides/quote` |
| 11 | Live driver search animation (orange radar) | Socket.IO `broadcast_started` |
| 12 | Driver assigned card (photo, plate, rating) | Socket.IO `trip_assigned` |
| 13 | Real-time trip status + SOS button | Socket.IO `trip_status_changed` |
| 14 | Post-trip rating (5 stars + comment) | `POST /api/v1/rides/{id}/rate` |
| — | Trip history | `GET /api/v1/customer/rides` |
| — | Profile | `GET/PATCH /api/v1/customer/me` |
| — | In-app chat with quick replies | Socket.IO `message` |
| — | Notification center | `GET /api/v1/customer/notifications` |

### Section B — Captain App (simplified — no self-registration)

**⚠️ Decision #13:** Captains are **admin-created only**. Admin uploads all documents in the dashboard and generates login credentials. Captain-facing registration screens (2–5 in the original design) are removed. The captain just downloads the app and logs in.

| # | Screen | Backend endpoint(s) needed |
|---|---|---|
| 1 | Login (phone + admin-issued password) — design screen 1 | `POST /api/v1/driver/login` |
| 2 | Home: online toggle, daily earnings, trips count, current zone — design screen 6 | `POST /api/v1/driver/status`, WebSocket heartbeat |
| 3 | Trip request popup (35-sec countdown) — design screen 7 | Socket.IO `trip_offered` |
| 4 | Navigation to pickup (typed address, no map) — design screen 8 | `GET /api/v1/rides/{id}` |
| 5 | Trip start button (no turn-by-turn) — design screen 9 | `POST /api/v1/rides/{id}/start` |
| 6 | Trip completion + net profit after commission — design screen 10 | `POST /api/v1/rides/{id}/complete` |
| 6b | No-show button — captain arrived, customer absent (new, not in design) | `POST /api/v1/rides/{id}/no-show` |
| 7 | Weekly earnings dashboard + graph — design screen 11 | `GET /api/v1/driver/earnings` |
| 8 | Driver profile (342 trips, 4.8 rating, gold tier) — design screen 12 | `GET /api/v1/driver/me` |
| 9 | Real-time chat with quick responses — design screen 13 | Socket.IO `message` |
| 10 | Trip history — design screen 14 | `GET /api/v1/driver/rides` |
| — | Notification center | `GET /api/v1/driver/notifications` |
| — | Warning banner at 5 daily rejections | `GET /api/v1/driver/discipline` |

### Section C — Admin Dashboard (12 screens)

| # | Screen | Backend endpoint(s) needed |
|---|---|---|
| 1 | Admin login | `POST /auth/login` |
| 2 | Main dashboard: 24 drivers online, 8 active trips, live activity feed | `GET /admin/live-metrics` (Socket.IO stream) |
| 3 | Incoming requests queue | `GET /admin/pending-broadcasts` |
| 4 | Manual driver assignment | `POST /admin/rides/{id}/assign` |
| 5 | Drivers management (filter online/in-trip/offline) | `GET /admin/drivers` |
| 6 | Driver profile (lifetime stats) | `GET /admin/drivers/{id}` |
| 7 | Active trips monitor (with alerts) | `GET /admin/rides?status=active` |
| 8 | Trip history & reports | `GET /admin/reports/trips` |
| 9 | Financial overview (128,450 EGP total, weekly chart) | `GET /admin/reports/financials` |
| 10 | Customer management (1,248 total, 142 active) | `GET /admin/customers` |
| 11 | Notification center (target all / drivers / customers / VIP) | `POST /admin/notifications/broadcast` |
| 12 | Settings (pricing, commission, zones, admin roles) | `GET/PATCH /admin/settings` |

### Section D — Alternate versions (17 screens)

Alternative design treatments for team review. Not part of v1 build — for reference only.

### 3.5 Design System — Exact Colors & Rules (extracted from the design PNGs)

**Local copy of all 59 design images:** `wassalny/design_reference/` (same folder structure as the site — `01_customer_app/`, `04_driver_app/`, `02_admin_dashboard/`, `03_brand_assets/`, `05_alternate_versions/`). Flutter and dashboard work must match these pixel references.

| Token | Hex | Where it's used |
|---|---|---|
| `background` | **#111125** | App + dashboard main background (deep navy-black) |
| `background-deep` | **#050819** | Darkest areas (splash, map underlay) |
| `surface` / cards | **#1E1E32** | All cards, bottom sheets, list rows |
| `surface-raised` | **#292A3C** | Elevated sheets (customer home bottom sheet) |
| `primary` (accent) | **#F57C00** | Buttons, FAB, active nav icon, links, logo orange |
| `success` | **#22C55E** | "متاح" online toggle, completed states |
| `text-primary` | **#FFFFFF** | Headings, numbers (450, 24, 8) |
| `text-muted` | **#9BA3A8** | Secondary labels, timestamps |
| `warning` | **#FBC02D** | Stars, delay alerts, checkmark badges |

**Visual rules seen across all screens:**
- Dark theme **always** — there is no light mode in the design
- Arabic RTL layout first; numbers can be Arabic-Indic (٤٥٠) or Latin per design
- Rounded corners everywhere: cards ~16 px, buttons/pills fully rounded
- Bottom navigation bar with 4 items, active item in orange `#F57C00`
- Big bold numbers for stats (earnings, trips, ratings)
- Online/offline is a large pill toggle: green when online, dark when offline
- Trip states use colored icon chips inside cards (orange = active, green/yellow check = done)

⚠️ **Correction:** earlier drafts of this plan said "gold + black". Pixel sampling of the real designs shows the brand accent is **orange #F57C00** on navy-black **#111125** — use these values, not gold.

---

## 4. Confirmed Product Decisions

Locked in during discovery. **Any change here requires re-planning.**

| # | Decision | Chosen option | Reason |
|---|---|---|---|
| 1 | Location model | Fixed list of Benha neighborhoods | Fast matching, no ambiguity, no map costs |
| 2 | Pricing model | Zone-to-zone fixed price table | Customer knows fare upfront, no distance math |
| 3 | Matching strategy | Broadcast → first captain to accept wins (10-sec window) | Fair, fast, Uber-style |
| 4 | AI missing info | Hand off to human agent immediately | Reliability over automation |
| 5 | No-driver fallback | Expand to nearby zones automatically | Better customer experience |
| 6 | Customer auth | Phone number only (no OTP) | Fastest UX ⚠️ (see Risks) |
| 7 | Mobile stack | Flutter | One codebase, Arabic RTL support |
| 8 | Timeline | Full product in 2–3 months | Real launch, not just MVP |
| 9 | Payment | Cash only at launch | Simplifies compliance, faster launch |
| 10 | Commission | **15 %** to Wassalny, 85 % to captain | Matches design spec (128,450/19,267 EGP shown) |
| 11 | Operating hours | **True 24/7** (no blackout, no surge) | Matches "أقرب كابتن هيكلمك" 24/7 tagline |
| 12 | Captain rejection tolerance | **Warn at 5 rejections/day, auto-suspend for 24 h at 10** | Two-strike discipline; fair balance |
| 13 | Captain onboarding | **Admin-created only** — no captain self-signup | Admin uploads all documents; no captain-facing registration wizard |
| 14 | Customer no-show | **10 EGP cancellation fee** added to customer's next trip | Fair compensation to captain who arrived |
| 15 | Zone adjacency | **Every zone adjacent to every other zone** (fully connected) | Benha is small; simplest & safest fallback |
| 16 | Zone list at launch | **5 test zones initially** — expand before real launch | Ship faster; add real neighborhoods incrementally |
| 17 | Zone-to-zone pricing | **Ibrahim provides rough prices** — seeded into matrix | Founder knows the market rates |
| 18 | WhatsApp stickers | **Custom-designed files** provided by Ibrahim | Brand consistency across replies |
| 19 | AI provider | **Google Gemini 2.0 Flash** (Ibrahim has GCP billing enabled) | Cheap, fast, excellent Arabic |
| 20 | Push notifications | **Firebase Cloud Messaging** (Ibrahim has FCM project) | Free, mature, Flutter SDK |
| 21 | App stores | **iOS + Android from day one** (Ibrahim has both dev accounts) | Wider reach at launch |
| 22 | Admin dashboard scope | **Single web control plane** — admin sees everything, controls everything, overrides everything | One place for all ops instead of scattered tools |

---

## 5. Tech Stack

### Backend
| Component | Choice | Rationale |
|---|---|---|
| Framework | **Flask 3.0 + Flask-SocketIO** | Already partly built, mature, real-time ready |
| Language | **Python 3.11** | Team familiarity, ecosystem |
| Persistent DB | **PostgreSQL 15** (managed) | Reliability, transactions, mature |
| Real-time cache | **Redis 7** (managed) | Driver availability, atomic locks, pub/sub |
| Background jobs | **RQ (Redis Queue)** | Simple, integrates with Redis we already use |
| WhatsApp | **Meta Cloud API** (direct) | No BSP markup, cheapest, official |
| AI parsing | **Google Gemini 2.0 Flash** | Fast (<1s), cheap, excellent Arabic |
| Push notifications | **Firebase Cloud Messaging** | Free, iOS + Android, Flutter SDK |
| Error tracking | **Sentry** (free tier) | Catch bugs before customers report |
| Deployment | **Railway** | Postgres + Redis add-ons, git-push deploys |
| App server | **Gunicorn + eventlet** | WebSocket support, production-tested |

### Mobile
| Component | Choice | Rationale |
|---|---|---|
| Framework | **Flutter 3.x** | One codebase, Arabic RTL, offline resilience |
| State mgmt | **Riverpod** or **BLoC** | Robust, testable |
| Networking | **Dio** | Interceptors, retries |
| WebSocket | **socket_io_client** | Matches backend |
| Push | **firebase_messaging** | FCM SDK |

### Admin Dashboard (web)
| Component | Choice | Rationale |
|---|---|---|
| Rendering | **Jinja2** (server-side) | Fast, secure, no separate build |
| Interactivity | **HTMX + vanilla JS** | Zero build tools, easy to maintain |
| Styling | **Tailwind CSS via CDN** | Fast to iterate |
| Real-time | **Socket.IO client** | Live dashboard updates |
| Design | Orange `#F57C00` on navy-black `#111125` (see §3.5) | Match approved designs |

---

## 6. High-Level Architecture

```
                     ┌──────────────────────┐
                     │   WhatsApp (Meta)    │
                     │    Cloud API         │
                     └──────────┬───────────┘
                                │ webhook
                                ▼
┌───────────────┐        ┌──────────────────────────────────────┐
│  Customer     │◄──────►│                                       │
│  Flutter App  │  REST  │       Flask + Flask-SocketIO          │
└───────────────┘  WS    │                                       │
                         │  ┌────────────┐  ┌────────────────┐   │
┌───────────────┐        │  │ Matching   │  │  AI Parser     │◄──┼──► Gemini
│  Captain      │◄──────►│  │ Engine     │  │  (Gemini API)  │   │
│  Flutter App  │  REST  │  └─────┬──────┘  └────────────────┘   │
└───────────────┘  WS    │        │                              │
                         │        ▼                              │
┌───────────────┐        │  ┌─────────────┐   ┌──────────────┐   │
│  Admin        │◄──────►│  │ PostgreSQL  │   │    Redis     │   │
│  Dashboard    │  WS    │  │ (permanent) │   │ (realtime)   │   │
└───────────────┘        │  └─────────────┘   └──────────────┘   │
                         └───────────────────────┬───────────────┘
                                                 │
                                                 ▼
                                          ┌─────────────┐
                                          │ RQ workers  │  ← async jobs
                                          │ + FCM push  │
                                          └─────────────┘
```

**Key principle:** every "hot path" (matching, availability, active-trip state) lives in Redis. PostgreSQL is only touched for creation, completion, and reporting. This is what gets us the sub-200 ms responsiveness.

---

## 7. Data Model

### PostgreSQL (permanent)

**users** — team members (admin / dispatcher / agent)
```
id, email, name, password_hash, role, is_active, created_at
```

**customers** — end users
```
id, wa_id (phone with country code), name, notes, created_at
```

**drivers** — captains (admin-created; no self-registration)
```
id, wa_id, name, password_hash, national_id, license_number, car_model, car_plate,
car_color, category (economy/business/premium), photo_url,
national_id_photo_url, license_photo_url, criminal_check_photo_url,
rating, total_trips, daily_rejections, discipline_status
(active/warned/suspended), suspended_until, is_active,
created_by_admin_id, created_at
```

**zones** — Benha neighborhoods
```
id, name_ar, name_en, slug, is_active
```
_Example: {name_ar: "الرملة", name_en: "El Ramla", slug: "el-ramla"}_

**zone_pricing** — pricing matrix
```
id, from_zone_id, to_zone_id, price_egp,
UNIQUE(from_zone_id, to_zone_id)
```

**rides** — the trip itself
```
id, customer_id, driver_id (nullable until assigned),
from_zone_id, to_zone_id, price_egp, commission_egp (= price * 0.15),
no_show_fee_egp (10 EGP if applicable), status (see state machine),
source (whatsapp/app/admin), created_at, assigned_at, started_at,
completed_at, cancelled_at, cancel_reason, rating, rating_comment
```

**customer_pending_fees** — accumulated fees to charge on next trip
```
id, customer_id, reason (no_show/etc), amount_egp, from_ride_id,
created_at, applied_to_ride_id (nullable), applied_at (nullable),
waived_by_admin_id (nullable), waived_at (nullable), waive_reason
```

**complaints** — tickets from customers, captains, or admin
```
id, filed_by_kind (customer/driver/admin), filed_by_id, subject,
description, category (missing_item/overcharge/rude/no_show/wrong_route/
safety/other), ride_id (nullable), assigned_to_user_id (nullable),
status (open/in_progress/waiting_user/resolved/closed), resolution,
resolution_action (refund/credit/warn/suspend/ban/none),
sla_breach (bool), created_at, resolved_at
```

**complaint_comments** — internal admin ↔ admin discussion on tickets
```
id, complaint_id, author_user_id, body, is_internal, created_at
```

**sos_alerts** — customer pressed SOS during trip
```
id, ride_id, customer_id, driver_id, message, status
(open/acknowledged/resolved), acknowledged_by_user_id,
acknowledged_at, resolved_at, notes
```

**audit_logs** — immutable log of every admin action
```
id, actor_user_id, action, target_kind, target_id, before_json,
after_json, ip_address, user_agent, created_at
```

**bans** — banned customers or captains
```
id, target_kind (customer/driver), target_id, reason,
banned_by_user_id, expires_at (nullable = permanent),
lifted_by_user_id (nullable), lifted_at (nullable), created_at
```

**credit_adjustments** — free trips or discounts given
```
id, customer_id (or driver_id), amount_egp, direction (credit/debit),
reason, created_by_user_id, applied_to_ride_id (nullable),
expires_at (nullable), created_at
```

**admin_broadcasts** — marketing/announcement campaigns
```
id, kind (whatsapp_marketing/inapp_banner), template_name (if WA),
message_ar, message_en, audience_filter_json,
scheduled_for, sent_at, recipient_count, delivered_count,
failed_count, created_by_user_id, created_at
```

**announcements** — in-app banner for customer/captain apps
```
id, audience (customer/driver/both), title_ar, title_en, body_ar,
body_en, starts_at, ends_at, priority (info/warning/critical),
created_by_user_id, created_at
```

**ride_admin_notes** — internal notes on a specific trip
```
id, ride_id, author_user_id, body, created_at
```

**captain_documents** — uploaded by admin per Decision #13
```
id, driver_id, kind (national_id/license/criminal_check/car_photo_front/
car_photo_back/car_photo_left/car_photo_right), file_url,
uploaded_by_user_id, uploaded_at, verified (bool), verified_by_user_id
```

**captain_ratings_given_to_customer** — captains rate customers back
```
id, ride_id, driver_id, customer_id, stars, comment, created_at
```

**conversation_tags** — tags applied to WhatsApp conversations
```
id, conversation_id, tag (vip/complaint/refund/feedback/spam),
applied_by_user_id, created_at
```

**admin_notifications** — in-app inbox for admin users
```
id, recipient_user_id, kind (assigned_complaint/sos/mention/system),
title, body, link, read_at, created_at
```

**broadcasts** — audit trail of matching attempts
```
id, ride_id, zone_id, driver_ids (JSON array), started_at, ended_at,
accepted_by_driver_id (nullable), outcome (accepted/timeout/expanded)
```

**conversations** + **messages** — WhatsApp inbox (already built)

**ai_sessions** — multi-message AI parsing state
```
id, customer_id, wa_id, status (parsing/handoff/completed/failed),
partial_pickup, partial_dropoff, last_message_at, expires_at (30 min)
```

**stickers** — brand assets used in WhatsApp replies
```
id, name, wa_media_id, purpose (booked/completed/no_driver/etc.)
```

**admin_alerts** — needs human attention
```
id, kind (ai_handoff/no_driver/dispute), payload (JSON),
status (open/handled), handled_by_user_id, created_at, resolved_at
```

**ride_status_events** — full audit log for disputes
```
id, ride_id, event (created/broadcast/assigned/started/completed/cancelled),
actor (customer/driver/admin/system), payload (JSON), created_at
```

**earnings** — daily rollup per driver
```
id, driver_id, date, trips_count, gross_egp, commission_egp,
net_egp, UNIQUE(driver_id, date)
```

**message_templates** — approved WhatsApp templates (already built)

**notifications** — in-app notifications
```
id, recipient_kind (customer/driver), recipient_id, title, body,
kind, payload (JSON), read_at, created_at
```

### Redis (real-time, ephemeral)

| Key pattern | Type | Purpose | TTL |
|---|---|---|---|
| `driver:{id}:status` | Hash | `online`, `available`, `zone_id`, `last_hb` | none (heartbeat maintained) |
| `zone:{id}:available_drivers` | Sorted Set | driver_ids scored by last activity | 5 min |
| `broadcast:{ride_id}` | Hash | active broadcast state | 30 s |
| `broadcast:{ride_id}:offered_to` | Set | driver_ids currently seeing offer | 30 s |
| `ride:{id}:lock` | String | atomic reservation lock | 15 s |
| `driver:{id}:current_ride` | String | ride_id if on active trip | none |
| `ai_session:{wa_id}` | Hash | AI conversation state | 30 min |
| `rate_limit:customer:{id}` | Counter | max 3 bookings per 10 min | 10 min |

---

## 8. Core Systems

### 8.1 Zone & Pricing Manager
- Admin defines all Benha neighborhoods once
- Admin fills the zone-to-zone price table (or bulk import from CSV)
- Also defines **adjacency** between zones (for nearby-zone expansion when no captain accepts)
- Admin can toggle a zone off (e.g. during a road closure)

### 8.2 Driver Availability Engine
- Captain's Flutter app opens WebSocket to `/driver` namespace on login
- Every 15 s the app pings `heartbeat` with `zone_id`
- No heartbeat for 60 s → captain auto-marked offline (real-time via Redis TTL)
- Captain manually toggles `available/busy` from the app
- On any state change → Redis atomically updates `driver:{id}:status` and `zone:{id}:available_drivers`

### 8.3 Booking Intake
Two entry points, both funnel into the same `create_ride()` function:
- **WhatsApp:** webhook → AI parser → `create_ride()`
- **Customer app:** `POST /api/v1/rides` → `create_ride()`

Both channels validate zones exist, look up price, then hand off to the Matching Engine.

### 8.4 Matching Engine (§11)
Broadcast + atomic accept + nearby-zone expansion. Details below.

### 8.5 Trip Lifecycle Manager
- Enforces the state machine (§10)
- Records every transition in `ride_status_events`
- Emits Socket.IO events to customer + captain + admin dashboard
- Updates driver availability (busy while trip is active, available on completion)
- Updates driver's `current_zone` to the trip's destination zone on completion

### 8.6 AI Parser Service (§12)
- Gemini 2.0 Flash with strict prompt including the current list of active zones
- 3-second timeout — otherwise handoff
- Multi-turn conversation via `ai_sessions` table
- Returns structured JSON: `{intent, from_zone_slug, to_zone_slug, confidence}`

### 8.7 WhatsApp Responder
- Send text replies (free-form inside 24 h window)
- Send stickers (brand-specific per event: booked, completed, no driver)
- Send approved templates outside 24 h window
- Deduplication by WhatsApp message ID

### 8.8 Push Notification Service
- FCM tokens registered by mobile apps on login
- Trip offers, trip updates, admin broadcasts go through here
- Falls back to WhatsApp if push fails

### 8.9 Admin Monitoring
- Live counters: online drivers, active trips, pending rides, avg matching time
- Streaming activity feed via Socket.IO
- Alerts panel: AI handoffs, no-driver events, disputes
- Manual intervention buttons

### 8.10 Reports & Analytics
- Daily rollup job at 00:05 Cairo time
- Populates `earnings` table per driver
- Financial dashboard queries the rollup, not raw rides (fast)

### 8.11 Rating & Reputation
- Customer rates driver 1–5 stars after trip completion
- Driver reputation = weighted rating (recency-weighted)
- Below 3.5 → auto-flag for admin review

### 8.12 Captain Discipline (per Decision #12)
- Every rejection increments `daily_rejections` (Redis counter, resets at midnight Cairo)
- At **5 rejections**: `discipline_status = warned` → banner in captain app "خد بالك، رفضت 5 مشاوير النهاردة"
- At **10 rejections**: `discipline_status = suspended`, `suspended_until = now + 24h`, kicked from availability pool, WhatsApp message sent
- Admin can override any suspension from the dashboard

### 8.13 Admin Captain Onboarding (per Decision #13)
Since captains cannot self-register:
- Admin dashboard has "Add Captain" wizard: personal data → vehicle → upload documents → generate credentials
- Admin sends credentials to captain via WhatsApp template (`captain_credentials_ar`)
- Captain opens app → logs in with phone + password → prompted to change password on first login
- All document files uploaded by admin (national ID, license, criminal check, vehicle photos)

### 8.14 No-Show Fee Handling (per Decision #14)
- After ride is `assigned`, captain has a **"No-show"** button (enabled after 5 min from `assigned_at`)
- Tapping "No-show" → ride status = `cancelled_no_show`, captain freed, `customer_pending_fees` row created with 10 EGP
- Next time customer books → matching engine adds pending fees to trip price
- Fees visible to customer in booking summary ("سعر: 25 EGP + 10 EGP رسوم عدم التزام")
- If customer disputes → admin can cancel the fee

### 8.15 Complaints & Dispute Resolution
- Customer or captain files a complaint from their app (linked to a ride optionally)
- Backend creates a `complaints` row, auto-assigns to on-call admin
- Admin sees it in dashboard §13.2 #5
- Internal comments recorded on the ticket
- Resolution actions carry side-effects (refund → creates `credit_adjustments`, warn/suspend → updates `drivers`, ban → creates `bans` row)
- SLA tracker: alert if unresolved > 4 h (warning) or > 24 h (critical)

### 8.16 SOS & Safety
- Customer app has SOS button during active trip
- POST `/api/v1/rides/{id}/sos` creates `sos_alerts` row
- Real-time push to all on-call admins (Socket.IO + FCM + phone call fallback via Twilio Voice, optional)
- Admin sees ride details + can call both parties in one tap
- Escalation button (dials 122 police) — configurable per country

### 8.17 Audit Log Service
- Every admin write action wraps the change in `audit_logs`
- Automatic — implemented via Flask `after_request` middleware + SQLAlchemy event hooks
- Immutable: rows never updated or deleted (enforced at DB level with revoked update/delete perms in prod)

### 8.18 Marketing & Announcements
- Admin composes broadcast → selects audience filter (all customers / VIP / dormant / by zone)
- Backend computes recipient list, shows cost estimate, waits for admin confirmation
- On confirm → enqueues jobs on RQ, sends via WhatsApp Cloud API
- Tracks delivery per recipient (via webhook status callbacks) → shows delivered/failed rates on the campaign page

### 8.19 Impersonation (Read-Only)
- Admin clicks "View as customer X" or "View as captain Y"
- Backend issues a short-lived read-only JWT with `impersonating_user_id`
- Admin dashboard shows exactly what that user would see (trip history, notifications, etc.)
- Every impersonation is logged in `audit_logs`

---

## 9. Booking Flows (End-to-End)

### 9.1 WhatsApp Booking Flow

```
Customer          Meta            Flask App         Gemini AI       Matching       Captain App
   │                │                │                 │                │              │
   │  "عايز عربية    │                │                 │                │              │
   │   في الرملة     │                │                 │                │              │
   │   لجامعة بنها" ►│                │                 │                │              │
   │                │──webhook──────►│                 │                │              │
   │                │                │──parse msg────►│                │              │
   │                │                │◄──{from:ramla,──│                │              │
   │                │                │    to:univ}     │                │              │
   │                │                │──create_ride()─┐                │              │
   │                │                │                │                │              │
   │                │                │──broadcast────►│                │              │
   │                │                │                │──offer trip───►│              │
   │                │  Sticker + 🚗  │                │                │              │
   │  "Captain      │  "captain      │                │                │              │
   │   Ahmed        │◄──contact"─────│                │                │              │
   │   coming"     ◄│                │                │                │              │
   │                │                │                │◄──accept──────│              │
   │                │                │◄──assigned─────│                │              │
   │                │                │◄──driver info──│                │              │
   │                │                │──contact info─►│──"customer     │              │
   │                │                │                │  phone:        │              │
   │                │                │                │  +2010..."───►│              │
                                                                       ▼
                                                              Captain calls
                                                              customer, meets,
                                                              taps "trip started"
```

**Failure branches:**
- Gemini can't parse → create `AdminAlert(kind=ai_handoff)` → dashboard notification → agent takes over the chat
- No captain accepts → expand to adjacent zones → still nothing → `AdminAlert(kind=no_driver)` + WhatsApp reply "معلش، مفيش عربية دلوقتي، هنبعتلك تاني بعد شوية"

### 9.2 Mobile App Booking Flow

```
Customer App          Flask API           Matching          Captain App
     │                    │                   │                 │
     │ open zone picker   │                   │                 │
     │──GET /zones───────►│                   │                 │
     │◄──[list]───────────│                   │                 │
     │                    │                   │                 │
     │ pick pickup=Ramla, │                   │                 │
     │      dropoff=Univ  │                   │                 │
     │                    │                   │                 │
     │──POST /rides/quote►│                   │                 │
     │◄──{price:30 EGP}───│                   │                 │
     │                    │                   │                 │
     │──POST /rides──────►│                   │                 │
     │                    │──broadcast───────►│                 │
     │◄──"searching..."───│                   │──offer_trip────►│
     │  (WebSocket)       │                   │                 │
     │                    │                   │◄──accept────────│
     │◄──trip_assigned────│                   │                 │
     │  (driver name,     │                   │                 │
     │   car, phone)      │                   │                 │
     │                    │                   │                 │
     │◄──trip_started─────│                   │◄──start btn─────│
     │◄──trip_completed───│                   │◄──complete btn──│
     │                    │                   │                 │
     │ rate the trip      │                   │                 │
```

---

## 10. Trip State Machine

```
                   ┌───────────────────────────────────────────────┐
                   │                                               │
                   ▼                                               │
   [ new ] ─── broadcast ───► [ broadcasting ]                    │
       │                             │                             │
       │                             │ accepted                    │
       │                             ▼                             │
       │                       [ assigned ]                        │
       │                             │                             │
       │                             │ captain taps "start"        │
       │                             ▼                             │
       │                       [ started ]                        │
       │                             │                             │
       │                             │ captain taps "complete"    │
       │                             ▼                             │
       │                       [ completed ] ────────► DONE       │
       │                                                            │
       │                                                            │
       └─── cancel (any state except completed) ───► [ cancelled ]
```

**Transitions:**
| From | Event | To | Actor |
|---|---|---|---|
| new | broadcast_started | broadcasting | system |
| broadcasting | driver_accepted | assigned | driver |
| broadcasting | timeout | broadcasting (expanded zone) | system |
| broadcasting | no_driver_after_expand | cancelled | system (auto) |
| assigned | driver_started | started | driver |
| assigned | customer_cancel | cancelled | customer |
| assigned | captain_no_heartbeat_60s | broadcasting (re-broadcast, exclude that captain) | system |
| assigned | captain_no_show (after 5 min) | cancelled_no_show (+10 EGP pending fee) | driver |
| started | driver_completed | completed | driver |
| any (except completed) | admin_cancel | cancelled | admin |

Any illegal transition is rejected server-side. Every transition is recorded in `ride_status_events` for dispute resolution.

---

## 11. Matching Algorithm

The most critical piece. Full pseudocode:

```python
def match_ride(ride):
    zone = ride.from_zone
    rounds = 0
    tried_zones = {zone.id}

    while rounds < 3:  # 1st round: same zone, 2nd: neighbors, 3rd: neighbors of neighbors
        # 1. Get available drivers from Redis (sub-5ms lookup)
        driver_ids = redis.zrange(f"zone:{zone.id}:available_drivers", 0, -1)

        if not driver_ids:
            zone = expand_to_next_neighbor(tried_zones)
            if not zone: break
            tried_zones.add(zone.id)
            rounds += 1
            continue

        # 2. Broadcast: mark drivers as offered
        redis.sadd(f"broadcast:{ride.id}:offered_to", *driver_ids)
        redis.expire(f"broadcast:{ride.id}:offered_to", 15)

        # 3. Push offer to each driver's Socket.IO connection + FCM
        for did in driver_ids:
            socketio.emit("trip_offered", ride.to_dict(),
                          room=f"driver:{did}")
            fcm.send(driver_fcm_token(did), ride.to_dict())

        # 4. Wait up to 10 seconds for someone to accept
        winner = wait_for_accept(ride.id, timeout=10)

        if winner:
            # 5. ATOMIC reservation — only one driver wins
            #    SET NX guarantees only one caller succeeds
            reserved = redis.set(f"ride:{ride.id}:lock",
                                 winner, nx=True, ex=15)
            if reserved:
                assign_ride(ride, winner)
                return "assigned"
            # If someone else already won, keep waiting
            continue

        rounds += 1

    # 6. Exhausted all zones
    ride.status = "cancelled"
    ride.cancel_reason = "no_driver_available"
    db.commit()
    create_admin_alert(kind="no_driver", ride_id=ride.id)
    notify_customer_no_driver(ride)
```

**Guarantees:**
- **No double-booking:** Redis `SET NX` is atomic — only one accept wins even if 3 drivers tap at the same millisecond
- **No orphan drivers:** offered set expires in 15 s, so a slow-network driver can't hold a lock forever
- **No busy captain gets a new offer:** filter by `available=true` at Redis lookup time
- **Fair broadcasting:** captains sorted by "last activity" ascending → whoever's been idle longest is at the top

---

## 12. WhatsApp AI Flow (Gemini)

### 12.1 System prompt (Arabic-aware)

```
أنت مساعد لتطبيق وصلني بنها للأجرة.
مهمتك: من كلام العميل، تعرف من أين ينطلق ومكان وجهته.

المناطق المتاحة (استخدم فقط أسماء من هذه القائمة):
- الرملة
- وسط البلد
- السرايات
- جامعة بنها
- طريق دمنهور
- ... (أضف الباقي هنا من DB)

أرجع النتيجة JSON فقط بالتنسيق:
{"intent": "book_ride" | "clarify" | "unknown",
 "from_zone": "<slug or null>",
 "to_zone": "<slug or null>",
 "confidence": 0.0-1.0,
 "reply_ar": "<if clarify or unknown, what to say back>"}
```

### 12.2 Flow

```
Incoming WhatsApp message
        │
        ▼
Is there an active ai_session (< 30 min old)?
        │
    ┌───┴───┐
    Yes     No
    │       │
    │       ▼
    │   Create new ai_session
    │       │
    └───────┤
            ▼
     Call Gemini (3s timeout)
        ┌───┴────────────────┐
    Success              Timeout / error
        │                     │
        ▼                     ▼
   Parse JSON              Handoff
        │                     │
   ┌────┴─────┐               ▼
   intent =   AdminAlert(kind=ai_handoff)
   book_ride  clarify   unknown
       │       │           │
       ▼       ▼           ▼
   Have both  Reply       AdminAlert
   zones?     with        (kind=ai_handoff)
       │      reply_ar
   ┌───┴──┐   from AI
   Yes    No
   │      │
   ▼      ▼
  Create  Save partial
  ride    to ai_session
  and     and reply
  match   "من فين لفين؟"
```

### 12.3 Cost estimate

- Gemini 2.0 Flash: ~$0.075 per 1M input tokens, ~$0.30 per 1M output tokens
- Per parse: ~500 in + 100 out ≈ $0.00007
- 10,000 parses/month → **~$0.70/month** — negligible

---

## 13. Admin Dashboard — The Control Plane

**Decision #22:** the web admin dashboard is the **single control plane** for the entire business. Every conversation, every trip, every complaint, every report, every setting is accessible and overridable from here. No secondary tools, no shell scripts, no direct database access needed in normal operation.

**Layout:** Dark theme per §3.5 design system (navy-black `#111125` + orange `#F57C00`), matching [ibrahimfakhrey.github.io/wasalny](https://ibrahimfakhrey.github.io/wasalny/). Left sidebar navigation. Top bar with live metrics widget and admin's notification bell.

### 13.1 Live Metrics Widget (always visible)

- 🟢 Captains online: **24**
- 🚗 Active trips: **8**
- ⏳ Pending broadcasts: **3**
- ⚡ Avg matching time: **6.2 s**
- 🆘 Open SOS alerts: **0**
- 🚨 Open complaints: **2**
- 💬 AI handoffs waiting: **1**

Updates via Socket.IO every 2 seconds. Any red number is a link to the relevant page.

### 13.2 Twelve Top-Level Sections

Each section is a full page with tabs/sub-pages. Everything is searchable and exportable to CSV.

#### 1. 🏠 Home — Live Overview
- Real-time counters (as in §13.1)
- Live activity feed: last 50 events (new booking, trip started, complaint filed, captain went offline, SOS raised)
- Open alerts panel (AI handoffs, no-driver events, SOS, disputes)
- Quick actions: "Create ride manually", "Send announcement", "Add captain"

#### 2. 🚗 Rides
Everything about trips, in one place.

- **Tabs:** Pending broadcasts · Active trips · Completed · Cancelled · No-shows · All
- **Filters:** by date, customer name/phone, captain, from-zone, to-zone, status, source (WhatsApp/app/admin)
- **Bulk actions:** export CSV, cancel selected, refund fees
- **Click any ride → full trip page:**
  - Full timeline (from `ride_status_events`) — every state change with timestamp + actor
  - Full WhatsApp chat history if booked via WhatsApp
  - Customer + captain info + phone numbers (one-click call)
  - **Manual actions:** force-assign to a different captain, force-cancel with refund, adjust fare, waive no-show fee, mark as disputed, add admin note
  - Rating + comment (if left)
  - Link to related complaint (if any)

#### 3. 💬 Conversations (WhatsApp Inbox)
Admin sees **every** WhatsApp conversation across the platform.

- Same real-time inbox already built, but expanded:
- **Filters:** unassigned, my conversations, by agent, customer/captain, unread, has-open-complaint
- **Take over any chat** even if assigned to another agent (with reason logged)
- **Transfer** conversation to another agent
- **Close / reopen** conversation
- **Tag** conversation (VIP, complaint, refund request, feedback, spam)
- **Full history search** by text across all messages ever

#### 4. 🤖 AI Handoffs
Chats waiting for a human because Gemini couldn't parse them.

- Queue view with wait time counter
- Click → take over conversation
- Mark as resolved after fixing the booking

#### 5. 😠 Complaints & Disputes
Full ticketing system.

- **Filters:** open, in-progress, resolved, my tickets, by category (missing item, overcharge, rude driver, no-show, wrong route, safety, other)
- **Filed by:** customer (from mobile app "Report" button) OR captain (from captain app) OR admin (opened proactively)
- **Ticket page:**
  - Subject + description + attachments
  - Related ride (auto-linked if reported from trip screen)
  - Full timeline of internal comments (admin ↔ admin)
  - Assign to team member
  - Status: open → in-progress → waiting-user → resolved → closed
  - Resolution actions: refund customer, credit customer, warn captain, suspend captain, ban customer, no action
- **SLA tracking:** color-coded by age (yellow > 4 h, red > 24 h)

#### 6. 🆘 SOS Alerts
When a customer taps SOS in the mobile app during a trip.

- Instant popup + phone alert to on-call admin
- Live view: customer name + phone, captain name + plate + phone, trip route, active time
- One-click call both parties
- Escalation button (calls police if configured)
- Post-incident report

#### 7. 👤 Captains
Full lifecycle management.

- **Add captain wizard** (per Decision #13 — admin-only creation):
  1. Personal data (name, phone, national ID)
  2. Vehicle data (model, plate, color, category)
  3. Upload documents (national ID, license, criminal check, 4 vehicle photos)
  4. Generate credentials → WhatsApp them to captain via template
- **List** with filters: online/offline, available/busy, active/suspended/banned, category, top-rated, most-rejected
- **Captain profile:**
  - Lifetime stats (total trips, rating, earnings, commission owed, rejection rate)
  - Weekly earnings graph
  - Trip history table
  - Complaint history
  - Documents (view/replace)
  - **Actions:** suspend, unsuspend, reset password, force offline, clear stuck active trip, ban, adjust rating, add internal note

#### 8. 🧍 Customers
- **Search** by phone, name
- **List** with filters: active (last 30 d), VIP, dormant, banned, has-open-complaint
- **Customer profile:**
  - Full trip history
  - Complaint history filed by them AND against them
  - Pending fees list (`customer_pending_fees`)
  - Ratings received (yes, customers get rated by captains too)
  - **Actions:** ban, unban, waive pending fee, give trip credit, add internal note, view all their WhatsApp messages

#### 9. 🗺️ Zones & Pricing
- **Zones CRUD** — add / rename / activate / deactivate a zone (deactivate for road closures)
- **Pricing matrix** — visual grid where rows = from-zone, columns = to-zone, cells editable inline
- **Bulk import prices** from CSV
- **Adjacency** — per Decision #15, fully connected by default. Toggle in future if we want tighter control.

#### 10. 📣 Marketing & Broadcasts
Where you drive growth without spamming (avoid the ban).

- **Marketing broadcasts** — send approved WhatsApp Marketing template to targeted audience:
  - All customers · VIP · Dormant (>30 d no trip) · By last-trip zone · Custom list
  - Pre-flight cost estimate before sending
  - Delivery report after send
- **In-app announcements** — banner shown in customer or captain apps ("مفيش عربية النهاردة عشان الأمطار")
- **Scheduled broadcasts** — send tomorrow 8 AM

#### 11. 📊 Reports & Analytics
- **Financial** — revenue, commission, waived fees, no-show fees collected; charts by day/week/month; export CSV
- **Driver performance** — trips, rating, rejections, earnings; top 10, bottom 10; individual drill-down
- **Customer analytics** — active count, retained cohorts (7 d / 30 d), churned, VIP list, avg trips per customer
- **Trip analytics** — popular routes (from→to heatmap), peak hours per day, avg matching time trend
- **Complaint metrics** — open count, avg resolution time, categories breakdown
- **Financial dashboard** widget (matches the design: 128,450 EGP total, weekly bar chart)
- **Export anything** to CSV or Excel

#### 12. ⚙️ Settings & Team
- **Team** — admin/dispatcher/agent CRUD (already built)
- **Business rules** — commission %, no-show fee, thresholds, broadcast timeouts
- **WhatsApp config** — verify token, access token, sender number, quality score
- **AI config** — Gemini API key, model version, prompt editor
- **Payment settings** (future) — Paymob/Fawry config
- **Notification templates** — text for common admin actions
- **Audit log** — immutable log of every admin action (who did what, when, before/after values)
- **API health** — WhatsApp quota, Gemini usage, Redis memory, Postgres CPU
- **Backup & restore** — download DB snapshot

### 13.3 Cross-cutting Features

- **Global search bar** (top nav): type a phone number, name, trip ID, plate number → jump to the right page
- **Impersonation** (admin only): "View as this captain" or "View as this customer" — read-only render of what they'd see (great for debugging user complaints)
- **Keyboard shortcuts** for power users (⌘K search, R go to rides, C to conversations)
- **Multi-language toggle** — Arabic ⇄ English
- **Dark theme always** (per §3.5 — the design has no light mode)
- **Session security** — 2-factor for admin role, auto-logout after 30 min idle
- **Real-time everywhere** — every list auto-updates without refresh via Socket.IO

---

## 14. Mobile Apps (Flutter)

Both apps share:
- Flutter 3.x + Riverpod
- Same networking layer (Dio + JWT interceptor)
- Same Socket.IO client wrapper
- Same design system (exact tokens in §3.5, pixel references in `design_reference/`)
- Same push notification setup (FCM)
- Arabic RTL first-class

### 14.1 Customer App

**Screens** — see [§3 Section A](#section-a--customer-app-14-screens) for full mapping.

**Critical flows:**
- Fast login (phone → straight in)
- Zone pickers as bottom-sheets with search
- Quote endpoint called before final booking so customer sees price (including any pending no-show fees)
- Live driver-search animation waits for `trip_assigned` socket event
- SOS button in active trip → posts to `/rides/{id}/sos` → admin alert
- **"Report a problem"** button on every completed trip → opens complaint form (category + description + optional photo) → creates a complaint ticket
- Post-trip rating is mandatory before booking again
- In-app announcements banner (from admin broadcasts)
- Notification center with unread badge

### 14.2 Captain App

**Screens** — see [§3 Section B](#section-b--captain-app-simplified--no-self-registration).

**Critical flows (Decision #13 — no registration wizard):**
- Login only (phone + admin-issued password → forced change on first login)
- Home dashboard with big "online/offline" toggle
- Zone selector (must be selected before going online)
- Trip offer full-screen popup with countdown timer + accept/reject
- One-tap "trip started" and "trip completed" buttons
- **No-show** button (enabled 5 min after trip assigned)
- Warning banner at 5 daily rejections (Decision #12)
- **"Report customer"** button on every completed trip → creates complaint ticket
- **Rate the customer** (stars + comment) after trip
- In-app announcements banner (from admin broadcasts)
- Earnings view shows today, this week, this month
- Weekly settlement date reminder

---

## 15. Non-Functional Requirements

| Requirement | Target | How we achieve it |
|---|---|---|
| API latency (P95) | **< 200 ms** | Redis for hot path, indexed Postgres, connection pooling |
| Matching time (median) | **< 5 s** | 10 s broadcast window, immediate Redis reads |
| Concurrent WebSocket clients | **10,000+** | Eventlet worker, Socket.IO with Redis adapter (v2) |
| Uptime | **99.5 %** first year | Railway managed services, health checks, alerts |
| Data durability | **Zero data loss on trips** | Postgres primary + daily backups |
| Rate limits | **3 bookings / 10 min / customer** | Redis counter |
| WhatsApp webhook processing | **Under 5 s** always | Return 200 immediately, process async in RQ |
| AI parse timeout | **3 s** | Fallback to human handoff |
| Push notification delivery | **< 3 s** | FCM directly, no queue in between |
| Localization | **Arabic (RTL) + English (LTR)** | Flutter intl, Jinja i18n |

---

## 16. Failure Scenarios & Handling

| Scenario | Detection | Response |
|---|---|---|
| Gemini API slow/down | 3-s timeout | Handoff to admin dashboard; customer sees "we're on it" |
| No driver accepts in any zone | 3 broadcast rounds all failed | Cancel ride + WhatsApp customer "no cars now" + admin alert |
| Captain accepts then disappears (bad internet) | No heartbeat 60 s after assign | Re-broadcast to other captains, mark first captain "unreliable +1" |
| Two captains tap accept at exact same time | Redis SET NX returns false for one | Loser sees "already taken, next request coming" |
| Customer double-books (spam) | Rate limit exceeded (Redis counter) | Reply "please wait 10 minutes" |
| WhatsApp webhook duplicated | Meta message ID already processed | Skip silently |
| Captain marks "started" but never picks up customer | Customer complaint | `/rides/{id}/dispute` → admin reviews `ride_status_events` |
| Postgres connection pool exhausted at peak | Connection wait > 500 ms | Alert; scale up pool; add read replica if persistent |
| Redis eviction | Ejected keys under memory pressure | Alarm on memory > 75 %; upgrade instance |
| FCM token expired for a captain | Push failed | Fall back to Socket.IO; if socket also disconnected → skip (they're offline) |

---

## 17. 12-Week Timeline

Assumes 1 backend dev + 1 Flutter dev + 1 designer/QA working full time.

### Weeks 1–2 · Backend Foundation
- ✅ Base Flask app scaffold (already done)
- Add Redis + RQ workers
- Zones + adjacency + zone_pricing tables
- Admin UI for zones and pricing matrix
- Driver availability engine (WebSocket + heartbeat + Redis)

### Weeks 3–4 · Booking + Matching Engine
- Ride model + state machine
- Broadcast matching algorithm (§11)
- Atomic reservation with Redis
- Nearby-zone expansion
- Trip lifecycle endpoints (`quote`, `create`, `start`, `complete`, `cancel`)
- Load-test with fake drivers + fake customers (targets: 100 concurrent bookings)

### Week 5 · WhatsApp AI Integration
- Gemini 2.0 Flash client with the Arabic prompt
- Multi-turn `ai_sessions` table
- Sticker upload + WhatsApp sticker sending
- Admin handoff flow (alerts + queue)
- End-to-end test: real WhatsApp message → ride created → captain notified

### Week 6 · Admin Dashboard — Core (Phase 4a)
- Apply §3.5 design system theme (navy-black + orange, from `design_reference/` PNGs)
- Live metrics widget + home overview
- Rides page (all statuses, filters, trip timeline, manual overrides)
- Conversations inbox (take-over/transfer/tag)
- AI handoff queue with agent-takeover
- Captains page + add-captain wizard (with document uploads)
- Captain profile (suspend / reset / ban / clear stuck trip)
- Customers page (search, profile, pending fees, ban/credit)
- Zones & pricing matrix editor

### Week 7 · Admin Dashboard — Ops Layer (Phase 4b)
- **Complaints ticketing system** with SLA tracking
- **SOS alerts receiver** (Socket.IO popup + FCM to on-call)
- **Marketing broadcasts** (target segments + delivery reports)
- **In-app announcements** (banner to customer/captain apps)
- **Reports & analytics** (financial, driver perf, customer, trip, complaints)
- **Audit log** middleware wrapping every write
- **Impersonation** (read-only view-as customer/captain)
- **Global search + keyboard shortcuts**
- Templates + stickers admin
- Settings + team management

### Weeks 8–9 · Customer Flutter App
- Splash + onboarding
- Login (phone only per decision)
- Home + zone pickers (bottom-sheet search)
- Trip quote + booking
- Live driver-search + assignment
- Active trip screen + SOS
- Rating + comments
- Profile + trip history
- Notification center

### Weeks 10–11 · Captain Flutter App
- Login screen (phone + admin-issued password, forced change on first login)
- Home with online/offline + earnings today
- Zone selector before going online
- Trip offer popup (full screen, 35-s countdown)
- Trip lifecycle buttons (start + complete + no-show)
- Discipline warning banner (5 rejections/day)
- Earnings + weekly graph
- Profile with lifetime stats
- Trip history

_Note: the multi-step registration wizard (originally weeks 10–11) is **moved to the admin dashboard in weeks 6–7** per Decision #13._

### Week 12 · Load Testing + Polish + Launch
- Simulate 500 concurrent captains streaming heartbeat
- Simulate 500 concurrent bookings across zones
- Fix P95 latency issues
- Sentry integration
- Production deploy on Railway (backend + Redis + Postgres)
- Meta production webhook switchover
- App Store + Play Store submissions
- Onboard first 20 captains (soft launch)
- Public launch

---

## 18. Cost Breakdown

### Fixed monthly costs

| Item | Cost |
|---|---|
| Railway backend (starter → pro) | **$20–40** |
| Railway PostgreSQL managed | **$10–20** |
| Railway Redis managed | **$10** |
| Sentry (free tier initially) | $0 |
| Firebase (FCM push) | $0 (unlimited free) |
| Meta Cloud API access | $0 (only pay per message) |
| Domain + SSL | $12/year (~$1/month) |
| **Total fixed** | **≈ $40–70/month** |

### Variable monthly costs (at 10 K customers, 2 trips/customer/month)

| Item | Calculation | Cost |
|---|---|---|
| WhatsApp Utility messages | 40 K × $0.0036 | **≈ $144** |
| Gemini AI parses | 5 K × $0.00007 | **≈ $0.35** |
| Bandwidth | ~50 GB | Included on Railway |
| **Total variable** | | **≈ $145** |

### Grand total at scale

**≈ $185–215 per month** to run the entire platform (backend + WhatsApp + AI). Roughly **11,000 EGP/month**.

At 15 % commission on ~20,000 monthly trips × avg 30 EGP fare = **90,000 EGP/month gross profit**. Very healthy margin.

---

## 19. Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Phone-only auth → fake accounts / abuse | **High** | High | Prepare code path for OTP; enable after MVP |
| 2 | WhatsApp number gets banned again | Medium | Critical | Cloud API + templates + opt-in — this is the whole point of the plan |
| 3 | Gemini API downtime | Low | Medium | Human handoff already built |
| 4 | Captain accepts but doesn't show up | Medium | Medium | Reputation system + auto-suspend below 3.5 rating |
| 5 | Sudden rush hour spike (Ramadan iftar time) | High | High | Load test at 5× baseline; auto-scale Railway |
| 6 | Zone list gets outdated (city grows) | Low | Low | Admin CRUD makes this a 30-second update |
| 7 | Postgres becomes bottleneck | Low | High | Read replica + query optimization; ride hot path is Redis anyway |
| 8 | Meta rate-limits our number | Medium | High | Ramp up messaging tier gradually; monitor Meta quality score |
| 9 | Fraudulent trip completion | Medium | Medium | Customer confirmation ping ("did you take this trip?") next-day |
| 10 | Flutter app rejected by App Store | Medium | Medium | Follow guidelines; TestFlight beta before submission |

---

## 20. Success Metrics / KPIs

**Product KPIs** (measure weekly)
- Trips completed per day
- Trip completion rate (% not cancelled)
- Median matching time
- % rides matched in same zone (vs expanded)
- % rides needing AI handoff
- Customer rating avg
- Captain rating avg
- 7-day customer retention
- 30-day captain retention

**Technical KPIs** (measure daily)
- API P95 latency
- WebSocket disconnection rate
- WhatsApp webhook success rate
- Gemini timeout rate
- Sentry error count
- Database CPU / Redis memory

**Business KPIs** (measure monthly)
- Total revenue (sum of fares)
- Wassalny cut (commission_egp)
- Active captains count
- Active customers count (booked at least 1 trip in last 30 days)
- Cost per trip (infra + WhatsApp + AI ÷ trips)

---

## 21. Open Questions

**All 12 original open questions have been answered ✅** (see [§4 Confirmed Product Decisions](#4-confirmed-product-decisions) for the full locked-in list).

### Data still to be provided by Ibrahim (blockers per phase)

| # | Item | Blocking phase | Notes |
|---|---|---|---|
| A | Rough zone-to-zone prices (from → to → EGP) | Phase 1 seeding | Ibrahim to send in a message. Seeded into `zone_pricing` table |
| B | WhatsApp sticker files (.webp, 512×512) — booked, captain-coming, completed, no-driver | Phase 3 | Ibrahim has these designed; will send files |
| C | Full list of Benha neighborhoods (Arabic + English) | **Before real launch (Week 12)** | 5 test zones are enough for Phase 1–6. Ibrahim to compile before soft launch |
| D | Gemini API key (`GEMINI_API_KEY`) | Phase 3 | Ibrahim has Google Cloud billing enabled — just generate key |
| E | Firebase service account JSON + FCM sender ID | Phase 5 | Ibrahim has FCM project — download from Firebase console |
| F | Apple Developer team ID + Play Console access | Week 12 launch | Ibrahim has both accounts |
| G | Real business phone number for WhatsApp (a fresh SIM, not the banned one) | Week 12 launch | Register in Meta as per §Step 10 of WhatsApp setup guide |

### Resolved answers (from discovery session on 2026-07-18)

- Commission = 15 %
- Hours = 24/7 always
- Rejection tolerance = warn @ 5/day, suspend @ 10/day
- Captain onboarding = **admin only** (no self-registration flow in the captain app)
- No-show fee = 10 EGP added to customer's next trip
- Zone list at launch = 5 test zones (Ramla, Downtown, University, Sarayat, Damanhour Road)
- Zone-to-zone pricing = Ibrahim provides rough prices, seeded into matrix
- Zone adjacency = fully connected (every zone adjacent to every other)
- Stickers = custom-designed by Ibrahim, sent as .webp files
- Gemini account = ✅ available
- Firebase account = ✅ available
- App store accounts = ✅ both available

---

## 22. Post-Launch Roadmap

Not part of v1 but plan for these in months 4–12:

- **Online payments** — Paymob / Fawry integration
- **Customer wallet** — top-up + loyalty points
- **Captain wallet + weekly settlement automation**
- **Scheduled rides** — book for tomorrow 8 AM
- **Multi-city expansion** — beyond Benha, use `city` field on zones
- **Corporate accounts** — companies pay monthly for employees
- **Delivery mode** — same platform for package delivery
- **Bike / tuk-tuk categories** — cheaper options for short trips
- **Full maps mode (optional)** — for customers who prefer it
- **Referral program** — captain-refers-captain and customer-refers-customer
- **In-app support chat** — team responds via admin dashboard
- **iOS + Android app store optimization** — Arabic ASO

---

## Appendix A — File Layout (target)

```
wassalny/
├── PLAN.md                          ← this file
├── design_reference/                ✅ all 59 design PNGs (offline copy of the design site)
├── README.md
├── requirements.txt
├── Procfile
├── config.py
├── wsgi.py
├── app/
│   ├── __init__.py
│   ├── models/
│   │   ├── user.py                  ✅ built
│   │   ├── customer.py              ✅ built
│   │   ├── driver.py                ✅ built
│   │   ├── conversation.py          ✅ built
│   │   ├── message.py               ✅ built
│   │   ├── ride_request.py          ✅ built (renamed to Ride in Phase 1)
│   │   ├── message_template.py      ✅ built
│   │   ├── zone.py                  🔜 Phase 1
│   │   ├── zone_pricing.py          🔜 Phase 1
│   │   ├── broadcast.py             🔜 Phase 2
│   │   ├── ai_session.py            🔜 Phase 3
│   │   ├── sticker.py               🔜 Phase 3
│   │   ├── admin_alert.py           🔜 Phase 3
│   │   ├── ride_status_event.py     🔜 Phase 2
│   │   ├── earnings.py              🔜 Phase 6
│   │   └── notification.py          🔜 Phase 4
│   ├── services/
│   │   ├── whatsapp.py              ✅ built
│   │   ├── inbox.py                 ✅ built
│   │   ├── availability.py          🔜 Phase 1
│   │   ├── matching.py              🔜 Phase 2
│   │   ├── ride_lifecycle.py        🔜 Phase 2
│   │   ├── ai_parser.py             🔜 Phase 3
│   │   ├── stickers.py              🔜 Phase 3
│   │   ├── pricing.py               🔜 Phase 1
│   │   ├── notifications.py         🔜 Phase 4
│   │   └── reports.py               🔜 Phase 6
│   ├── routes/
│   │   ├── auth.py                  ✅ built
│   │   ├── inbox.py                 ✅ built
│   │   ├── drivers.py               ✅ built
│   │   ├── rides.py                 ✅ built (expand in Phase 2)
│   │   ├── users.py                 ✅ built
│   │   ├── webhook.py               ✅ built (extend in Phase 3)
│   │   ├── zones.py                 🔜 Phase 1
│   │   ├── pricing.py               🔜 Phase 1
│   │   ├── stickers.py              🔜 Phase 3
│   │   ├── alerts.py                🔜 Phase 3
│   │   └── reports.py               🔜 Phase 6
│   ├── api/
│   │   └── v1.py                    ✅ built (extend each phase)
│   ├── sockets/
│   │   ├── inbox_socket.py          ✅ built
│   │   ├── driver_socket.py         🔜 Phase 1
│   │   ├── customer_socket.py       🔜 Phase 2
│   │   └── admin_socket.py          🔜 Phase 6
│   ├── workers/
│   │   ├── whatsapp_processor.py    🔜 Phase 3
│   │   ├── ai_parser_worker.py      🔜 Phase 3
│   │   └── earnings_rollup.py       🔜 Phase 6
│   ├── templates/                   ← Jinja2 admin dashboard
│   └── static/
├── mobile/                          🔜 Phase 8+
│   ├── customer_app/                ← Flutter project
│   └── captain_app/                 ← Flutter project
└── ops/
    ├── loadtest/                    🔜 Phase 4
    └── seed_zones.py                🔜 Phase 1
```

---

## Appendix B — Environment Variables (final)

```env
# Flask
SECRET_KEY=
JWT_SECRET_KEY=
FLASK_ENV=production

# Databases
DATABASE_URL=postgresql://...
REDIS_URL=redis://...

# WhatsApp Cloud API
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_BUSINESS_ACCOUNT_ID=
WHATSAPP_VERIFY_TOKEN=
WHATSAPP_APP_SECRET=
WHATSAPP_API_VERSION=v21.0

# Gemini AI
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash

# Firebase
FCM_PROJECT_ID=
FCM_SERVICE_ACCOUNT_JSON=       # base64-encoded service account

# Monitoring
SENTRY_DSN=

# Business (per Confirmed Decisions §4)
WASSALNY_COMMISSION_RATE=0.15
BROADCAST_ACCEPT_WINDOW_SECONDS=10
DRIVER_HEARTBEAT_TIMEOUT_SECONDS=60
CUSTOMER_RATE_LIMIT_PER_10MIN=3
NO_SHOW_FEE_EGP=10
NO_SHOW_ENABLE_AFTER_MINUTES=5
CAPTAIN_REJECT_WARN_THRESHOLD=5
CAPTAIN_REJECT_SUSPEND_THRESHOLD=10
CAPTAIN_SUSPEND_HOURS=24
OPERATING_MODE=always_on   # or business_hours

# Initial admin
ADMIN_EMAIL=admin@wassalny.com
ADMIN_PASSWORD=
```

---

## Appendix C — Seed Data (Phase 1 starter)

Applied by `ops/seed_zones.py` on first deploy. Real Benha list replaces this before launch (Open Question C).

### Test zones (5)

| slug | name_ar | name_en |
|---|---|---|
| ramla | الرملة | El Ramla |
| downtown | وسط البلد | Downtown |
| university | جامعة بنها | Benha University |
| sarayat | السرايات | El Sarayat |
| damanhour_road | طريق دمنهور | Damanhour Road |

### Placeholder pricing matrix (25 pairs including same-zone)

Ibrahim will overwrite with real prices via admin dashboard. Placeholder formula: **20 EGP flat + 5 EGP if different zone**.

| from → to | Ramla | Downtown | University | Sarayat | Damanhour |
|---|---|---|---|---|---|
| **Ramla** | 20 | 25 | 25 | 25 | 25 |
| **Downtown** | 25 | 20 | 25 | 25 | 25 |
| **University** | 25 | 25 | 20 | 25 | 25 |
| **Sarayat** | 25 | 25 | 25 | 20 | 25 |
| **Damanhour** | 25 | 25 | 25 | 25 | 20 |

### Placeholder adjacency

Per Decision #15: **all zones are adjacent to all others** — no explicit adjacency table needed in v1. If the 3 broadcast rounds all fail, ride is auto-cancelled with reason `no_driver_available`.

### Placeholder stickers (until Ibrahim sends real files)

Use text + emoji fallback:
- `booked` → "🚗 تم استلام طلبك، بندور على كابتن..."
- `captain_coming` → "✅ الكابتن {name} جاي، رقمه {phone}"
- `completed` → "🚩 وصلت بأمان، شكراً لاستخدامك وصلني"
- `no_driver` → "🙏 معلش، مفيش عربية دلوقتي، جرب تاني بعد شوية"

---

**End of plan.** Next step: review this document with the team, answer the [Open Questions](#21-open-questions), and kick off Phase 1.
