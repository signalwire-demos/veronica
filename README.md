# Veronica Mars — Voice AI Agent

A SignalWire voice AI agent that collects and validates email addresses and physical addresses from inbound callers, using the persona of Veronica Mars, private investigator.

**All routing logic lives in code. The LLM only handles personality and natural language.**

## Architecture

State machine with tool-forced transitions — the AI never decides where to go. Every step has `valid_steps=[]` and tools call `swml_change_step()` to route deterministically.

```
veronica/
├── veronica.py          # VeronicaAgent — state machine, tools, per-call config
├── state_store.py       # SQLite — callers, call_state, consent_log
├── api_clients.py       # Trestle, ZeroBounce, Google Maps, Smarty, Postmark
├── config.py            # Env vars with defaults
├── requirements.txt     # signalwire-agents, requests, python-dotenv
├── .env.example         # Template
└── calls/               # Post-call JSON saved by on_summary
```

## Call Flow (Phase 1)

```
greeting ──[confirm_identity]──┬── email_confirm    (candidate email on file)
                               └── email_collection (no email)

email_confirm ──[process_email_confirmation]──┬── zerobounce_check (accepted)
                                              └── email_collection (rejected)

email_collection ──[initiate_email_collection]──── voice_spelling

voice_spelling ──[submit_spelled_email]──┬── zerobounce_check (confirmed)
                                         ├── voice_spelling   (retry, <3 attempts)
                                         └── wrap_up          (3rd fail)

zerobounce_check ──[validate_email]──┬── email_send_consent (valid/unknown)
                                     ├── email_collection   (invalid, retries left)
                                     └── wrap_up            (retries exhausted)

email_send_consent ──[process_email_consent]──── wrap_up
```

## Pre-Call Enrichment

Runs before every call in `_per_call_config`:

1. **Data store lookup** (ANI key) — returning callers skip API calls
2. **Trestle Reverse Phone API** — name, email, address, line type
3. **Google Maps Geocoding** — normalize address, lat/lng, confidence
4. **Smarty US Street Address** — USPS CASS deliverability (dpv_match_code)

Results cached in SQLite `callers` table. Returning callers with fresh records hit zero external APIs. Stale records get re-enriched with delta tracking.

## APIs

| Vendor | Phase | Free Tier | Purpose |
|--------|-------|-----------|---------|
| Trestle Reverse Phone | 1 | — | Name, email, address, line type from ANI |
| ZeroBounce | 1 | 100/mo | Real-time email validation mid-call |
| Postmark | 1 | 100/mo | Confirmation email on consent |
| Google Maps Geocoding | 1 | — | Address normalization + lat/lng |
| Smarty US Street | 2 | 250/mo | USPS deliverability check |
| SignalWire Messaging | 3 | — | SMS tokenized link for email collection |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in API keys in .env
python3 veronica.py
```

SWML endpoint will be available at `http://localhost:3000/swml`. Point a SignalWire phone number at it.

## Environment Variables

See `.env.example` for the full list. Minimum for Phase 1:

- `TRESTLE_API_KEY` — Reverse phone enrichment
- `ZEROBOUNCE_API_KEY` — Email validation
- `SIGNALWIRE_PHONE_NUMBER` — Your SignalWire number
- `POSTMARK_SERVER_TOKEN` + `POSTMARK_FROM_EMAIL` — Confirmation emails
- `GOOGLE_MAPS_API_KEY` — Address geocoding

## Consent Tracking

All consent is logged to `consent_log` table with phone, call_id, type (sms/email_send), boolean, and timestamp. The agent will not send an email without explicit verbal consent.

## Phased Build

- **Phase 1** (current): Voice-only email collection, ZeroBounce validation, Postmark send, geocode enrichment
- **Phase 2**: Address confirmation + collection with Google Maps + Smarty
- **Phase 3**: SMS email collection path (tokenized link for mobile callers)
- **Phase 4**: Post-call async pipeline (re-check unknowns, Trestle Real Contact correlation)
- **Phase 5**: Dashboard + follow-up trigger

## Debug

SWML output is printed to stderr on every call. Call data is saved to `calls/{call_id}.json` after hangup. Full pre-call enrichment is logged with every API call result.
