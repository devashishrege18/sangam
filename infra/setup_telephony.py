"""
Automated telephony wiring: Twilio -> LiveKit SIP -> Sangam agent.

Usage:
    python -m infra.setup_telephony

Reads credentials from .env and performs:
  1. Creates a LiveKit SIP Inbound Trunk
  2. Creates a LiveKit SIP Dispatch Rule (room = caller number)
  3. Creates a Twilio TwiML Bin that dials the LiveKit SIP URI
  4. Configures the Twilio phone number's voice webhook

Idempotent -- safe to re-run (skips resources that already exist).
"""

import os
import sys
import asyncio

from dotenv import load_dotenv

load_dotenv()

# -- Required env vars ------------------------------------------------------
LIVEKIT_URL = os.environ["LIVEKIT_URL"]           # wss://xxx.livekit.cloud
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]

# Derive the LiveKit HTTP API URL from the WSS URL
LIVEKIT_HTTP_URL = LIVEKIT_URL.replace("wss://", "https://")


async def setup_livekit_sip():
    """Create SIP Inbound Trunk + Dispatch Rule via LiveKit Server SDK."""
    from livekit import api

    print("\n=== LiveKit SIP Setup ===")

    lk = api.LiveKitAPI(
        LIVEKIT_HTTP_URL,
        LIVEKIT_API_KEY,
        LIVEKIT_API_SECRET,
    )

    trunk_id = None

    # -- 1. List existing trunks to check for duplicates -----------------
    try:
        existing_trunks = await lk.sip.list_sip_inbound_trunk(
            api.ListSIPInboundTrunkRequest()
        )
        for t in existing_trunks.items:
            if t.name == "sangam-twilio-trunk":
                print(f"  [OK] SIP Inbound Trunk already exists: {t.sip_trunk_id}")
                trunk_id = t.sip_trunk_id
                break
    except Exception as e:
        print(f"  [WARN] Could not list trunks: {e}")

    # -- 2. Create inbound trunk if needed -------------------------------
    if trunk_id is None:
        trunk = await lk.sip.create_sip_inbound_trunk(
            api.CreateSIPInboundTrunkRequest(
                trunk=api.SIPInboundTrunkInfo(
                    name="sangam-twilio-trunk",
                    numbers=[TWILIO_PHONE_NUMBER],
                    allowed_numbers=[],       # allow all callers
                )
            )
        )
        trunk_id = trunk.sip_trunk_id
        print(f"  [OK] Created SIP Inbound Trunk: {trunk_id}")

    # -- 3. List existing dispatch rules ---------------------------------
    try:
        existing_rules = await lk.sip.list_sip_dispatch_rule(
            api.ListSIPDispatchRuleRequest()
        )
        for r in existing_rules.items:
            if r.name == "sangam-dispatch":
                print(f"  [OK] Dispatch Rule already exists: {r.sip_dispatch_rule_id}")
                await lk.aclose()
                return trunk_id
    except Exception:
        pass

    # -- 4. Create dispatch rule: room name = caller's phone number ------
    rule = await lk.sip.create_sip_dispatch_rule(
        api.CreateSIPDispatchRuleRequest(
            name="sangam-dispatch",
            trunk_ids=[trunk_id],
            rule=api.SIPDispatchRule(
                dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                    room_prefix="sangam-",
                    pin="",                   # no PIN required
                )
            ),
        )
    )
    print(f"  [OK] Created Dispatch Rule: {rule.sip_dispatch_rule_id}")
    print(f"       Room pattern: sangam-<caller_number>")

    await lk.aclose()
    return trunk_id


