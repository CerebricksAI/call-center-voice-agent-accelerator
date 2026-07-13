# Telephony E2E Spec — isolated Twilio phone path

This document describes how the **telephone** integration works end to end, what is
in scope vs out of scope, and how to test it (unit + live call).

The phone path lives under `server/telephone/` and is mounted from `server.py` at:

| Route | Role |
|-------|------|
| `POST/GET /telephone/voice` | Twilio voice webhook → TwiML Media Stream |
| `WS /telephone/ws` | Twilio Media Streams ↔ Azure Voice Live |

It is **not** the accelerator’s auto-detected Twilio provider (`TWILIO_AUTH_TOKEN` →
`/voice` + `/twilio/ws`). Those routes stay dormant unless that env var is set.

---

## 1. Goals

1. Accept an inbound PSTN call on a Twilio number.
2. Bridge caller audio to Azure Voice Live (Maya).
3. Bridge agent audio back to the caller.
4. Do so without registering the built-in Twilio provider or colliding with ACS/etc.

---

## 2. Architecture (single process)

```
Caller phone
    │
    ▼
Twilio PSTN
    │  POST /telephone/voice  (signature validated)
    ▼
server.py stub  →  telephone.handlers.handle_telephone_voice
    │  TwiML: <Connect><Stream url="wss://…/telephone/ws"/>
    ▼
Twilio Media Streams
    │  WebSocket /telephone/ws
    ▼
telephone.handlers.handle_telephone_ws
    │  TwilioMediaHandler (mulaw 8 kHz ↔ PCM 24 kHz)
    ▼
run_call_loop  →  Azure Voice Live
```

**Code split**

| Layer | Location | Responsibility |
|-------|----------|----------------|
| Routes (stubs only) | `server/server.py` | Register `/telephone/*`, pass `request` / `websocket` / `call_manager` |
| Config | `server/telephone/config.py` | Read `TELEPHONE_TWILIO_*`; feature off if token unset/placeholder |
| Handlers | `server/telephone/handlers.py` | Signature check, TwiML, WS acquire/loop/release |
| Reused (unchanged) | `app.providers.twilio.*`, `app.call_loop`, `app.call_manager` | Validation, media bridge, call loop |

**Env isolation**

- Use `TELEPHONE_TWILIO_AUTH_TOKEN` (required) and optional `TELEPHONE_TWILIO_PHONE_NUMBER`.
- Do **not** set `TWILIO_AUTH_TOKEN` for this path — that triggers provider auto-detection.
- Handlers pass a **local dict** key `"TWILIO_AUTH_TOKEN"` into existing Twilio classes;
  that is not an environment variable.

**Auth**

- `/telephone/` is a public path prefix (`app/auth.py`) so Twilio webhooks are not
  redirected to login.

---

## 3. Call sequence (happy path)

1. Caller dials the Twilio number configured in the Twilio Console.
2. Twilio `POST`s to `https://<public-host>/telephone/voice` with `X-Twilio-Signature`.
3. `get_telephone_config()` loads; if missing → **503**.
4. `TwilioEventHandler.validate_request` runs:
   - unavailable → **503**
   - invalid → **403**
5. App returns TwiML that:
   - Says a short wait prompt
   - Opens a bidirectional Media Stream to `wss://<public-host>/telephone/ws`
   - Passes a short-lived HMAC `token` custom parameter
6. Twilio opens the WebSocket; `TwilioMediaHandler.authenticate_and_start()` waits for
   `connected` / `start` and verifies the token.
7. `call_manager.acquire(call_id, "telephone")` — failure → WS close **4429**.
8. `run_call_loop(...)` bridges audio until hangup/error/cancel.
9. `finally`: `call_manager.release` + `handler.cleanup()`.

---

## 4. Behavior vs web client

| Aspect | Web (`/web/ws`) | Telephone (`/telephone/ws`) |
|--------|-----------------|-----------------------------|
| Transport | Browser WebSocket + PCM | Twilio Media Streams + mulaw |
| Voice engine | Azure Voice Live | Same |
| Call loop | `run_call_loop` | Same |
| Handler | `OrchestratedWebHandler` when `ORCHESTRATOR_ENABLED` | `TwilioMediaHandler` only |
| Skills / FSM / tools | Yes (orchestrator on) | **No** — not wired on this path |
| Greeting | App / orchestrator | Twilio “please wait…” then Voice Live persona |

E2E telephony testing verifies **connectivity + voice conversation**, not orchestrator
compliance evals. Those remain in `server/evals/`.

---

## 5. Prerequisites

### Environment (`server/.env`)

