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
# CONFIG ping
# ==============================
PING_INTERVAL    = 300   # Sekunden zwischen WebSocket-Pings
RECONNECT_DELAY  = 5    # Sekunden warten nach Verbindungsabbruch
RECV_TIMEOUT     = 60   # Sekunden Timeout fürs Warten auf eine Nachricht

# ==============================
# STRATEGIE-EINSTELLUNGEN
# ==============================

EMA_FAST = 2   # kurze EMA-Periode (z. B. 9, 10, 20)
EMA_SLOW = 4  # lange EMA-Periode (z. B. 21, 30, 50)

TRADE_RISK_PCT = 0.0025  # 2% vom verfügbaren Kapital pro Trade

# ==============================
# Risk Management Parameter
# ==============================
STOP_LOSS_PCT      = 0.02   # fester Stop-Loss, z. B. -2%
TRAILING_STOP_PCT  = 0.01   # Trailing Stop, z. B. 1% Abstand


def to_local_dt(ms_since_epoch: int) -> datetime:
    return datetime.fromtimestamp(ms_since_epoch/1000, tz=timezone.utc).astimezone(LOCAL_TZ)

# Candle-Historie für EMA-Berechnung
candle_history = {epic: deque(maxlen=200) for epic in INSTRUMENTS}

# Merker: pro Instrument zuletzt ausgegebene Sekunde
last_printed_sec = {epic: None for epic in INSTRUMENTS}


# ==============================
# TRADE berechnen aufgrund von verfügbarem Kontostand und %-davon
# ==============================

def calc_trade_size(CST, XSEC, epic, risk_pct=TRADE_RISK_PCT):
    # # 1. Kontostand abrufen
    # url_acc = f"{BASE_REST}/api/v1/accounts"
    # headers = {
    #     "X-CAP-API-KEY": API_KEY,
    #     "CST": CST,
    #     "X-SECURITY-TOKEN": XSEC,
    #     "Accept": "application/json"
    # }
    # r_acc = requests.get(url_acc, headers=headers)
    # if r_acc.status_code != 200:
    #     print("⚠️ Fehler beim Abrufen des Kontostands:", r_acc.status_code, r_acc.text)
    #     return 1
    # acc_data = r_acc.json()
    # available = float(acc_data.get("availableToDeal", 0))
    # risk_amount = available * risk_pct

    # # 2. Instrument-Infos abrufen
    # url_mkt = f"{BASE_REST}/api/v1/markets/{epic}"
    # r_mkt = requests.get(url_mkt, headers=headers)
    # if r_mkt.status_code != 200:
    #     print("⚠️ Fehler beim Abrufen der Marktdaten:", r_mkt.status_code, r_mkt.text)
    #     return 1
    # mkt_data = r_mkt.json().get("instrument", {})
    # contract_size = float(mkt_data.get("contractSize", 1))
    # margin_factor = float(mkt_data.get("marginFactor", 1)) / 100  # kommt in %

    # # 3. Stück berechnen
    # # -> angenommener Kurs: letzter Preis aus Market-Details
    # snapshot = r_mkt.json().get("snapshot", {})
    # price = float(snapshot.get("bid", 1))
    # margin_per_unit = price * contract_size * margin_factor
    # if margin_per_unit <= 0:
    #     return 1
    # size = risk_amount / margin_per_unit

    # print(f"📊 calc_trade_size Debug → risk_amount={risk_amount}, margin_per_unit={margin_per_unit}, "
    #   f"raw_size={risk_amount / margin_per_unit}, size_rounded={round(size, 3)}, "
    #   f"minDealSize={mkt_data.get('minDealSize')}, lotSize={mkt_data.get('lotSize')}")

    size = 0.2 # test mit hartem wert, da im demo konto anscheinend kein kontostand übermittelt wird ...
    return round(size, 3)  # 3 Nachkommastellen, also 0.001 genau





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


