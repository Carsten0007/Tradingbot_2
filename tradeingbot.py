# tradingbot.py – Capital.com Tick → Candle → Demo-Signal

import os
import json
import requests
import asyncio
import websockets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ==============================
# SETTINGS (entspricht Zelle A)
# ==============================

# Zugangsdaten aus Umgebungsvariablen oder direkt hier eintragen
API_KEY  = os.getenv("CAPITAL_API_KEY") or "50vfL7RdFiukl2UE"
USERNAME = os.getenv("CAPITAL_USERNAME") or "carsten.schoettke@gmx.de"
PWD      = os.getenv("CAPITAL_PASSWORD") or "G8ZdGJHN7VB9vJy&"


# Basis-URLs LIVE
#BASE_REST   = "https://api-capital.backend-capital.com"
#BASE_STREAM = "wss://api-streaming-capital.backend-capital.com/connect"
#ACCOUNT  = os.getenv("CAPITAL_ACCOUNT_TYPE", "live")  # "demo" oder "live"

# Basis-URLs DEMO
BASE_REST   = "https://demo-api-capital.backend-capital.com"
BASE_STREAM = "wss://api-streaming-capital.backend-capital.com/connect"
ACCOUNT  = os.getenv("CAPITAL_ACCOUNT_TYPE", "demo")

# Instrumente
#INSTRUMENTS = ["BTCUSD", "ETHUSD"]
INSTRUMENTS = ["BTCUSD"]

# Lokalzeit
LOCAL_TZ = ZoneInfo("Europe/Berlin")

def to_local_dt(ms_since_epoch: int) -> datetime:
    return datetime.fromtimestamp(ms_since_epoch/1000, tz=timezone.utc).astimezone(LOCAL_TZ)


# ==============================
# LOGIN (entspricht Zelle B)
# ==============================

def capital_login():
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "identifier": USERNAME,
        "password": PWD,
        "encryptedPassword": False
    }
    r = requests.post(f"{BASE_REST}/api/v1/session", headers=headers, json=payload)
    print("Login HTTP:", r.status_code)
    CST  = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")
    print("CST vorhanden?", bool(CST), "XSEC vorhanden?", bool(XSEC))
    return CST, XSEC


# ==============================
# SIGNAL-LOGIK (Zelle D)
# ==============================

def on_candle_forming(epic, bar):
    """Wird bei jedem Tick innerhalb einer Kerze aufgerufen (noch nicht geschlossen)."""
    if bar["ticks"] % 50 == 0:  # alle 50 Ticks ein Check
        if bar["close"] > bar["open"]:
            signal = "BUY ✅"
        elif bar["close"] < bar["open"]:
            signal = "SELL ⛔"
        else:
            signal = "NEUTRAL ⚪"

        print(
            f"🔄 Forming-Signal [{epic}] — "
            f"O:{bar['open']:.2f} C:{bar['close']:.2f} "
            f"(Ticks:{bar['ticks']}) → {signal}"
        )

def on_candle_close(epic, bar):
    if bar["close"] > bar["open"]:
        signal = "BUY ✅"
    elif bar["close"] < bar["open"]:
        signal = "SELL ⛔"
    else:
        signal = "NEUTRAL ⚪"

    print(
        f"📊 Signal [{epic}] — O:{bar['open']:.2f} C:{bar['close']:.2f} → {signal}"
    )


# ==============================
# CANDLE-AGGREGATOR (Zelle C)
# ==============================

def local_minute_floor(ts_ms: int) -> datetime:
    dt_local = to_local_dt(ts_ms)
    return dt_local.replace(second=0, microsecond=0)

async def run_candle_aggregator_per_instrument(CST, XSEC):
    ws_url = f"{BASE_STREAM}?CST={CST}&X-SECURITY-TOKEN={XSEC}"
    subscribe = {
        "destination": "marketData.subscribe",
        "correlationId": "candles",
        "cst": CST,
        "securityToken": XSEC,
        "payload": {"epics": INSTRUMENTS},
    }

    states = {epic: {"minute": None, "bar": None} for epic in INSTRUMENTS}

    print("Verbinde:", ws_url)
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps(subscribe))
        print("Subscribed:", INSTRUMENTS)

        while True:
            raw = await ws.recv()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("destination") != "quote":
                continue

            p = msg["payload"]
            epic = p.get("epic")
            if epic not in states:
                continue

            try:
                bid = float(p["bid"])
                ask = float(p["ofr"])
                ts_ms = int(p["timestamp"])
            except Exception:
                continue

            px = (bid + ask) / 2.0
            minute_key = local_minute_floor(ts_ms)
            st = states[epic]

            if st["minute"] is not None and minute_key > st["minute"] and st["bar"] is not None:
                bar = st["bar"]
                print(
                    f"\n✅ [{epic}] Closed 1m  {st['minute'].strftime('%d.%m.%Y %H:%M:%S %Z')}  "
                    f"O:{bar['open']:.2f} H:{bar['high']:.2f} L:{bar['low']:.2f} C:{bar['close']:.2f}  "
                    f"Ticks:{bar['ticks']}"
                )
                on_candle_close(epic, bar)
                st["minute"] = minute_key
                st["bar"] = {"open": px, "high": px, "low": px, "close": px, "ticks": 1}
            else:
                if st["minute"] is None:
                    st["minute"] = minute_key
                    st["bar"] = {"open": px, "high": px, "low": px, "close": px, "ticks": 1}
                else:
                    b = st["bar"]
                    b["high"]  = max(b["high"], px)
                    b["low"]   = min(b["low"],  px)
                    b["close"] = px
                    b["ticks"] += 1

                on_candle_forming(epic, st["bar"])


# ==============================
# MAIN
# ==============================

if __name__ == "__main__":
    CST, XSEC = capital_login()
    asyncio.run(run_candle_aggregator_per_instrument(CST, XSEC))

