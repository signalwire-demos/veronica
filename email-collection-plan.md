# Email & Address Collection via Voice AI
## Architecture Plan — PGI Decision Tree

**SignalWire / Mars Investigations Demo**  
All routing is deterministic. The LLM operates within governed context built from pre-call enrichment. It does not make routing decisions.

---

## PRE-CALL

> Single Trestle Reverse Phone API call at answer. ANI lookup hits your data store first. Full API enrichment only runs on first-time callers or stale records.

### Step 1 — Inbound Call Received
ANI captured from SIP/PSTN.

### Step 2 — Data Store Lookup (ANI Key)
Query internal store for an existing validated record on this number. This runs before any external API call. Hit rate grows with call volume.

**Decision: Validated record found?**
- **Yes** → check staleness
- **No (first-time caller)** → full Trestle enrichment

---

### RETURNING CALLER PATH

**Decision: Record staleness?**
- **Fresh (within TTL policy)** → preload call state from stored record. No external API calls.
- **Stale / TTL expired** → re-run Trestle Reverse Phone API, compare delta, flag significant changes

**Recommended TTL defaults:**
- Address: 90 days
- Email: 180 days
- Line type: 180 days

---

### NEW CALLER PATH — Full Enrichment

**Trestle Reverse Phone API**
One call returns: line type, owner name, probable email(s), current address. No separate line type lookup needed.

**Decision: Line type?**
- **Confirmed Mobile** → sms_eligible = true
- **VOIP / Unknown** → sms_eligible = false (SMS deliverability unpredictable)
- **Landline** → sms_eligible = false (no SMS capability)

**Decision: Probable email returned?**
- **Yes** → store as candidate_email
- **No** → will collect during call

**Decision: Address returned?**
- **Yes** → geocode via Google Maps API
- **No** → will collect during call

**Google Maps Geocoding API**
Normalize address string, get lat/lng, check confidence score. Low confidence = treat as unverified.

**Smarty US Street Address API** *(Free: 250 lookups/month + 42-day 1,000 lookup trial)*
CASS-certified USPS check on geocoded address.
- `Y` = deliverable
- `S` = missing secondary (apartment/suite)
- `N` = not deliverable

---

### Step 3 — Populate Call State

```
line_type
sms_eligible
owner_name
candidate_email          (source: trestle / stored record)
candidate_address        (source: trestle / stored record)
geocode_result
geocode_confidence
dpv_match_code           (Y / S / N)
record_source            (new / returning / refreshed)
consent_flag             = null
sms_consent_flag         = null
follow_up_required       = false
```

LLM receives governed context built from this. No ad-libbing on routing.

---

## DURING CALL

> Identity confirmed via Trestle name and address greeting before any data collection. ZeroBounce runs mid-call on confirmed email. SMS consent includes mandatory rate disclosure. Email send consent is a separate hard branch.

---

### GREETING + IDENTITY CONFIRMATION

**LLM greets and confirms identity**
Uses `owner_name` from call state. Scripted: *"Hi, am I speaking with [name]?"*
- Returning callers: greeting uses validated record
- New callers: greeting uses Trestle candidate
- No name available: greet generically, skip to address check

**Decision: Caller confirms name?**
- **Yes** → proceed to address confirmation
- **No / different person** → store identity_mismatch=true in call state, continue with elevated fraud threshold. Do not abandon the call.

**Decision: Trestle address in call state?**
- **Yes** → ask: *"Do you still live at [normalized address]?"* (uses Smarty-normalized form)
- **No** → skip to email collection

**Decision: Caller still at address?**
- **Yes** → address confirmed, skip address collection entirely
- **No** → collect updated address during call
- **Decline to confirm** → note it, move on, no friction

---

### EMAIL COLLECTION

**Decision: Validated or candidate email in call state?**
- **Yes** → EMAIL CONFIRM PATH
- **No** → EMAIL COLLECTION PATH

---

#### EMAIL CONFIRM PATH

LLM offers known email casually. Scripted, not ad-libbed.

**Decision: Caller accepts candidate?**
- **Yes** → proceed to ZeroBounce check
- **No** → collect different email (fall to collection path)