def open_position(CST, XSEC, epic, direction, size, entry_price, retry=True):
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
        CST, XSEC = new_CST, new_XSEC  # global aktualisieren
        raise RuntimeError("force_reconnect")

    print("📩 Order-Response:", r.status_code, r.text)

    if r.status_code == 200:
        try:
            ref = r.json().get("dealReference")
            if ref:
                conf_url = f"{BASE_REST}/api/v1/confirms/{ref}"
                conf = requests.get(conf_url, headers=headers)
                if conf.status_code == 200:
                    conf_data = conf.json()
                    deal_id = None

                    affected = conf_data.get("affectedDeals")
                    if affected and isinstance(affected, list) and affected:
                        deal_id = affected[0].get("dealId")

                    if not deal_id and conf_data.get("dealId"):
                        deal_id = conf_data.get("dealId")

                    if deal_id:
                        open_positions[epic] = {
                            "direction": direction,
                            "dealId": deal_id,
                            "entry_price": entry_price,
                            "trailing_stop": None
                        }
                        print(f"🆕 [{epic}] Open erfolgreich → {direction} (dealId={deal_id}, entry={entry_price})")
                    else:
                        print(f"⚠️ Keine dealId aus Confirm extrahiert für {epic}")
        except Exception as e:
            print("⚠️ Confirm-Check fehlgeschlagen:", e)
    return r


def close_position(CST, XSEC, epic, deal_id=None, retry=True):
    # Offene Position schließen über DELETE /positions/{dealId}
    if not deal_id:
        print(f"⚠️ close_position: kein dealId übergeben für {epic}")
        return None

    deal_id = str(deal_id)  # API erwartet string
    url = f"{BASE_REST}/api/v1/positions/{deal_id}"
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
        "Accept": "application/json"
    }

    print(f"🔎 Versuche Close mit DELETE {url} ...")
    r = requests.delete(url, headers=headers)

    if r is None:
        print("⚠️ Close-Request hat keine Antwort geliefert!")
        return None

    if r.status_code == 401 and retry:
        print("🔑 Session abgelaufen → erneuter Login (close_position) ...")
        new_CST, new_XSEC = capital_login()
        CST, XSEC = new_CST, new_XSEC  # global aktualisieren
        raise RuntimeError("force_reconnect")

    print(f"📩 Close-Response: {r.status_code} {r.text}")
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



def safe_close(CST, XSEC, epic, deal_id=None):
    # Wrapper: Close-Order robust mit Retry und Reset in open_positions.
    # Holt sich dealId und Richtung aus open_positions oder notfalls via get_positions().

    direction = None
    full_position = None

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
                    full_position = pos  # komplette Rohdaten merken
                    deal_id = position.get("dealId")
                    direction = position.get("direction")
                    print(f"🔎 safe_close Fallback get_positions({epic}) → dealId={deal_id}, direction={direction}")
                    break

    # Debug: komplette Positionsdaten dumpen
    if full_position:
        try:
            print("📋 Vollständige Positionsdaten:", json.dumps(full_position, indent=2))
        except Exception as e:
            print("⚠️ Dump der Positionsdaten fehlgeschlagen:", e)

    # Wenn immer noch nichts → Notlösung
    if not direction:
        direction = "SELL"

    # Gegenseite nur fürs Log bestimmen
    close_dir = "SELL" if direction == "BUY" else "BUY"

    print(f"📊 [{epic}] Versuche Close (dealId={deal_id}, position={direction} → close_dir={close_dir}) ...")

    # Close-Request starten (API erwartet dealId als string)
    if deal_id is not None:
        deal_id = str(deal_id)

    r = close_position(CST, XSEC, epic, deal_id=deal_id)
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


