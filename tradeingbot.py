# tradingbot.py – Capital.com Tick → Candle → Demo-Signal

import os
import json
import requests
import asyncio
import websockets
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import deque
from colorama import Fore, Style, init
init(autoreset=True)

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
INSTRUMENTS = ["ETHUSD"]

# Lokalzeit
LOCAL_TZ = ZoneInfo("Europe/Berlin")

CST, XSEC = None, None


# ==============================
# STRATEGIE-EINSTELLUNGEN
# ==============================

EMA_FAST = 2   # kurze EMA-Periode (z. B. 9, 10, 20)
EMA_SLOW = 4  # lange EMA-Periode (z. B. 21, 30, 50)

def to_local_dt(ms_since_epoch: int) -> datetime:
    return datetime.fromtimestamp(ms_since_epoch/1000, tz=timezone.utc).astimezone(LOCAL_TZ)

# Candle-Historie für EMA-Berechnung
candle_history = {epic: deque(maxlen=200) for epic in INSTRUMENTS}


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
# POSITIONS-MANAGER mit Auto-ReLogin + Robustheit
# ==============================

def get_positions(CST, XSEC, retry=True):
    """Alle offenen Positionen abfragen."""
    url = f"{BASE_REST}/api/v1/positions"
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
        "Accept": "application/json"
    }
    r = requests.get(url, headers=headers)

    if r.status_code == 401 and retry:
        print("🔑 Session abgelaufen → erneuter Login (get_positions) ...")
        new_CST, new_XSEC = capital_login()
        return get_positions(new_CST, new_XSEC, retry=False)

    if r.status_code != 200:
        print("⚠️ Fehler beim Abrufen der Positionen:", r.status_code, r.text)
        return []

    try:
        data = r.json()
    except Exception:
        return []

    positions = data.get("positions", [])
    if not isinstance(positions, list):
        return []

    return positions


def open_position(CST, XSEC, epic, direction, size=1, retry=True):
    """Neue Position eröffnen (Market-Order)."""
    url = f"{BASE_REST}/api/v1/positions"
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
        "Content-Type": "application/json"
    }
    data = {
        "epic": epic,
        "direction": direction,   # "BUY" oder "SELL"
        "size": size,
        "orderType": "MARKET",
        "guaranteedStop": False
    }
    r = requests.post(url, headers=headers, json=data)

    if r.status_code == 401 and retry:
        print("🔑 Session abgelaufen → erneuter Login (open_position) ...")
        new_CST, new_XSEC = capital_login()
        return open_position(new_CST, new_XSEC, epic, direction, size, retry=False)

    print("📩 Order-Response:", r.status_code, r.text)
    return r


def close_position(CST, XSEC, deal_id, size=1, retry=True):
    """Offene Position schließen – probiert verschiedene API-Methoden."""
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Variante 1: DELETE /positions/otc/{dealId}
    url_delete = f"{BASE_REST}/api/v1/positions/otc/{deal_id}"
    print(f"🔎 Versuche Close mit DELETE {url_delete} ...")
    r = requests.delete(url_delete, headers=headers)

    if r.status_code == 401 and retry:
        print("🔑 Session abgelaufen → erneuter Login (close_position/DELETE) ...")
        new_CST, new_XSEC = capital_login()
        return close_position(new_CST, new_XSEC, deal_id, size, retry=False)

    if r.status_code == 200:
        print("📩 Close-Response (DELETE):", r.status_code, r.text)
        return r

    # Variante 2: POST /positions/close
    url_post = f"{BASE_REST}/api/v1/positions/close"
    data = {
        "dealId": deal_id,
        "size": size,
        "orderType": "MARKET"
    }
    print(f"🔎 Versuche Close mit POST {url_post} ...")
    r = requests.post(url_post, headers=headers, json=data)

    if r.status_code == 401 and retry:
        print("🔑 Session abgelaufen → erneuter Login (close_position/POST) ...")
        new_CST, new_XSEC = capital_login()
        return close_position(new_CST, new_XSEC, deal_id, size, retry=False)

    print("📩 Close-Response (POST /close):", r.status_code, r.text)
    return r



# ==============================
# SIGNAL-LOGIK (Zelle D)
# ==============================

def on_candle_forming(epic, bar, ts_ms):
    """Wird bei jedem Tick innerhalb einer Kerze aufgerufen (noch nicht geschlossen)."""
    closes = list(candle_history[epic]) + [bar["close"]]
    spread = (bar["high"] - bar["low"]) / max(1, bar["ticks"])
    trend = evaluate_trend_signal(epic, closes, spread)

    # Zeit konvertieren
    local_time = to_local_dt(ts_ms).strftime("%d.%m.%Y %H:%M:%S")

    # Nur bei neuem Close ausgeben
    if bar["ticks"] == 1 or bar["close"] != bar.get("last_printed", None):
        bar["last_printed"] = bar["close"]
        if bar["close"] > bar["open"]:
            instant = "BUY ✅"
        elif bar["close"] < bar["open"]:
            instant = "SELL ⛔"
        else:
            instant = "NEUTRAL ⚪"

        print(
            f"🔄 Forming-Signal [{epic}] {local_time} — "
            f"O:{bar['open']:.2f} C:{bar['close']:.2f} "
            f"(Ticks:{bar['ticks']}) → {instant} | Trend: {trend}"
        )

