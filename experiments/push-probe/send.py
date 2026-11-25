#!/usr/bin/env python3
"""Send a silent push to the push probe app every few minutes and log each send.

Needs only Python 3, curl and openssl. Configure with environment variables:
APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID, APNS_TOPIC (the app's bundle id) and
APNS_DEVICE_TOKEN (shown in the app).
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

HOSTS = {"sandbox": "api.sandbox.push.apple.com", "production": "api.push.apple.com"}
TOKEN_MAX_AGE = 50 * 60
# A config problem won't fix itself, so stop instead of logging the same failure for days.
FATAL = {400, 403, 405, 410, 413}


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def der_to_raw(der):
    """ECDSA signature from DER SEQUENCE{r, s} to the 64-byte r||s that JWT wants."""
    i = 3 if der[1] == 0x81 else 2
    out = b""
    for _ in range(2):
        if der[i] != 0x02:
            raise ValueError("not a DER ECDSA signature")
        n = der[i + 1]
        out += der[i + 2 : i + 2 + n].lstrip(b"\0").rjust(32, b"\0")
        i += 2 + n
    return out


def provider_token(key_path, key_id, team_id, now=None):
    header = b64(json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode())
    claims = b64(json.dumps({"iss": team_id, "iat": int(now or time.time())}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    der = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_path],
        input=signing_input, capture_output=True, check=True,
    ).stdout
    return f"{header}.{claims}.{b64(der_to_raw(der))}"


def send(env, device, topic, token, push_id, sent_at, expiration):
    payload = {"aps": {"content-available": 1}, "probe": {"id": push_id, "sent_at": sent_at}}
    headers = [
        f"authorization: bearer {token}",
        f"apns-topic: {topic}",
        "apns-push-type: background",
        "apns-priority: 5",
        f"apns-expiration: {int(sent_at) + expiration if expiration else 0}",
        f"apns-id: {push_id}",
    ]
    # Headers go through a file so the token isn't on the command line.
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("\n".join(headers) + "\n")
        header_file = f.name
    try:
        result = subprocess.run(
            ["curl", "-sS", "--http2", "--max-time", "30", "-H", f"@{header_file}",
             "--data-binary", "@-", "-w", "\n%{http_code}",
             f"https://{HOSTS[env]}/3/device/{device}"],
            input=json.dumps(payload).encode(), capture_output=True,
        )
    finally:
        os.unlink(header_file)
    if result.returncode != 0:
        return None, result.stderr.decode().strip()
    body, _, code = result.stdout.decode().rpartition("\n")
    reason = None
    if body.strip():
        try:
            reason = json.loads(body).get("reason")
        except ValueError:
            reason = body.strip()
    return int(code), reason


def config():
    names = ["APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_TOPIC", "APNS_DEVICE_TOKEN"]
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit(f"set {', '.join(missing)}")
    return [os.environ[n] for n in names]


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--env", choices=HOSTS, default="sandbox",
                   help="sandbox for builds run from Xcode, production for TestFlight")
    p.add_argument("--interval", type=float, default=20, help="minutes between pushes (default 20)")
    p.add_argument("--count", type=int, help="stop after this many pushes")
    p.add_argument("--expiration", type=int, default=3600,
                   help="seconds APNs keeps a push for an offline phone; 0 means try once (default 3600)")
    p.add_argument("--log", default="push-sends.jsonl", help="send log (default push-sends.jsonl)")
    args = p.parse_args()
    key_path, key_id, team_id, topic, device = config()

    token, token_at, sent = None, 0, 0
    while args.count is None or sent < args.count:
        tick = time.time()
        if tick - token_at > TOKEN_MAX_AGE:
            token, token_at = provider_token(key_path, key_id, team_id, tick), tick
        push_id = str(uuid.uuid4())
        status, reason = send(args.env, device, topic, token, push_id, tick, args.expiration)
        if status == 403 and reason == "ExpiredProviderToken":
            token, token_at = provider_token(key_path, key_id, team_id), time.time()
            status, reason = send(args.env, device, topic, token, push_id, tick, args.expiration)
        record = {"id": push_id, "sent_at": tick, "env": args.env, "status": status, "reason": reason}
        with open(args.log, "a") as f:
            f.write(json.dumps(record) + "\n")
        sent += 1
        print(time.strftime("%Y-%m-%d %H:%M:%S"), push_id[:8], status, reason or "", flush=True)
        if status in FATAL:
            sys.exit(f"APNs refused the push ({status} {reason}); fix the config and start again")
        if args.count is None or sent < args.count:
            time.sleep(max(0, tick + args.interval * 60 - time.time()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
