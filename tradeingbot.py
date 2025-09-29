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

# Merker: pro Instrument zuletzt ausgegebene Sekunde
last_printed_sec = {epic: None for epic in INSTRUMENTS}

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
    #Alle offenen Positionen abfragen.
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
    # Neue Position eröffnen (Market-Order), liefert Response-Objekt zurück.
    url = f"{BASE_REST}/api/v1/positions"
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
        "Content-Type": "application/json"
    }
    data = {
        "epic": epic,
        "direction": direction,
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


def close_position(CST, XSEC, epic, deal_id=None, size=1, direction="SELL", retry=True):
    # Offene Position schließen – probiere DELETE, POST (/close), PUT und POST (/otc mit _method=DELETE)

    deal_id_str = str(deal_id) if deal_id is not None else None

    url_delete = f"{BASE_REST}/api/v1/positions/otc/{deal_id_str}" if deal_id_str else None
    url_post   = f"{BASE_REST}/api/v1/positions/close"
    url_put    = f"{BASE_REST}/api/v1/positions/otc/{deal_id_str}" if deal_id_str else None
    url_otc    = f"{BASE_REST}/api/v1/positions/otc"

    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Payload-Varianten
    data_deal = {
        "dealId": deal_id_str,
        "size": size,
        "orderType": "MARKET",
        "direction": direction
    }
    data_epic = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "orderType": "MARKET",
        "timeInForce": "FILL_OR_KILL"
    }

    # Debug: aktuelle offene Positionen checken
    try:
        positions = get_positions(CST, XSEC)
        ids = [p["position"]["dealId"] for p in positions if "position" in p]
        print("📋 Offene Positionen lt. API:", ids)
        if deal_id_str:
            print("🆔 Close-Request mit dealId:", deal_id_str, "direction:", direction)
        else:
            print("🆔 Close-Request ohne dealId → nur epic:", epic, "direction:", direction)
    except Exception as e:
        print("⚠️ get_positions vor Close fehlgeschlagen:", e)

    # Versuch 1: DELETE /positions/otc/{dealId}
    if url_delete:
        print(f"🔎 Versuche Close mit DELETE {url_delete} ...")
        r = requests.delete(url_delete, headers={**headers, "_method": "DELETE"})
        print(f"📩 Close-Response (DELETE): {r.status_code} {r.text}")
        if r.status_code == 200:
            return r

    # Versuch 2: POST /positions/close
    print(f"🔎 Versuche Close mit POST {url_post} ... Daten={data_deal}")
    r = requests.post(url_post, headers=headers, json=data_deal)
    print(f"📩 Close-Response (POST /close): {r.status_code} {r.text}")
    if r.status_code == 200:
        return r

    # Versuch 3: PUT /positions/otc/{dealId}
    if url_put:
        print(f"🔎 Versuche Close mit PUT {url_put} ... Daten={data_deal}")
        r = requests.put(url_put, headers=headers, json=data_deal)
        print(f"📩 Close-Response (PUT): {r.status_code} {r.text}")
        if r.status_code == 200:
            return r

    # Versuch 4: POST /positions/otc mit _method=DELETE + epic
    print(f"🔎 Versuche Close mit POST {url_otc} + _method=DELETE ... Daten={data_epic}")
    r = requests.post(url_otc, headers={**headers, "_method": "DELETE"}, json=data_epic)
    print(f"📩 Close-Response (POST /otc _method=DELETE): {r.status_code} {r.text}")
    if r.status_code == 200:
        return r

    # Debug: nachsehen, ob Position noch existiert
    try:
        positions = get_positions(CST, XSEC)
        ids = [p["position"]["dealId"] for p in positions if "position" in p]
        if deal_id_str and deal_id_str in ids:
            print(f"⚠️ Deal {deal_id_str} ist nach allen Versuchen noch offen!")
        else:
            print(f"✅ Position scheint entfernt (oder kein dealId mehr sichtbar).")
    except Exception as e:
        print("⚠️ Abgleich mit get_positions nach Close fehlgeschlagen:", e)

    return r


# ==============================
# SIGNAL-LOGIK (Zelle D)
# ==============================

def on_candle_forming(epic, bar, ts_ms):
    # Wird bei jedem Tick innerhalb einer Kerze aufgerufen (noch nicht geschlossen).
    closes = list(candle_history[epic]) + [bar["close"]]
    spread = (bar["high"] - bar["low"]) / max(1, bar["ticks"])
    trend = evaluate_trend_signal(epic, closes, spread)

    # Zeit konvertieren
    local_dt = to_local_dt(ts_ms)
    local_time = local_dt.strftime("%d.%m.%Y %H:%M:%S")

    # Nur letzten Tick pro Sekunde ausgeben
    sec_key = local_dt.replace(microsecond=0)
    if last_printed_sec[epic] == sec_key:
        return
    last_printed_sec[epic] = sec_key

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
    #Einfache EMA-Berechnung auf einer Liste von Werten.
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def evaluate_trend_signal(epic, closes, spread):
    #Ermittle BUY/SELL/HOLD basierend auf EMA fast/slow und Spread-Filter.
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