def setup_twilio_webhook():
    """Create TwiML Bin + configure phone number webhook via Twilio SDK."""
    from twilio.rest import Client

    print("\n=== Twilio Webhook Setup ===")

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    # Derive the LiveKit SIP domain from LIVEKIT_URL
    # wss://rime-39ohkvce.livekit.cloud -> rime-39ohkvce.sip.livekit.cloud
    lk_host = LIVEKIT_URL.replace("wss://", "").replace("ws://", "")
    sip_domain = lk_host.replace(".livekit.cloud", ".sip.livekit.cloud")

    twiml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Dial>"
        f"<Sip>{sip_domain};transport=tcp</Sip>"
        "</Dial>"
        "</Response>"
    )

    twiml_bin_url = None

    # -- 1. Try to create TwiML Bin via REST API -------------------------
    import json
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode
    from base64 import b64encode

    auth_str = f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}"
    auth_b64 = b64encode(auth_str.encode()).decode()

    # List existing TwiML Bins
    list_url = f"https://twiml.twilio.com/v1/Accounts/{TWILIO_ACCOUNT_SID}/Bins"
    req = Request(list_url, headers={"Authorization": f"Basic {auth_b64}"})
    try:
        resp = urlopen(req)
        data = json.loads(resp.read())
        for b in data.get("bins", []):
            if b.get("friendly_name") == "LiveKit Sangam":
                twiml_bin_url = b.get("url")
                print(f"  [OK] TwiML Bin already exists: {b.get('sid')}")
                break
    except Exception:
        pass

    # -- 2. Create TwiML Bin if needed -----------------------------------
    if twiml_bin_url is None:
        try:
            create_api_url = f"https://twiml.twilio.com/v1/Accounts/{TWILIO_ACCOUNT_SID}/Bins"
            post_data = urlencode({
                "FriendlyName": "LiveKit Sangam",
                "Twiml": twiml_content,
            }).encode()
            req = Request(
                create_api_url,
                data=post_data,
                headers={
                    "Authorization": f"Basic {auth_b64}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            resp = urlopen(req)
            result = json.loads(resp.read())
            twiml_bin_url = result.get("url")
            print(f"  [OK] Created TwiML Bin: {result.get('sid')}")
            print(f"       SIP target: {sip_domain}")
        except Exception as e:
            print(f"  [WARN] Could not create TwiML Bin via API: {e}")
            twiml_bin_url = None

    # -- 3. Configure the phone number's voice webhook -------------------
    numbers = client.incoming_phone_numbers.list(phone_number=TWILIO_PHONE_NUMBER)
    if not numbers:
        print(f"  [FAIL] Phone number {TWILIO_PHONE_NUMBER} not found in your account!")
        print(f"         Available numbers:")
        for n in client.incoming_phone_numbers.list():
            print(f"           {n.phone_number} ({n.sid})")
        return False

    phone_sid = numbers[0].sid
    print(f"  [OK] Found phone number: {TWILIO_PHONE_NUMBER} ({phone_sid})")

    if twiml_bin_url:
        # Point voice webhook to the TwiML Bin URL
        client.incoming_phone_numbers(phone_sid).update(
            voice_url=twiml_bin_url,
            voice_method="POST",
        )
        print(f"  [OK] Voice webhook set to TwiML Bin: {twiml_bin_url}")
    else:
        # Fallback: manual instructions
        print(f"  [WARN] TwiML Bin creation failed. Manual step needed:")
        print(f"    Go to: https://console.twilio.com/us1/develop/phone-numbers/manage/incoming/{phone_sid}/configure")
        print(f"    Set 'A call comes in' -> TwiML Bin -> paste this TwiML:")
        print(f"\n{twiml_content}\n")
        return False

    print("\n[DONE] Telephony setup complete!")
    print(f"   Call {TWILIO_PHONE_NUMBER} -> Twilio -> LiveKit SIP -> Sangam agent")
    return True


async def main():
    print("+--------------------------------------------------+")
    print("|   Sangam Telephony Setup (Twilio -> LiveKit)      |")
    print("+--------------------------------------------------+")

    try:
        await setup_livekit_sip()
    except ImportError:
        print("  [FAIL] livekit-api not installed. Run: pip install livekit-api")
        sys.exit(1)
    except Exception as e:
        print(f"  [WARN] LiveKit SIP setup error: {e}")
        print("    You may need to set up SIP manually in the LiveKit Cloud console.")

    try:
        success = setup_twilio_webhook()
    except ImportError:
        print("  [FAIL] twilio SDK not installed. Run: pip install twilio")
        sys.exit(1)
    except Exception as e:
        print(f"  [WARN] Twilio setup error: {e}")
        success = False

    if success:
        print("\n>> Next step: Start the agent worker:")
        print("   python -m agent.main dev")
        print(f"\n>> Then call {TWILIO_PHONE_NUMBER} from your phone!")
    else:
        print("\n[WARN] Some steps require manual action. See messages above.")


if __name__ == "__main__":
    asyncio.run(main())
