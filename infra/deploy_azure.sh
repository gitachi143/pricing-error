#!/usr/bin/env bash
# One-shot deploy of Deal Desk to Azure App Service.
#
#   az login                       # do this yourself first (opens a browser)
#   ./infra/deploy_azure.sh
#
# Re-run it any time to push new code; it reuses the same resources and secrets.
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="${APP_NAME:-dealdesk}"
RG="${RG:-rg-dealdesk}"
LOCATION="${LOCATION:-eastus}"
SKU="${SKU:-B1}"

# Prefer a system `az`; fall back to the bundled venv wrapper (tools/az).
if command -v az >/dev/null; then AZ=az
elif [ -x ./tools/az ]; then AZ=./tools/az
else echo "Azure CLI not found. Install it:
  python3 -m pip install azure-cli      (or: brew install azure-cli)"; exit 1; fi

$AZ account show >/dev/null 2>&1 || { echo "Not signed in. Run:  $AZ login"; exit 1; }
echo "▸ subscription: $($AZ account show --query name -o tsv)"

# Student/sponsored subscriptions restrict which regions you may deploy into.
ALLOWED=$($AZ policy assignment list \
  --query "[?parameters.listOfAllowedLocations].parameters.listOfAllowedLocations.value[]" \
  -o tsv 2>/dev/null | tr '\n' ' ' || true)
if [ -n "$ALLOWED" ] && ! echo " $ALLOWED " | grep -q " $LOCATION "; then
  echo "✗ Your subscription's policy does not allow '$LOCATION'."
  echo "  Allowed: $ALLOWED"
  echo "  Re-run with, e.g.:  LOCATION=$(echo $ALLOWED | awk '{print $1}') $0"
  exit 1
fi

# ---- secrets ---------------------------------------------------------------
# Reuse .env if it exists so the password you already have keeps working.
# Read it literally — `source` would try to expand the $ signs in a scrypt hash.
if [ -f .env ]; then
  while IFS='=' read -r _k _v || [ -n "$_k" ]; do
    case "$_k" in ''|'#'*) continue ;; esac
    _v="${_v%\"}"; _v="${_v#\"}"
    export "$_k=$_v"
  done < .env
fi
if [ -z "${ADMIN_PASSWORD_HASH:-}" ]; then
  echo "▸ generating a fresh admin password"
  OUT=$(python3 tools/mkpass.py)
  echo "$OUT"
  ADMIN_PASSWORD_HASH=$(echo "$OUT" | grep ADMIN_PASSWORD_HASH | cut -d= -f2-)
fi
SESSION_SECRET="${SESSION_SECRET:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')}"
INGEST_TOKEN="${INGEST_TOKEN:-ing_$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')}"
WORKER_TOKEN="${WORKER_TOKEN:-wrk_$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')}"

# ---- infrastructure --------------------------------------------------------
echo "▸ resource group $RG in $LOCATION"
$AZ group create -n "$RG" -l "$LOCATION" -o none

echo "▸ deploying infrastructure (App Service + Key Vault + managed identity)"
DEPLOY=$($AZ deployment group create \
  -g "$RG" -n dealdesk-infra \
  --template-file infra/main.bicep \
  --parameters appName="$APP_NAME" location="$LOCATION" sku="$SKU" \
               adminPasswordHash="$ADMIN_PASSWORD_HASH" \
               sessionSecret="$SESSION_SECRET" \
               ingestToken="$INGEST_TOKEN" \
               workerToken="$WORKER_TOKEN" \
  --query properties.outputs -o json)

