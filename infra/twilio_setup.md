# Telephony wiring: Twilio → LiveKit → Sangam agent

This is the part that can't be faked with a browser microphone. Judges'
rules explicitly call out that browser or studio-mic results don't prove
telephone performance — so this setup is what makes the demo real.

## 1. Twilio side
1. Buy a phone number in the Twilio console (any number that supports voice).
2. Create a SIP Domain under Voice → SIP Domains, pointing to your LiveKit
   SIP endpoint.
3. Configure the phone number's "A call comes in" webhook to dial the SIP
   domain, or use Twilio Elastic SIP Trunking if you want failover/multiple
   numbers.

## 2. LiveKit side
1. Enable SIP on your LiveKit Cloud project (or self-hosted LiveKit with the
   `sip` service running).
2. Create a SIP Trunk + Dispatch Rule. Set the dispatch rule so the LiveKit
   **room name equals the caller's phone number** (the `From` field) — this
   is what `main.py` relies on to key session recovery per-caller, not
   per-call.
3. Deploy the agent worker (`python -m agent.main start`) so it's listening
   for room dispatch.

## 3. Why this matters for the demo
- Record calls placed from an actual mobile phone on a real cellular
  network, not a softphone on the same WiFi as the server. Cellular audio
  is compressed and lossy in ways that stress STT language detection —
  which is the exact claim being tested.
- For the dropped-call stress case: place a call, get partway through the
  intake flow, then put the phone in airplane mode for 5-10 seconds and
  turn it back on, or just hang up and call back within 3 minutes. Show the
  agent resuming instead of re-greeting.
- Log `speaker.active_provider` (see `agent/rime_client.py`) to a visible
  console during the recorded demo so judges can see Rime is the one
  speaking, not a cached fallback.

## Known limitation to disclose
No SIP trunk failover is configured. If the LiveKit SIP service restarts
mid-call, the call drops and must be recovered via the session-recovery
path on callback rather than an in-call reconnect. This is disclosed
intentionally rather than hidden — see RIME_EVIDENCE.md limitations.