---

#### EMAIL COLLECTION PATH

**Decision: SMS eligible from call state?**
- **Yes (confirmed mobile)** → SMS PATH
- **No (landline or VOIP)** → VOICE SPELLING PATH

---

##### SMS PATH

**Request SMS consent — VERBATIM DISCLOSURE REQUIRED**

> *"May I send you a text message to collect your email address? Message and data rates may apply. You okay with that?"*

Do not paraphrase. Do not omit the rate disclosure.

**Decision: Caller consents?**
- **Yes** → store sms_consent_flag=true + timestamp, send tokenized SMS link
- **No** → store sms_consent_flag=false, fall to voice spelling. No retry on SMS.

**Send SMS with short-lived tokenized link**
Voice session held open. State machine waits on webhook for form completion.

**Decision: Webhook received within timeout?**
- **Yes** → email captured via form, proceed to ZeroBounce
- **Timeout** → fall to voice spelling

---

##### VOICE SPELLING PATH

LLM prompts for spelled email. *"Spell it out for me."*

ASR capture + normalization:
- `at sign` → `@`
- `dot com` → `.com`
- `dash` / `hyphen` → `-`
- `underscore` → `_`

LLM reads back in NATO phonetics for confirmation.

**Decision: Caller confirms?**
- **Yes** → proceed to ZeroBounce
- **No** → retry (max 2 retries)
- **3rd failure** → store follow_up_required=true, reason=spelling_failure. Schedule outbound follow-up. Continue call without valid email. Do not loop.

---

### ZEROBOUNCE — REAL-TIME EMAIL VALIDATION

*(Free: 100 credits/month, replenished monthly)*

Runs while caller is still on the line. MX lookup, SMTP probe, disposable/role/spam-trap detection.

**Decision: ZeroBounce result?**
- **Valid** → proceed to email send consent
- **Invalid / disposable / role account** → LLM informs caller and requests correction
- **Unknown / catch-all** → store zb_status=unknown + low-confidence flag, proceed. Post-call re-check will run.

**On invalid result — LLM scripted response:**
> *"That one's not checking out on my end. You got another one I can try?"*

**Decision: Retry count?**
- **Under limit** → re-collect email
- **Limit hit (2 retries)** → store follow_up_required=true, reason=email_validation_failed. Schedule outbound. Continue call.

---

### EMAIL SEND CONSENT

**HARD BRANCH — NOT A SOFT ASSUMPTION**

> *"Would you like me to send a confirmation to that email address?"*

- **Yes** → consent_flag=true + timestamp stored
- **No** → consent_flag=false. Respect it. Do not ask again.

Consent flag, response transcript, and timestamp all stored in call state. This is your audit trail.

---

### ADDRESS COLLECTION

**Decision: Verified address in call state?**
- **Geocoded + Smarty Y match** → ADDRESS CONFIRM PATH
- **Low confidence, S/N match, or missing** → ADDRESS COLLECTION PATH

---

#### ADDRESS CONFIRM PATH

LLM offers Smarty-normalized address for confirmation. Not the raw Trestle string.

**Decision: Caller confirms?**
- **Yes** → address locked in call state
- **No** → collect corrected address

---

#### ADDRESS COLLECTION PATH

LLM collects full address as a single natural utterance. Reads it back verbatim for confirmation.

**Decision: Caller confirms?**
- **Yes** → geocode immediately
- **No** → re-collect

**Google Maps Geocoding API** — runs on confirmed address string. Google handles natural language input well.

**Smarty USPS deliverability check** — same pipeline as pre-call path. dpv_match_code stored in call state.

**Decision: Geocode + Smarty result?**
- **High confidence + Smarty Y** → store as verified
- **Low confidence or S/N match** → store as unverified, flag for review

---

## POST-CALL

> Async pipeline fires after hangup. ZeroBounce re-check runs only on unknowns — valid mid-call results are reused. Outbound follow-up call scheduled if email unresolved. Postmark fires only on explicit consent + valid email.

---

### EMAIL — POST-CALL PROCESSING