SITE_NAME=$(echo "$DEPLOY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["siteName"]["value"])')
SITE_URL=$(echo  "$DEPLOY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["siteUrl"]["value"])')
KV=$(echo        "$DEPLOY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["keyVault"]["value"])')

# ---- code ------------------------------------------------------------------
echo "▸ packaging"
ZIP=$(mktemp -d)/app.zip
zip -qr "$ZIP" app worker prompts tools requirements.txt startup.sh \
  -x '*__pycache__*' '*.pyc' '*.db' 'app/static/.DS_Store'

echo "▸ deploying code to $SITE_NAME"
# --async: Oryx installs dependencies server-side and the synchronous Kudu call
# routinely 504s on a B1 plan even though the build is fine. We poll instead.
$AZ webapp deploy -g "$RG" -n "$SITE_NAME" --src-path "$ZIP" --type zip \
  --async true --track-status false -o none

echo "▸ waiting for the build and first boot (up to 10 min)"
HEALTHY=0
for i in $(seq 1 60); do
  if curl -fsS --max-time 10 "$SITE_URL/api/health" >/dev/null 2>&1; then HEALTHY=1; break; fi
  sleep 10
done
if [ "$HEALTHY" != "1" ]; then
  echo "✗ Still not answering /api/health. Check the build log:"
  echo "    $AZ webapp log deployment show -n $SITE_NAME -g $RG"
  echo "    $AZ webapp log tail -n $SITE_NAME -g $RG"
  exit 1
fi

# ---- Key Vault references --------------------------------------------------
# On a first deploy the app boots before the managed identity's RBAC grant has
# propagated, so every @Microsoft.KeyVault(...) setting resolves to its own
# literal text and stays cached that way. A plain restart does NOT re-resolve —
# it takes a configuration change. So: touch a setting, wait for ARM to report
# every reference Resolved, then restart to load them into the container.
echo "▸ resolving Key Vault references"
SUB=$($AZ account show --query id -o tsv)
REFS_URI="https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Web/sites/$SITE_NAME/config/configreferences/appsettings?api-version=2022-03-01"
$AZ webapp config appsettings set -n "$SITE_NAME" -g "$RG" \
  --settings "KV_REFRESH=$(date +%s)" -o none

for i in $(seq 1 30); do
  UNRESOLVED=$($AZ rest --method GET --uri "$REFS_URI" -o json 2>/dev/null \
    | python3 -c "import json,sys
try: v=json.load(sys.stdin).get('value',[])
except Exception: print(99); raise SystemExit
print(sum(1 for r in v if r['properties'].get('status')!='Resolved'))" 2>/dev/null || echo 99)
  [ "$UNRESOLVED" = "0" ] && { echo "  all references resolved"; break; }
  sleep 10
done

$AZ webapp restart -n "$SITE_NAME" -g "$RG" -o none
echo "▸ waiting for the app to come back with its secrets"
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
    -X POST "$SITE_URL/api/login" -H 'content-type: application/json' \
    -d '{"password":"__probe__"}' 2>/dev/null)
  # 401 means the app is up AND parsed a real hash; 000/503 means still booting
  [ "$code" = "401" ] && { echo "  up"; break; }
  sleep 8
done

cat <<EOF

────────────────────────────────────────────────────────────
  Deal Desk is live

  URL           $SITE_URL
  Sign in with  the password printed above (or the one in .env)

  Key Vault     $KV   (secrets: admin-password-hash, session-secret,
                       ingest-token, worker-token)
  Logs          $AZ webapp log tail -g $RG -n $SITE_NAME

  Point the checkout worker at it:
    export DEALDESK_URL=$SITE_URL
    export WORKER_TOKEN=$WORKER_TOKEN
    python3 worker/run_worker.py

  Discord relay posts to:
    $SITE_URL/api/ingest/message   (Bearer $INGEST_TOKEN)
────────────────────────────────────────────────────────────
EOF

# keep local .env in sync so re-runs are idempotent
python3 - "$ADMIN_PASSWORD_HASH" "$SESSION_SECRET" "$INGEST_TOKEN" "$WORKER_TOKEN" "$SITE_URL" <<'PY'
import sys, pathlib
h, s, i, w, url = sys.argv[1:6]
p = pathlib.Path(".env")
lines = p.read_text().splitlines() if p.exists() else []
vals = {"ADMIN_PASSWORD_HASH": h, "SESSION_SECRET": s, "INGEST_TOKEN": i,
        "WORKER_TOKEN": w, "DEALDESK_URL": url}
out, seen = [], set()
for ln in lines:
    k = ln.split("=", 1)[0].strip()
    if k in vals:
        out.append(f"{k}={vals[k]}"); seen.add(k)
    else:
        out.append(ln)
out += [f"{k}={v}" for k, v in vals.items() if k not in seen]
p.write_text("\n".join(out) + "\n")
print("▸ .env updated")
PY
