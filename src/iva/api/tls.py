"""Self-signed TLS identity for the LAN API.

`iva-api` serves HTTPS by default so RPC/chat traffic (the portal password,
bearer tokens, conversation turns) can't be sniffed by others on the Wi-Fi.
Trust is bootstrapped over the paired BLE channel instead of a CA: the app
fetches `net.info` (which includes this certificate's SHA-256 fingerprint)
over BLE and PINS that fingerprint for all LAN calls — a LAN
man-in-the-middle presenting any other cert is rejected by the pin.

Cert + key live in ~/.config/iva-voice/tls/ (EC P-256, 10 years), generated
on first use with the device's openssl. Delete the dir to rotate; the app
re-learns the new fingerprint over BLE on its next connect.
"""
import base64
import hashlib
import os
import subprocess

TLS_DIR = os.path.expanduser(os.environ.get("IVA_TLS_DIR", "~/.config/iva-voice/tls"))
CERT = os.path.join(TLS_DIR, "cert.pem")
KEY = os.path.join(TLS_DIR, "key.pem")


def ensure():
    """Generate the self-signed cert+key if missing; return (cert, key) paths."""
    if not (os.path.isfile(CERT) and os.path.isfile(KEY)):
        os.makedirs(TLS_DIR, exist_ok=True)
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec",
             "-pkeyopt", "ec_paramgen_curve:prime256v1",
             "-keyout", KEY, "-out", CERT, "-days", "3650", "-nodes",
             "-subj", "/CN=iva.local",
             "-addext", "subjectAltName=DNS:iva.local"],
            check=True, capture_output=True)
        os.chmod(KEY, 0o600)
    return CERT, KEY


def fingerprint():
    """SHA-256 hex of the certificate in DER form (what the app pins),
    generating the cert first if needed."""
    cert, _ = ensure()
    with open(cert) as f:
        pem = f.read()
    body = pem.split("-----BEGIN CERTIFICATE-----")[1].split("-----END CERTIFICATE-----")[0]
    der = base64.b64decode("".join(body.split()))
    return hashlib.sha256(der).hexdigest()
