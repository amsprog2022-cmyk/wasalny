# Brand stickers

WhatsApp brand stickers sent by the Wassalny bot at key moments in a trip.
Registered in the DB by `ops/seed_stickers.py` — one row per `purpose`.

## Naming convention

`<purpose>.<ext>` — e.g. `captain_coming.png`, `booked.webp`.

Any of `.png`, `.webp` works locally, but Meta WhatsApp Cloud API requires
`.webp` at 512×512 for real sticker messages. When we go to production the
sticker uploader in Phase 3 will convert automatically.

## Slots (per PLAN.md §Appendix C)

| purpose | sent when |
|---|---|
| `booked` | trip received, searching for a captain |
| `captain_coming` | captain assigned, on their way — **provided by Ibrahim 2026-07-18** |
| `completed` | trip finished safely |
| `no_driver` | no captain available now |
| `generic` | brand touchpoints (onboarding reply, etc.) |

## What to drop in this folder

Save the "24/7 · أقرب كابتن هيكلمك · 01029188887" branded PNG that Ibrahim sent as:

```
wassalny/stickers/captain_coming.png
```

Then run `python -m ops.seed_stickers` to register it.
