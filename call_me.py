"""
call_me.py

Triggers an outbound test call from Twilio to your verified mobile number.
Because incoming international calls are FREE in India, you don't need any ISD
balance on your mobile SIM.

When you answer the incoming call, Twilio bridges you directly to your
LiveKit Sangam agent over SIP!

NOTE: Twilio trial accounts do NOT support the inline `twiml=` parameter on
Calls.create(). They require a `url=` pointing to hosted TwiML. We use
Twilio's free twimlets.com/echo service to serve the TwiML without needing
our own server.
"""
import os
import sys
from urllib.parse import urlencode
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

sid = os.environ.get("TWILIO_ACCOUNT_SID")
token = os.environ.get("TWILIO_AUTH_TOKEN")

if not sid or not token:
    print("Error: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not found in .env")
    sys.exit(1)

client = Client(sid, token)

# Target verified phone number (your mobile phone)
USER_PHONE = os.environ.get("TEST_PHONE_NUMBER", "+919109208025")
# Purchased Twilio number (US number)
TWILIO_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "+17759972207")
# LiveKit SIP URI domain
SIP_DOMAIN = os.environ.get("LIVEKIT_SIP_DOMAIN", "rime-39ohkvce.sip.livekit.cloud")

twiml_xml = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Response><Dial>'
    f'<Sip>sip:{TWILIO_NUMBER.lstrip("+")}@{SIP_DOMAIN};transport=tcp</Sip>'
    '</Dial></Response>'
)

# Host TwiML via Twilio's free twimlets echo service (no server needed)
webhook_url = "http://twimlets.com/echo?" + urlencode({"Twiml": twiml_xml})

print(f"Placing outbound test call...")
print(f"  To:   {USER_PHONE} (Incoming is 100% free for you)")
print(f"  From: {TWILIO_NUMBER} (Twilio)")
print(f"  SIP:  {SIP_DOMAIN}")

try:
    call = client.calls.create(
        to=USER_PHONE,
        from_=TWILIO_NUMBER,
        url=webhook_url,
        method="GET",
    )
    print(f"\nSuccess! Call SID: {call.sid}")
    print("Your phone should ring in a few seconds. Answer it to speak with Sangam AI!")
except Exception as e:
    print(f"\nFailed to initiate call: {e}")
