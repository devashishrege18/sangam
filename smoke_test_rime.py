"""
smoke_test_rime.py

Standalone sanity check — confirms your Rime API key works and that the
exact model/voice/language combo used in Sangam (agent/rime_client.py)
is valid, BEFORE wiring LiveKit, Twilio, or Deepgram. Zero dependencies
beyond `requests`.

Usage:
    export RIME_API_KEY=your_real_key
    python smoke_test_rime.py

Produces three files: hello_hi_greeting.wav, hello_hi.wav, hello_codeswitch.wav —
play them to confirm you hear real speech in the correct language(s).
"""

import os
import sys

import requests

API_KEY = os.environ.get("RIME_API_KEY")
if not API_KEY:
    print("ERROR: set RIME_API_KEY in your environment first.")
    print('  export RIME_API_KEY="your_real_key"')
    sys.exit(1)

URL = "https://users.rime.ai/v1/rime-tts"

CASES = [
    {
        "label": "Hindi-first greeting",
        "outfile": "hello_hi_greeting.wav",
        "payload": {
            "text": "नमस्ते जी। अपना room या flat number बताइए।",
            "modelId": "coda",
            "speaker": "taru",
            "lang": "hi",
            "samplingRate": 24000,
        },
    },
    {
        "label": "Hindi",
        "outfile": "hello_hi.wav",
        "payload": {
            "text": "नमस्ते, कॉल करने के लिए धन्यवाद। आपका कमरा नंबर क्या है?",
            "modelId": "coda",
            "speaker": "taru",
            "lang": "hi",
            "samplingRate": 24000,
        },
    },
    {
        "label": "Code-switched Hinglish (the actual product claim)",
        "outfile": "hello_codeswitch.wav",
        "payload": {
            "text": "मुझे पता चला कि bathroom में पानी की फुगा है — मैं इसे urgent flag कर रहा हूँ।",
            "modelId": "coda",
            "speaker": "taru",
            "lang": "hi",
            "samplingRate": 24000,
        },
    },
]

headers = {
    "Accept": "audio/wav",
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

all_ok = True
for case in CASES:
    print(f"\n[{case['label']}] requesting {case['outfile']} ...")
    try:
        resp = requests.post(URL, headers=headers, json=case["payload"], stream=True, timeout=30)
        resp.raise_for_status()
        with open(case["outfile"], "wb") as f:
            for chunk in resp.iter_content(chunk_size=4096):
                if chunk:
                    f.write(chunk)
        size = os.path.getsize(case["outfile"])
        print(f"  OK — wrote {size} bytes to {case['outfile']}")
        if size < 1000:
            print("  WARNING: file is suspiciously small — likely an error response, not audio.")
            all_ok = False
    except requests.HTTPError as e:
        print(f"  FAILED: {e}")
        print(f"  Response body: {e.response.text[:500]}")
        all_ok = False
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED: {e}")
        all_ok = False

print("\n" + ("=" * 50))
if all_ok:
    print("All requests succeeded. Play the .wav files to confirm audio quality.")
    print("Confirm the Hindi and short Hinglish samples sound natural and connected")
    print("before using this Hindi-first Coda configuration in a live call.")
else:
    print("At least one request failed — check the error output above.")
    print("Common causes: wrong key, key not activated yet, wrong speaker")
    print("name for the model (re-check against Rime's live voice catalog).")
sys.exit(0 if all_ok else 1)