**Syntax check** — regex + format. Fail fast. ZeroBounce already ran during call; this is a safety net.

**Decision: ZeroBounce status from call state?**
- **Valid** → skip re-check, proceed to delta analysis
- **Unknown / catch-all** → ZeroBounce secondary check. Some unknowns resolve on a second attempt.
- **follow_up_required=true** → schedule outbound follow-up call

**Outbound follow-up call context:**
- Greet by name, reference prior call
- Collect email only — targeted, not a full intake
- ZeroBounce check runs real-time on whatever is collected
- Has its own SMS consent gate if texting is used

**Delta analysis**
If final email differs from Trestle candidate: record both + delta in payload. Large divergence = fraud signal. Small divergence = likely stale Trestle data.

---

### ADDRESS — POST-CALL PROCESSING

**Decision: Final address already geocoded + Smarty-checked during call?**
- **Yes** → reuse stored result, run delta only
- **No (address changed or pre-call check skipped)** → re-geocode + Smarty check on final address

**Delta analysis**
Compare final address to Trestle candidate. Flag significant divergence. Store dpv_match_code, geocode_confidence, and source (trestle-confirmed / caller-provided) in payload.

---

### IDENTITY CORRELATION

**Trestle Real Contact API**
Confirm final email + address correlate to the calling number. Runs once on final confirmed values.

**Decision: Correlation result?**
- **Strong match** → proceed to Postmark gate
- **Weak / no match** → flag for human review. Do not suppress record — absence of correlation is not proof of fraud.

---

### POSTMARK SEND GATE

*(Free: 100 emails/month, never expires. Paid from $15/month for 10k.)*

Two conditions must both be true before Postmark fires:

**Decision: consent_flag in call state?**
- **true** → check deliverability gate
- **false** → suppress send
- **follow_up_required=true** → suppress send (no valid email to send to)

**Decision: Email passed ZeroBounce?**
- **Yes** → fire Postmark transactional send (async, post-hangup)
- **No** → suppress send, log failure

Log `MessageID` from Postmark in post-call payload as audit trail.

---

### WRITE TO DATA STORE + CRM

Full post-call payload:

```
validated_email
validated_address
lat / lng
geocode_confidence
dpv_match_code
trestle_correlation_score
trestle_candidates            (original candidates for delta reference)
delta_flags
line_type
sms_eligible
sms_consent_flag
sms_consent_timestamp
email_consent_flag
email_consent_timestamp
postmark_message_id           (if sent)
collection_source             (trestle-confirmed / caller-provided / sms-form)
identity_mismatch_flag
follow_up_required
follow_up_reason
last_validated_at
record_source                 (new / returning / refreshed)
```

**Data store upserted by ANI.** Next call preloads from here. No API spend on clean returning callers.

---

## API SURFACE SUMMARY

| Phase | Vendor | Free Tier | Purpose |
|-------|--------|-----------|---------|
| PRE | Internal data store (ANI key) | — | First lookup every call. Eliminates redundant API spend on returning callers. |
| PRE | Trestle Reverse Phone API | — | New/stale callers only. Line type + name + candidate email + address in one call. |
| PRE | Google Maps Geocoding API | — | Normalize address, lat/lng, confidence score. |
| PRE | Smarty US Street Address API | 250/mo + 42-day trial | USPS CASS deliverability on Trestle address. dpv_match_code. |
| DURING | SignalWire Messaging API | — | SMS tokenized link (mobile + consent only). |
| DURING | Google Maps Geocoding API | — | Geocode caller-provided address on confirmation. |
| DURING | Smarty US Street Address API | 250/mo | USPS deliverability on caller-provided address. |
| DURING | ZeroBounce (real-time) | 100/mo recurring | Email validation mid-call. Invalid triggers re-collection before hangup. |
| POST | ZeroBounce (secondary) | — | Re-check on unknown/catch-all only. Skipped if during-call result was valid. |
| POST | Trestle Real Contact API | — | Identity correlation — email + address to phone number. |
| POST | Postmark | 100 emails/mo | Transactional send. Fires only on explicit consent + valid email. |