def safe_open(CST, XSEC, epic, direction, size, entry_price):
    # Wrapper: Open-Order robust mit Retry + Ergänzen von Trailing Stop
    global open_positions

    r = open_position(CST, XSEC, epic, direction, size, entry_price)
    ok = (r is not None and r.status_code == 200)

    if ok and isinstance(open_positions.get(epic), dict):
        # Trailing Stop initial setzen
        if direction == "BUY":
            trailing_stop = entry_price * (1 - TRAILING_STOP_PCT)
        else:  # SELL
            trailing_stop = entry_price * (1 + TRAILING_STOP_PCT)

        # Nur Trailing Stop ergänzen
        open_positions[epic]["trailing_stop"] = trailing_stop
        print(f"🆕 [{epic}] Open erfolgreich → {direction} "
              f"(dealId={open_positions[epic].get('dealId')}, entry={entry_price}, trailing={trailing_stop})")

    return ok


# ==============================
# STOP LOSS & TRAILING STOP überwachen
# ==============================

def check_protection_rules(epic, price, CST, XSEC):
    # Prüft Stop-Loss und Trailing Stop für offene Positionen
    global open_positions

    pos = open_positions.get(epic)
    if not isinstance(pos, dict):
        return  # keine offene Position

    direction = pos.get("direction")
    deal_id   = pos.get("dealId")
    entry     = pos.get("entry_price")
    stop      = pos.get("trailing_stop")

    if not (direction and entry):
        return  # unvollständige Daten

    # ===== LONG =====
    if direction == "BUY":
        stop_loss_level = entry * (1 - STOP_LOSS_PCT)

        # Trailing-Stop nachziehen
        if price > entry:
            new_trailing = price * (1 - TRAILING_STOP_PCT)
            if stop is None or new_trailing > stop:
                pos["trailing_stop"] = new_trailing
                print(f"🔧 [{epic}] Trailing Stop angepasst auf {new_trailing:.2f}")

        # Stop prüfen
        if price <= stop_loss_level or (stop is not None and price <= stop):
            print(f"⛔ [{epic}] Stop ausgelöst → schließe LONG")
            safe_close(CST, XSEC, epic, deal_id=deal_id)

    # ===== SHORT =====
    elif direction == "SELL":
        stop_loss_level = entry * (1 + STOP_LOSS_PCT)

        # Trailing-Stop nachziehen
        if price < entry:
            new_trailing = price * (1 + TRAILING_STOP_PCT)
            if stop is None or new_trailing < stop:
                pos["trailing_stop"] = new_trailing
                print(f"🔧 [{epic}] Trailing Stop angepasst auf {new_trailing:.2f}")

        # Stop prüfen
        if price >= stop_loss_level or (stop is not None and price >= stop):
            print(f"⛔ [{epic}] Stop ausgelöst → schließe SHORT")
            safe_close(CST, XSEC, epic, deal_id=deal_id)


# ==============================
# HILFSFUNKTION
# ==============================

