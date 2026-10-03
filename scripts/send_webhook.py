"""Send a signed webhook to a running API (simulates the payment provider).

  python -m scripts.send_webhook pay_abc123 SUCCESS --event-id evt_1
  python -m scripts.send_webhook pay_abc123 SUCCESS --event-id evt_1   # duplicate -> "duplicate"
"""
import argparse
import json
import urllib.error
import urllib.request
import uuid

from app.services.webhooks import sign

ap = argparse.ArgumentParser()
ap.add_argument("payment_ref")
ap.add_argument("status", choices=["SUCCESS", "FAILED"])
ap.add_argument("--event-id", default=f"evt_{uuid.uuid4().hex[:12]}")
ap.add_argument("--url", default="http://localhost:8000/payments/webhook/")
args = ap.parse_args()

body = json.dumps({"event_id": args.event_id, "payment_ref": args.payment_ref,
                   "status": args.status}).encode()
req = urllib.request.Request(args.url, data=body, method="POST", headers={
    "Content-Type": "application/json", "X-Webhook-Signature": sign(body)})
try:
    with urllib.request.urlopen(req) as r:
        print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