def safe_close(CST, XSEC, epic, deal_id=None, size=1):
    # Wrapper: Close-Order robust mit Retry und Reset in open_positions.
    # Holt sich die Richtung aus open_positions[epic] oder notfalls via get_positions().

    # Richtung und dealId ermitteln
    direction = None
    if epic in open_positions and isinstance(open_positions[epic], dict):
        direction = open_positions[epic].get("direction")
        if not deal_id:
            deal_id = open_positions[epic].get("dealId")

    # Fallback: API direkt fragen
    if not deal_id or not direction:
        positions = get_positions(CST, XSEC)
        if positions:
            for pos in positions:
                position = pos.get("position")
                if position and position.get("epic") == epic:
                    deal_id = position.get("dealId")
                    direction = position.get("direction")
                    print(f"🔎 safe_close Fallback get_positions({epic}) → dealId={deal_id}, direction={direction}")
                    break

    # Wenn immer noch nichts → Notlösung
    if not direction:
        direction = "SELL"

    # Gegenseite bestimmen
    close_dir = "SELL" if direction == "BUY" else "BUY"

    print(f"📊 [{epic}] Versuche Close (dealId={deal_id}, position={direction} → close_dir={close_dir}) ...")

    # Close-Request starten
    r = close_position(CST, XSEC, epic, deal_id=deal_id, size=size, direction=close_dir)
    ok = (r is not None and r.status_code == 200)

    if ok:
        open_positions[epic] = None
        print(f"✅ [{epic}] Close erfolgreich → open_positions reset")

        # Zusatz: nachprüfen, ob die Position wirklich weg ist
        try:
            positions = get_positions(CST, XSEC)
            ids = [p["position"]["dealId"] for p in positions if "position" in p]
            if deal_id and deal_id in ids:
                print(f"⚠️ [{epic}] Deal {deal_id} taucht nach Close noch in get_positions() auf!")
            else:
                print(f"✅ [{epic}] Deal {deal_id} ist aus get_positions() verschwunden.")
        except Exception as e:
            print(f"⚠️ [{epic}] Abgleich nach Close fehlgeschlagen:", e)
    else:
        print(f"⚠️ [{epic}] Close fehlgeschlagen (dealId={deal_id})")

    return ok



def safe_open(CST, XSEC, epic, direction, size=1):
    # Wrapper: Open-Order robust mit Retry und Update von open_positions.
    r = open_position(CST, XSEC, epic, direction, size)
    ok = (r is not None and r.status_code == 200)

    if ok:
        try:
            # Bestätigung noch einmal abfragen
            ref = r.json().get("dealReference")
            if ref:
                conf_url = f"{BASE_REST}/api/v1/confirms/{ref}"
                headers = {
                    "X-CAP-API-KEY": API_KEY,
                    "CST": CST,
                    "X-SECURITY-TOKEN": XSEC,
                    "Accept": "application/json"
                }
                conf = requests.get(conf_url, headers=headers)
                if conf.status_code == 200:
                    conf_data = conf.json()
                    deal_id = None
                    # zuerst echte Positions-ID aus affectedDeals nehmen
                    if conf_data.get("affectedDeals"):
                        deal_id = conf_data["affectedDeals"][0].get("dealId")
                    # fallback: dealId aus Confirm selbst
                    if not deal_id and conf_data.get("dealId"):
                        deal_id = conf_data.get("dealId")

                    if deal_id:
                        open_positions[epic] = {"direction": direction, "dealId": str(deal_id)}
                        print(f"🆕 [{epic}] Open erfolgreich → {direction} (dealId={deal_id})")
                        return True
        except Exception as e:
            print(f"⚠️ [{epic}] Fehler beim Bestätigen von safe_open:", e)

    else:
        print(f"⚠️ [{epic}] Open fehlgeschlagen")

    return ok


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
    # Entscheidet basierend auf Signal + aktueller Position mit Schutz-Logik + Farben.
    global open_positions

    pos = open_positions.get(epic)
    current = pos.get("direction") if isinstance(pos, dict) else None
    deal_id = pos.get("dealId") if isinstance(pos, dict) else None

    # ===========================
    # LONG-SIGNAL
    # ===========================
    if signal.startswith("READY TO TRADE: BUY"):
        if current == "BUY":
            print(Fore.GREEN + f"⚖️ [{epic}] Bereits LONG, nichts tun.")
        elif current == "SELL":
            print(Fore.YELLOW + f"📊 [{epic}] Versuche SHORT zu schließen (dealId={deal_id})")
            if safe_close(CST, XSEC, epic, deal_id=deal_id):
                safe_open(CST, XSEC, epic, "BUY")
            else:
                print(Fore.RED + f"⚠️ [{epic}] Close fehlgeschlagen, retry beim nächsten Signal")
        else:
            print(f"{Fore.YELLOW}🚀 [{epic}] Long eröffnen{Style.RESET_ALL}")
            safe_open(CST, XSEC, epic, "BUY")

    # ===========================
    # SHORT-SIGNAL
    # ===========================
    elif signal.startswith("READY TO TRADE: SELL"):
        if current == "SELL":
            print(f"{Fore.RED}⚖️ [{epic}] Bereits SHORT, nichts tun. → {signal}{Style.RESET_ALL}")
        elif current == "BUY":
            print(f"{Fore.YELLOW}📊 [{epic}] Versuche LONG zu schließen (dealId={deal_id}){Style.RESET_ALL}")
            if safe_close(CST, XSEC, epic, deal_id=deal_id):
                safe_open(CST, XSEC, epic, "SELL")
            else:
                print(Fore.RED + f"⚠️ [{epic}] Close fehlgeschlagen, retry beim nächsten Signal")
        else:
            print(f"{Fore.YELLOW}🚀 [{epic}] Short eröffnen{Style.RESET_ALL}")
            safe_open(CST, XSEC, epic, "SELL")

    # ===========================
    # KEIN KLARES SIGNAL
    # ===========================
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