def on_candle_close(epic, bar):
    candle_history[epic].append(bar["close"])
    spread = (bar["high"] - bar["low"]) / max(1, bar["ticks"])
    signal = evaluate_trend_signal(epic, list(candle_history[epic]), spread)

    print(
        f"📊 Trend-Signal [{epic}] — O:{bar['open']:.2f} C:{bar['close']:.2f} → {signal}"
    )

    # Positions-Manager aufrufen
    decide_and_trade(CST, XSEC, epic, signal)


def ema(values, period: int):
    """Einfache EMA-Berechnung auf einer Liste von Werten."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def evaluate_trend_signal(epic, closes, spread):
    """Ermittle BUY/SELL/HOLD basierend auf EMA fast/slow und Spread-Filter."""
    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)

    if ema_fast is None or ema_slow is None:
        return f"HOLD (zu wenig Daten, {len(closes)}/{EMA_SLOW} Kerzen)"

    last_close = closes[-1]
    prev_close = closes[-2]

    if ema_fast > ema_slow and (last_close - prev_close) > 2 * spread:
        return "READY TO TRADE: BUY ✅"
    elif ema_fast < ema_slow and (prev_close - last_close) > 2 * spread:
        return "READY TO TRADE: SELL ⛔"
    else:
        return "UNSAFE ⚪"


# ==============================
# Hilfsfunktionen für robustes Open/Close
# ==============================

def safe_close(CST, XSEC, deal_id, size=1):
    """Wrapper: Close-Order robust mit Retry."""
    r = close_position(CST, XSEC, deal_id, size)
    return (r is not None and r.status_code == 200)

def safe_open(CST, XSEC, epic, direction, size=1):
    """Wrapper: Open-Order robust mit Retry."""
    r = open_position(CST, XSEC, epic, direction, size)
    return (r is not None and r.status_code == 200)


# ==============================
# DECISION-MANAGER (mit Schutz + Farben)
# ==============================

# Farben (ANSI-Codes)
RESET  = "\033[0m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"

open_positions = {epic: None for epic in INSTRUMENTS}  # Merker: None | "BUY" | "SELL"

def decide_and_trade(CST, XSEC, epic, signal):
    """Entscheidet basierend auf Signal + aktueller Position mit Schutz-Logik + Farben."""
    global open_positions

    current = open_positions[epic]

    def get_deal_info(epic):
        #Hole Details zur offenen Position für ein Epic.
        positions = get_positions(CST, XSEC)
        if not positions:
            print("⚠️ get_positions() lieferte keine offenen Positionen")
            return None

        for pos in positions:
            position = pos.get("position")
            if not position:
                print(f"⚠️ Unerwarteter Eintrag ohne 'position': {pos}")
                continue

            pos_epic = position.get("epic")
            if pos_epic == epic:
                return position

        print(f"ℹ️ Keine Position für {epic} gefunden.")
        return None

    if signal.startswith("READY TO TRADE: BUY"):
        if current == "BUY":
            print(Fore.GREEN + f"⚖️ [{epic}] Bereits LONG, nichts tun.")
        elif current == "SELL":
            deal = get_deal_info()
            print(f"🔎 get_deal_info() → {deal}")
            if deal:
                profit = float(deal.get("profitLoss", 0))
                deal_id = deal.get("dealId")  # 👀
                print(Fore.YELLOW + f"📊 [{epic}] Offener SHORT mit PnL={profit:.2f}")
                if profit >= 0:
                    print(Fore.GREEN + f"🔄 [{epic}] Short im Gewinn → schließen & Long eröffnen")
                    print(Fore.CYAN + f"🆔 Close-Versuch für Deal {deal_id}")  # 👀
                    if safe_close(CST, XSEC, deal["dealId"]):
                        if safe_open(CST, XSEC, epic, "BUY"):
                            open_positions[epic] = "BUY"
                    else:
                        print(Fore.RED + f"⚠️ [{epic}] Close fehlgeschlagen, retry beim nächsten Signal")
                else:
                    print(Fore.RED + f"⏸️ [{epic}] Short im Verlust → halte Position (kein Blind-Drehen)")
            else:
                print(f"{Fore.YELLOW}🚀 [{epic}] Long eröffnen (keine offene Position gefunden){Style.RESET_ALL}")
                if safe_open(CST, XSEC, epic, "BUY"):
                    open_positions[epic] = "BUY"
        else:
            print(f"{Fore.YELLOW}🚀 [{epic}] Long eröffnen{Style.RESET_ALL}")
            if safe_open(CST, XSEC, epic, "BUY"):
                open_positions[epic] = "BUY"

    elif signal.startswith("READY TO TRADE: SELL"):
        if current == "SELL":
            print(f"{Fore.RED}⚖️ [{epic}] Bereits SHORT, nichts tun. → {signal}{Style.RESET_ALL}")
        elif current == "BUY":
            deal = get_deal_info()
            print(f"🔎 get_deal_info() → {deal}")
            if deal:
                profit = float(deal.get("profitLoss", 0))
                deal_id = deal.get("dealId")  # 👀
                print(f"{Fore.GREEN}📊 [{epic}] Offener LONG mit PnL={profit:.2f}{Style.RESET_ALL}")
                if profit >= 0:
                    print(f"{Fore.YELLOW}🔄 [{epic}] Long im Gewinn → schließen & Short eröffnen{Style.RESET_ALL}")
                    print(Fore.CYAN + f"🆔 Close-Versuch für Deal {deal_id}")  # 👀
                    if safe_close(CST, XSEC, deal["dealId"]):
                        if safe_open(CST, XSEC, epic, "SELL"):
                            open_positions[epic] = "SELL"
                    else:
                        print(Fore.RED + f"⚠️ [{epic}] Close fehlgeschlagen, retry beim nächsten Signal")
                else:
                    print(f"{Fore.GREEN}⏸️ [{epic}] Long im Verlust → halte Position (kein Blind-Drehen){Style.RESET_ALL}")
            else:
                print(f"{Fore.YELLOW}🚀 [{epic}] Short eröffnen (keine offene Position gefunden){Style.RESET_ALL}")
                if safe_open(CST, XSEC, epic, "SELL"):
                    open_positions[epic] = "SELL"
        else:
            print(f"{Fore.YELLOW}🚀 [{epic}] Short eröffnen{Style.RESET_ALL}")
            if safe_open(CST, XSEC, epic, "SELL"):
                open_positions[epic] = "SELL"

    else:
        if current == "BUY":
            print(f"{Fore.GREEN}🤔 [{epic}] LONG offen → Signal = {signal}{Style.RESET_ALL}")
        elif current == "SELL":
            print(f"{Fore.RED}🤔 [{epic}] SHORT offen → Signal = {signal}{Style.RESET_ALL}")
        else:
            print(f"{Fore.YELLOW}🤔 [{epic}] Kein Trade offen → Signal = {signal}{Style.RESET_ALL}")


# ==============================
# CANDLE-AGGREGATOR (Zelle C)
# ==============================

def local_minute_floor(ts_ms: int) -> datetime:
    dt_local = to_local_dt(ts_ms)
    return dt_local.replace(second=0, microsecond=0)

async def run_candle_aggregator_per_instrument():
    global CST, XSEC

    while True:  # Endlosschleife mit Reconnect & Token-Refresh
        # Falls Tokens fehlen oder abgelaufen sind → neu einloggen
        if not CST or not XSEC:
            CST, XSEC = capital_login()

        ws_url = f"{BASE_STREAM}?CST={CST}&X-SECURITY-TOKEN={XSEC}"
        subscribe = {
            "destination": "marketData.subscribe",
            "correlationId": "candles",
            "cst": CST,
            "securityToken": XSEC,
            "payload": {"epics": INSTRUMENTS},
        }

        states = {epic: {"minute": None, "bar": None} for epic in INSTRUMENTS}

        print("🔌 Verbinde:", ws_url)
        try:
            async with websockets.connect(ws_url, ping_interval=None) as ws:
                await ws.send(json.dumps(subscribe))
                print("✅ Subscribed:", INSTRUMENTS)

                last_msg = time.time()

                while True:
                    # --- alle 5 Minuten ein Ping ---
                    if time.time() - last_msg > 300:
                        await ws.ping()
                        print("📡 Ping gesendet")
                        last_msg = time.time()

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        msg = json.loads(raw)
                        last_msg = time.time()
                    except asyncio.TimeoutError:
                        print("⚠️ Timeout → reconnect ...")
                        break
                    except Exception as e:
                        print("⚠️ Fehler beim Empfangen:", e)
                        # Session ungültig? → Tokens löschen → nächster Loop macht Login neu
                        if "invalid.session.token" in str(e).lower():
                            CST, XSEC = None, None
                        break

                    # nur Quotes weiterverarbeiten
                    if msg.get("destination") != "quote":
                        continue

                    # Payload robust auslesen
                    p = msg.get("payload", {})
                    epic = p.get("epic")
                    if not epic or epic not in states:
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

                        on_candle_forming(epic, st["bar"], ts_ms)

        except Exception as e:
            print("❌ Verbindungsfehler:", e)
            if "invalid.session.token" in str(e).lower():
                CST, XSEC = None, None

        print("⏳ 5s warten, dann neuer Versuch ...")
        await asyncio.sleep(5)

# ==============================
# MAIN
# ==============================

if __name__ == "__main__":
    asyncio.run(run_candle_aggregator_per_instrument())