```env
# Required for Voice Live (already used by web)
AZURE_VOICE_LIVE_API_KEY=...
AZURE_VOICE_LIVE_ENDPOINT=...
VOICE_LIVE_MODEL=gpt-4o-mini

# Telephone integration (isolated)
TELEPHONE_TWILIO_AUTH_TOKEN=<real Twilio Auth Token>
TELEPHONE_TWILIO_PHONE_NUMBER=+1XXXXXXXXXX

# Leave unset / commented so provider auto-detect stays off:
# TWILIO_AUTH_TOKEN=...
```

Placeholder values like `<Twilio Auth Token for the phone number>` are treated as
**unset** (feature off → 503).

### Packages

```bash
cd server
uv sync --extra twilio
```

### Public HTTPS URL

Twilio cannot reach `localhost`. Use a tunnel (e.g. ngrok) to the server port
(default **8000** if `PORT` is unset):

```bash
uv run server.py          # terminal 1
ngrok http 8000           # terminal 2
```

### Twilio Console

Phone number → Voice → **A call comes in**:

- Webhook URL: `https://<ngrok-host>/telephone/voice`
- Method: `HTTP POST`
- Save configuration

---

## 6. Test plan

### 6.1 Unit tests (offline)

```bash
cd server
uv run python -m pytest tests/test_telephone.py -v
```

Coverage (mocked Twilio / Voice Live):

| Case | Expect |
|------|--------|
| Token unset / placeholder / blank | `get_telephone_config()` → `None` |
| Token set | Config dataclass with auth token |
| `TELEPHONE_*` alone | Does not set `TWILIO_AUTH_TOKEN` env |
| Voice, no config | **503** |
| Voice, bad signature | **403** |
| Voice, valid | **200** + TwiML containing `/telephone/ws` |
| Host `http://` | Rewritten to `wss://…/telephone/ws` |
| WS, no config | Close **4503** |
| WS, acquire fail | Close **4429** |
| WS, success | `run_call_loop` then release + cleanup |

**Pass gate before live dial:** all tests green.

### 6.2 Smoke (tunnel up, before dialing)

| Check | Expect |
|-------|--------|
| `GET http://127.0.0.1:<port>/health` | `{"status":"healthy"}` |
| Browser `https://<ngrok>/health` | After ngrok free interstitial (“Visit Site”), same JSON |
| Server log | `No telephony provider credentials found` (old Twilio provider off) |
| Routes registered | `/telephone/voice` and `/telephone/ws` always (stubs), independent of provider detect |

### 6.3 Live E2E (PSTN)

1. Keep `uv run server.py` and `ngrok http <port>` running.
2. Confirm Twilio webhook URL matches the **current** ngrok host + `/telephone/voice`.
3. Dial `TELEPHONE_TWILIO_PHONE_NUMBER` from a real phone.
4. **Expected caller experience**
   - Short Twilio wait prompt
   - Agent (Maya / Voice Live) speaks
   - Caller can talk; agent responds (barge-in / short turns as Voice Live allows)
5. **Expected server logs**
   - `Telephone /voice webhook` (optionally with number)
   - `Incoming telephone Media Stream WebSocket connection`
   - Voice Live session activity; clean release on hangup
6. Hang up; confirm no orphaned call in logs (release/cleanup ran).

### 6.4 Negative / failure matrix

| Condition | Symptom |
|-----------|---------|
| Token unset / placeholder | Voice → **503**; WS closes **4503** |
| Wrong auth token or URL Twilio signed ≠ reconstructed URL | Voice → **403** |
| ngrok offline / wrong host in Twilio | Twilio error; no server log |
| Port mismatch (ngrok ≠ server `PORT`) | Tunnel 502 / no app traffic |
| `TWILIO_AUTH_TOKEN` also set | Provider may register `/voice` — avoid for this test |
| Too many concurrent calls | WS **4429** |
| Voice Live creds bad | Stream connects; little/no agent audio; handler errors in logs |

---

## 7. Acceptance criteria (E2E done)

- [ ] `tests/test_telephone.py` all pass
- [ ] Inbound call to Twilio number reaches `/telephone/voice` (log line present)
- [ ] Media Stream reaches `/telephone/ws` (log line present)
- [ ] Caller hears agent speech and can converse
- [ ] Hangup cleans up without process crash
- [ ] `TWILIO_AUTH_TOKEN` unset; no duplicate `/voice` provider registration

---

## 8. Out of scope (this path)

- Orchestrator skills / FSM / tool gates on phone (web-only today)
- ACS / Infobip / Genesys providers
- Changing `app/providers/twilio/` behavior for the auto-detected provider
- CI live dial against real Twilio (manual / staging only)

---

## 9. Quick reference commands

```bash
# Install + unit tests
cd server
uv sync --extra twilio
uv run python -m pytest tests/test_telephone.py -v

# Local run
uv run server.py

# Public tunnel (match server port)
ngrok http 8000
```

Twilio voice webhook:

```text
https://<your-ngrok-host>/telephone/voice
```
