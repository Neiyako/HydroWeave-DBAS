#!/bin/sh
# Capture only CDS connection-layer traffic. TLS payload is intentionally not decoded.
set -eu
usage() { printf '%s\n' "Usage: $0 --output DIR [--host HOST] [--interface IFACE] [--port PORT] [--dry-run]"; }
out="data/captures"; host="api.ecmwf.int"; iface="any"; port="443"; dry=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out=$2; shift 2;; --host) host=$2; shift 2;; --interface) iface=$2; shift 2;; --port) port=$2; shift 2;; --dry-run) dry=1; shift;; -h|--help) usage; exit 0;; *) usage >&2; exit 2;; esac
done
mkdir -p "$out"
pcap="$out/capture.pcap"; manifest="$out/pcap_manifest.json"
filter="host $host and port $port"
if [ "$dry" -eq 1 ]; then
  printf '{"dry_run":true,"interface":"%s","host":"%s","port":%s,"filter":"%s","pcap":"%s","evidence":"connection-layer only"}\n' "$iface" "$host" "$port" "$filter" "$pcap" > "$manifest"
  printf '%s\n' "dry-run: tcpdump -i $iface -w $pcap '$filter'"; exit 0
fi
command -v tcpdump >/dev/null 2>&1 || { echo "tcpdump is required" >&2; exit 127; }
start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
trap 'status=$?; end=$(date -u +%Y-%m-%dT%H:%M:%SZ); printf "{\"dry_run\":false,\"interface\":\"%s\",\"host\":\"%s\",\"port\":%s,\"filter\":\"%s\",\"pcap\":\"%s\",\"started\":\"%s\",\"ended\":\"%s\",\"status\":%s,\"evidence\":\"connection-layer only; HTTPS payload not decoded\"}\n" "$iface" "$host" "$port" "$filter" "$pcap" "$start" "$end" "$status" > "$manifest"; exit $status' INT TERM EXIT
tcpdump -i "$iface" -s 128 -w "$pcap" "$filter"
