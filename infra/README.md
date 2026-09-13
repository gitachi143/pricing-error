# Deploying to Azure

```bash
az login                     # opens a browser — you have to do this bit
./infra/deploy_azure.sh      # ~4 minutes the first time
```

## What it creates

| Resource | Why |
|---|---|
| App Service plan (Linux, B1 ≈ $13/mo) | runs the dashboard + pipeline |
| Web App (Python 3.12) | the app itself, HTTPS-only, health-checked at `/api/health` |
| Key Vault | holds the admin password hash, session secret, and the two bearer tokens |
| System-assigned managed identity | lets the app read those secrets — no credentials in config |
| Application Insights | request traces and failures (set `enableAppInsights=false` to skip) |

The SQLite database lives at `/home/data/dealbot.db`. `/home` is Azure's persistent
share, so it survives restarts, scaling operations and re-deploys. Back it up with:

```bash
az webapp ssh -g rg-dealdesk -n <site> --command "cat /home/data/dealbot.db" > backup.db
```

## What is deliberately *not* on Azure

The **checkout worker**. It holds your card numbers and drives a real browser, so it
runs on your own machine (or a VM you control). The server never sees a card number —
only nickname, last 4, and how much credit you told it is left.

## Knobs

| Env var | Default | Meaning |
|---|---|---|
| `APP_NAME` | `dealdesk` | prefix for every resource |
| `RG` | `rg-dealdesk` | resource group |
| `LOCATION` | `eastus` | pick a region near the merchants — it's latency off your checkout |

**Region policy.** Azure for Students (and many sponsored/enterprise subscriptions) only permit a
handful of regions. The script checks the policy before it deploys and prints the allowed list if
your choice isn't on it. This subscription allows `centralus`, `westus`, `belgiumcentral`,
`francecentral`, `norwayeast` — `centralus` is the closest of those to the US East coast.
| `SKU` | `B1` | `S1`+ if you want deployment slots or autoscale |

## Scaling note

The app runs a single gunicorn worker on purpose: SQLite in WAL mode plus one process
is simple and fast enough for a Discord feed. If you ever need more than one instance,
move `DB_PATH` to Azure Database for PostgreSQL first — otherwise two instances will
fight over the same file.
