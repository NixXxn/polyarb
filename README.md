# polymarket-arb

Streamlined Polymarket **two-leg arbitrage** bot (paper + live).

Buys YES+NO (or Up+Down) when combined cost stays under $1, then runs hybrid active exits (ladder TP, lose-leg salvage, momentum rebalance). Key parameters are editable from the dashboard and written to `config/settings.yaml`.

## Install

```bash
cd polymarket-arb
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dashboard,live,dev]'
cp .env.example .env   # add ARBOT_PRIVATE_KEY for live
```

## Paper

```bash
arbot run --once
arbot run                 # continuous
arbot status
arbot dashboard           # http://127.0.0.1:8788
```

## Live

```bash
# .env
ARBOT_LIVE=1
ARBOT_PRIVATE_KEY=0x...
ARBOT_FUNDER=0x...          # Magic/proxy wallet if used
ARBOT_SIGNATURE_TYPE=1

arbot run --mode live --confirm-live
# or: arbot run --mode live --confirm-live --once
```

Live posts real CLOB orders via `py-clob-client-v2`. Resting orders are synced each scan.

## Dashboard settings

Open the dashboard → edit sizing, pair-cost caps, exit ladder, lose-leg thresholds, rebalance knobs, and live CLOB host/funder → **Save settings**. Changes go to `config/settings.yaml` and apply on the next scan/restart.
