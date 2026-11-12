#!/usr/bin/env bash
# End-to-end demo against a running Compose stack:
#   http://localhost:8000  (installation API, /docs)
#   http://localhost:8001  (device simulator)
#
# Scenarios:
#   1. signed v2 installation -> SUCCESS
#   2. same version + same digest -> idempotent original result
#   3. same version + different digest -> REJECTED
#   4. receipt lost after activation -> VERIFYING -> reconciled SUCCESS
#   5. (manual) restart the API container -> open task preserved and recovered
set -euo pipefail

API="${API_URL:-http://localhost:8000}"
DEVICE="${DEVICE_URL:-http://localhost:8001}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
wait_terminal() {
  local id="$1"
  for _ in $(seq 1 30); do
    local state
    state="$(curl -fsS "$API/installations/$id" | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])')"
    echo "  task $id state: $state"
    case "$state" in
      SUCCESS|REJECTED) return 0 ;;
    esac
    sleep 1
  done
  echo "task $id did not reach a terminal state" >&2
  return 1
}
sign() { # version outdir [extra-size]
  local version="$1" outdir="$2" size="${3:-4096}"
  python3 -m signing.sample_sign --keys-dir keys --out "$outdir" \
    --model gateway-gx100 --version "$version" --size "$size" >/dev/null
}
submit() { # outdir -> prints task id
  local outdir="$1"
  curl -fsS -X POST "$API/installations" \
    -F "firmware=@$outdir/firmware.bin" \
    -F "manifest=@$outdir/manifest.json;type=application/json" \
    -F "signature=$(cat "$outdir/signature.b64")" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])'
}

log "device state before demo"
curl -fsS "$DEVICE/status" | python3 -m json.tool

# --- scenario 1: valid signed installation --------------------------------
log "1) signed v2 installation"
sign 2 "$WORK/v2"
T2="$(submit "$WORK/v2")"
wait_terminal "$T2"
curl -fsS "$API/installations/$T2" | python3 -m json.tool

# --- scenario 2: same version + same digest (idempotent original result) ---
log "2) resubmit identical v2 package (expect SUCCESS / already_installed)"
T2B="$(submit "$WORK/v2")"
wait_terminal "$T2B"
curl -fsS "$API/installations/$T2B" | python3 -m json.tool

# --- scenario 3: same version, different digest ----------------------------
log "3) same version v2 with different firmware (expect REJECTED)"
sign 2 "$WORK/v2-conflict" 8192
T2C="$(submit "$WORK/v2-conflict")"
wait_terminal "$T2C"
curl -fsS "$API/installations/$T2C" | python3 -m json.tool

# --- scenario 4: receipt lost after activation -----------------------------
log "4) arm device to drop the next receipt, then install v3"
curl -fsS -X POST "$DEVICE/admin/lose-next-receipt" \
  -H 'Content-Type: application/json' -d '{"enabled": true}' | python3 -m json.tool
sign 3 "$WORK/v3"
T3="$(submit "$WORK/v3")"
echo "  task $T3 (receipt will be lost)"
for _ in $(seq 1 12); do
  STATE="$(curl -fsS "$API/installations/$T3" | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])')"
  echo "  task $T3 state: $STATE"
  [ "$STATE" = "VERIFYING" ] && break
  sleep 1
done
log "device actually activated v3 despite lost receipt"
curl -fsS "$DEVICE/status" | python3 -m json.tool
log "trigger immediate reconciliation (background loop would do this anyway)"
curl -fsS -X POST "$API/installations/reconcile" | python3 -m json.tool
wait_terminal "$T3"

# --- slot lock demonstration -----------------------------------------------
log "5) while an open task exists new uploads are refused with 409"
curl -fsS -X POST "$DEVICE/admin/lose-next-receipt" \
  -H 'Content-Type: application/json' -d '{"enabled": true}' >/dev/null
sign 4 "$WORK/v4"
T4="$(submit "$WORK/v4")"
# Wait until the slot is genuinely open (VERIFYING) before racing a new upload.
for _ in $(seq 1 12); do
  STATE="$(curl -fsS "$API/installations/$T4" | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])')"
  [ "$STATE" = "VERIFYING" ] && break
  sleep 1
done
echo "  task $T4 holding slot in state: $STATE"
sign 5 "$WORK/v5"
set +e
CONFLICT="$(curl -sS -o "$WORK/conflict.json" -w '%{http_code}' -X POST "$API/installations" \
  -F "firmware=@$WORK/v5/firmware.bin" \
  -F "manifest=@$WORK/v5/manifest.json;type=application/json" \
  -F "signature=$(cat "$WORK/v5/signature.b64")")"
set -e
echo "  HTTP $CONFLICT (expected 409)"
python3 -m json.tool "$WORK/conflict.json"
log "status queries remain available during the lock"
curl -fsS "$API/installations/$T4" | python3 -m json.tool
log "reconcile recovers the v4 task"
curl -fsS -X POST "$API/installations/reconcile" >/dev/null
wait_terminal "$T4"

log "final device state"
curl -fsS "$DEVICE/status" | python3 -m json.tool
log "all tasks"
curl -fsS "$API/installations" | python3 -m json.tool
echo
echo "Demo finished. Interactive docs: $API/docs"
echo "Recovery test: docker compose restart api  -> open tasks are reconciled on startup"
