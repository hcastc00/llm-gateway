#!/usr/bin/env bash
# Generate a self-signed TLS certificate for local/LAN use.
# For production, replace with a Let's Encrypt cert (certbot / acme.sh).

set -euo pipefail

CERTS_DIR="$(cd "$(dirname "$0")/.." && pwd)/certs"
mkdir -p "$CERTS_DIR"

# Detect the machine's LAN IP for the SAN
LAN_IP="${1:-192.168.1.46}"

openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout "$CERTS_DIR/key.pem" \
  -out    "$CERTS_DIR/cert.pem" \
  -days   825 \
  -subj   "/CN=llm-gateway" \
  -addext "subjectAltName=IP:${LAN_IP},IP:127.0.0.1,DNS:localhost"

echo ""
echo "Certificates written to $CERTS_DIR"
echo "  cert.pem  – share this with clients so they trust the gateway"
echo "  key.pem   – keep private, never commit"
echo ""
echo "To trust on macOS:"
echo "  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain $CERTS_DIR/cert.pem"
echo ""
echo "For production, use Let's Encrypt:"
echo "  certbot certonly --standalone -d your.domain.com"
echo "  Then copy fullchain.pem → certs/cert.pem and privkey.pem → certs/key.pem"