def get_last_price(CST, XSEC, epic):
    # Holt den letzten Bid/Ask für ein Instrument und gibt den Mid-Preis zurück
    url = f"{BASE_REST}/api/v1/markets/{epic}"
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
        "Accept": "application/json"
    }
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        snapshot = r.json().get("snapshot", {})
        try:
            bid = float(snapshot.get("bid", 0))
            ask = float(snapshot.get("ofr", 0))
            return (bid + ask) / 2.0
        except Exception:
            pass
    print(f"⚠️ get_last_price fehlgeschlagen für {epic} ({r.status_code})")
    return None



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
                entry_price = get_last_price(CST, XSEC, epic)
                safe_open(CST, XSEC, epic, "BUY", calc_trade_size(CST, XSEC, epic), entry_price)
            else:
                print(Fore.RED + f"⚠️ [{epic}] Close fehlgeschlagen, retry beim nächsten Signal")
        else:
            print(f"{Fore.YELLOW}🚀 [{epic}] Long eröffnen{Style.RESET_ALL}")
            entry_price = get_last_price(CST, XSEC, epic)
            safe_open(CST, XSEC, epic, "BUY", calc_trade_size(CST, XSEC, epic), entry_price)

    # ===========================
    # SHORT-SIGNAL
    # ===========================
    elif signal.startswith("READY TO TRADE: SELL"):
        if current == "SELL":
            print(f"{Fore.RED}⚖️ [{epic}] Bereits SHORT, nichts tun. → {signal}{Style.RESET_ALL}")
        elif current == "BUY":
            print(f"{Fore.YELLOW}📊 [{epic}] Versuche LONG zu schließen (dealId={deal_id}){Style.RESET_ALL}")
            if safe_close(CST, XSEC, epic, deal_id=deal_id):
                entry_price = get_last_price(CST, XSEC, epic)
                safe_open(CST, XSEC, epic, "SELL", calc_trade_size(CST, XSEC, epic), entry_price)
            else:
                print(Fore.RED + f"⚠️ [{epic}] Close fehlgeschlagen, retry beim nächsten Signal")
        else:
            print(f"{Fore.YELLOW}🚀 [{epic}] Short eröffnen{Style.RESET_ALL}")
            entry_price = get_last_price(CST, XSEC, epic)
            safe_open(CST, XSEC, epic, "SELL", calc_trade_size(CST, XSEC, epic), entry_price)

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

                last_ping = time.time()

                while True:
                    now = time.time()

                    # --- alle PING_INTERVAL Sekunden ein Ping ---
                    if now - last_ping > PING_INTERVAL:
                        try:
                            await ws.ping()
                            print("📡 Ping gesendet")
                            last_ping = now
                        except Exception as e:
                            print("⚠️ Ping fehlgeschlagen:", e)
                            break

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                        msg = json.loads(raw)
                    except asyncio.TimeoutError:
                        print("⚠️ Timeout → reconnect ...")
                        break
                    except Exception as e:
                        print("⚠️ Fehler beim Empfangen:", e)
                        if "invalid.session.token" in str(e).lower() or "force_reconnect" in str(e).lower():
                            CST, XSEC = None, None
                        break

                    if msg.get("destination") != "quote":
                        continue

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
                            b["high"] = max(b["high"], px)
                            b["low"] = min(b["low"], px)
                            b["close"] = px
                            b["ticks"] += 1

                        on_candle_forming(epic, st["bar"], ts_ms)

                        # Schutz-Regeln prüfen (Stop-Loss & Trailing Stop)
                        current_price = st["bar"]["close"]
                        check_protection_rules(epic, current_price, CST, XSEC)

        except Exception as e:
            print("❌ Verbindungsfehler:", e)
            if "invalid.session.token" in str(e).lower() or "force_reconnect" in str(e).lower():
                CST, XSEC = None, None

        print("⏳ 5s warten, dann neuer Versuch ...")
        await asyncio.sleep(RECONNECT_DELAY)

#TESTMETHODE, öffnet und schließt sofort trade
# def test_open_and_close(CST, XSEC, epic, direction="BUY", size=1):
#     print(f"🧪 Test: Öffne {direction} für {epic} ...")
#     if safe_open(CST, XSEC, epic, direction, size):
#         print(f"✅ Test-Open erfolgreich für {epic} → versuche sofort zu schließen ...")

#         pos = open_positions.get(epic)
#         deal_id = pos.get("dealId") if isinstance(pos, dict) else None

#         if safe_close(CST, XSEC, epic, deal_id=deal_id):
#             print(f"✅ Test-Close erfolgreich für {epic}")
#         else:
#             print(f"⚠️ Test-Close fehlgeschlagen für {epic}")
#     else:
#         print(f"⚠️ Test-Open fehlgeschlagen für {epic}")



# ==============================
# MAIN
# ==============================

if __name__ == "__main__":
    asyncio.run(run_candle_aggregator_per_instrument())



# ==============================
# MAIN test
# ==============================

# DEBUG_TEST = True  # 🧪 auf False setzen, wenn kein Testlauf gewünscht

# if __name__ == "__main__":
#     # Login holen
#     CST, XSEC = capital_login()

#     if DEBUG_TEST:
#         # 🧪 Test: einmal öffnen & sofort wieder schließen
#         test_open_and_close(CST, XSEC, "ETHUSD")

#     # Danach normal den Bot starten
#     asyncio.run(run_candle_aggregator_per_instrument())


