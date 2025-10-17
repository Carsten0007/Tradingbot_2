import threading
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import datetime as dt
from collections import deque
import time


class ChartManager:
    def __init__(self, window_size_sec=300):
        self.window = window_size_sec
        self.data = {}      # {epic: deque([...])}
        self.lines = {}     # {epic: dict(matplotlib handles)}
        self.lock = threading.Lock()
        self.last_trade_state = {}

        plt.ion()  # Interaktiver Modus aktiv

    # -------------------------------------------------------
    #   Öffentliche Methode zum Aktualisieren
    # -------------------------------------------------------
    def update(
        self, epic, ts_ms, bar, pos,
        ema_fast=None, ema_slow=None, hma_fast=None, hma_slow=None,
        entry=None, sl=None, tp=None, ts=None
):
        with self.lock:
            if pos is None:
                pos = {}

            if epic not in self.data:
                self._init_chart(epic)

            dq = self.data[epic]

            # Einheitliche lokale Zeit (identisch zu tradeingbot)
            try:
                from tradeingbot import to_local_dt
                now = to_local_dt(ts_ms)
            except ImportError:
                now = dt.datetime.fromtimestamp(ts_ms / 1000.0)

            # Positionsdaten sicher lesen
            entry_price = pos.get("entry_price") if isinstance(pos, dict) else None
            trailing_stop = pos.get("trailing_stop") if isinstance(pos, dict) else None
            break_even = pos.get("break_even_level") if isinstance(pos, dict) else None
            direction = pos.get("direction") if isinstance(pos, dict) else None

            # Bid / Ask übernehmen
            # 🧩 Sicherstellen, dass Bid/Ask immer float sind
            bid = bar.get("bid")
            ask = bar.get("ask")
            # Falls gültige Werte vorhanden → in float umwandeln
            bid = float(bid) if bid not in (None, "None") else None
            ask = float(ask) if ask not in (None, "None") else None



            # 🧠 Letzten Datensatz übernehmen, falls neue Werte fehlen
            last = dq[-1] if dq else {}

            entry = bar.get("entry") or last.get("entry")
            sl = bar.get("sl") or last.get("sl")
            tp = bar.get("tp") or last.get("tp")
            ts = bar.get("ts") or last.get("ts")
            be = bar.get("be") or last.get("be")

            dq.append({
                "time": now,
                "bid": bid,
                "ask": ask,
                "close": bar.get("close"),
                "entry": entry or entry_price,
                "sl": float(pos.get("stop_loss")) if isinstance(pos.get("stop_loss"), (int, float, str)) and pos.get("stop_loss") not in (None, "None") else None,
                "tp": float(pos.get("take_profit")) if isinstance(pos.get("take_profit"), (int, float, str)) and pos.get("take_profit") not in (None, "None") else None,
                "ts": float(pos.get("trailing_stop")) if isinstance(pos.get("trailing_stop"), (int, float, str)) and pos.get("trailing_stop") not in (None, "None") else None,
                "be": float(pos.get("break_even_level")) if isinstance(pos.get("break_even_level"), (int, float, str)) and pos.get("break_even_level") not in (None, "None") else None,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "hma_fast": hma_fast,
                "hma_slow": hma_slow,
                "direction": direction,
            })



            # 🕒 Time-Sync-Fix – Datenpunkte chronologisch halten
            # Falls ein verspäteter Tick kommt, sortieren wir die deque neu
            if len(dq) > 2 and dq[-1]["time"] < dq[-2]["time"]:
                dq = deque(sorted(dq, key=lambda x: x["time"]), maxlen=dq.maxlen)
                self.data[epic] = dq


            # Rolling Window begrenzen
            while len(dq) > 2 and (now - dq[0]["time"]).total_seconds() > self.window:
                dq.popleft()

           # 🧠 Trade-Zustand prüfen
            trade_open = bool(direction)
            last_state = self.last_trade_state.get(epic)

            if trade_open:
                # Trade aktiv → Entry immer aktualisieren (Marker bleibt sichtbar)
                self._mark_entry(epic, entry_price)


            elif not trade_open and last_state:
                # Trade wurde beendet – jetzt wirklich löschen
                print(f"[Chart] {epic}: Trade beendet, lösche Linien ...")
                self._clear_trade_lines(epic)
                # 🧩 Auch visuelle Linien sofort löschen
                for key in ["sl", "tp", "ts", "be"]:
                    self.lines[epic][key].set_data([], [])
                self.lines[epic]["entry_marker"].set_data([], [])
                self.lines[epic]["entry"].set_data([], [])
                self.lines[epic]["fig"].canvas.draw_idle()

                # Stelle sicher, dass direction zurückgesetzt wird
                for d in dq:
                    d["direction"] = None


            # elif not trade_open and not last_state:
                # Kein Trade aktiv → sicherheitshalber auch alle Linien leeren
                # self._clear_trade_lines(epic)

            # Status speichern
            self.last_trade_state[epic] = trade_open


            # 🔁 Refresh
            self._refresh_chart(epic)
            plt.draw()
            self.lines[epic]["fig"].canvas.flush_events()



    # -------------------------------------------------------
    #   Neues Instrument initialisieren
    # -------------------------------------------------------
    def _init_chart(self, epic):
        dq = deque(maxlen=600)
        self.data[epic] = dq

        fig, ax = plt.subplots()
        fig.canvas.manager.window.attributes('-topmost', 0)
        ax.set_title(f"Live Chart – {epic}")
        ax.set_xlabel("Zeit")
        ax.set_ylabel("Preis")
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))

        # 🧭 Initial-Skalierung – 5 Minuten Fenster und Dummy-Y-Range
        now = dt.datetime.now()
        ax.set_xlim(now - dt.timedelta(seconds=self.window), now)

        # Temporärer Y-Bereich, damit nichts flackert (z. B. ±1 % um 4000)
        # 🧠 Dynamischer Startbereich – später auto-angepasst
        ax.set_ylim(3800, 4100)   # breiter Startbereich, damit Kurs sofort sichtbar ist



        # Linien vorbereiten – deutlicher & konsistenter
        lines = {
            "bid": ax.plot([], [], label="Bid", color="lightblue", linewidth=0.8, alpha=0.8)[0],
            "ask": ax.plot([], [], label="Ask", color="lightcoral", linewidth=0.8, alpha=0.8)[0],
            "entry": ax.plot([], [], label="Entry", color="gray", linestyle="--")[0],
            "sl": ax.plot([], [], label="Stop-Loss", color="red", linestyle=":", linewidth=1.1)[0],
            "ts": ax.plot([], [], label="Trailing", color="orange", linestyle="--", linewidth=1.1)[0],
            "tp": ax.plot([], [], label="Take-Profit", color="green", linestyle=":", linewidth=1.1)[0],
            "be": ax.plot([], [], label="Break-Even", color="purple", linestyle="-.", linewidth=1.1)[0],
            "ema_fast": ax.plot([], [], label="EMA Fast", color="cyan", linewidth=0.9, alpha=0.8)[0],
            "ema_slow": ax.plot([], [], label="EMA Slow", color="magenta", linewidth=0.9, alpha=0.8)[0],
            "hma_fast": ax.plot([], [], label="HMA Fast", color="deepskyblue", linewidth=0.9, alpha=0.8)[0],
            "hma_slow": ax.plot([], [], label="HMA Slow", color="violet", linewidth=0.9, alpha=0.8)[0],
        }

        # Marker für Entry
        lines["entry_marker"] = ax.plot([], [], "go", markersize=8, label="Entry")[0]

        ax.legend(loc="upper left", fontsize="small")
        plt.show(block=False)
        self.lines[epic] = {"fig": fig, "ax": ax, **lines}

    # -------------------------------------------------------
    #   Chart-Redraw
    # -------------------------------------------------------
    def _refresh_chart(self, epic):
        dq = self.data[epic]
        if len(dq) < 1:
            return

        lines = self.lines[epic]
        times = [d["time"] for d in dq]

        # 🚫 Falls kein Trade offen → BE-, SL-, TP-, TS-Linien löschen
        if not any(d.get("direction") for d in dq if d.get("direction")):
            for key in ["sl", "tp", "ts", "be"]:
                lines[key].set_data([], [])


        # 🔧 Immer letzten bekannten Wert übernehmen (statt leere Stellen)
        bids, asks = [], []
        last_bid = last_ask = None
        for d in dq:
            if d.get("bid") is not None:
                last_bid = d["bid"]
            if d.get("ask") is not None:
                last_ask = d["ask"]
            bids.append(last_bid)
            asks.append(last_ask)

        # 📊 Nur gültige Punkte verwenden
        valid_points = [(t, b, a) for t, b, a in zip(times, bids, asks) if b is not None and a is not None]
        if not valid_points:
            return  # kein einziger Tick → nichts zeichnen

        t_vals, b_vals, a_vals = zip(*valid_points)
        lines["bid"].set_data(t_vals, b_vals)
        lines["ask"].set_data(t_vals, a_vals)

        # 📏 Dynamische Initial-Skalierung
        ax = self.lines[epic]["ax"]
        mid = (b_vals[-1] + a_vals[-1]) / 2
        ax.set_ylim(mid - 15, mid + 15)


        # 📈 EMA/HMA-Linien mit Sanity-Check (nur wenn genügend gültige Werte)
        for key in ["ema_fast", "ema_slow", "hma_fast", "hma_slow"]:
            vals = [d[key] for d in dq if isinstance(d.get(key), (int, float))]
            if len(vals) >= 3:
                valid_times = [d["time"] for d in dq if isinstance(d.get(key), (int, float))]
                lines[key].set_data(valid_times, vals)
            else:
                lines[key].set_data([], [])
        
        # 🔹 Einzelwerte (Stop, TP, Trailing, Break-Even)
        for key in ["sl", "tp", "ts", "be"]:
            # Letzten gültigen Wert übernehmen (z. B. bei Trade weiter aktiv)
            last_val = next((d[key] for d in reversed(dq) if isinstance(d.get(key), (int, float))), None)
            if last_val is not None:
                valid_times = [d["time"] for d in dq if isinstance(d.get(key), (int, float))]
                vals = [d[key] for d in dq if isinstance(d.get(key), (int, float))]
                lines[key].set_data(valid_times, vals)
            else:
                lines[key].set_data([], [])

        # 🕒 X-Achsen-Fenster stabilisieren – immer "rollendes" Zeitfenster
        ax = lines["ax"]

        if bids or asks:
            # 🧩 Falls Startbereich zu klein oder leer, dynamisch skalieren
            if any(bids) and any(asks):
                mid = (bids[-1] + asks[-1]) / 2
                ax.set_ylim(mid - 15, mid + 15)

            # 🩹 Dynamische X-Achsen-Anpassung – beim Start langsam wachsen lassen
            if (times[-1] - times[0]).total_seconds() < self.window:
                ax.set_xlim(times[0], times[-1])
            else:
                max_time = times[-1]
                min_time = max_time - dt.timedelta(seconds=self.window)
                ax.set_xlim(min_time, max_time)
            
            # 🕒 Dynamische Skalierung: erst wachsen, dann scrollen
            elapsed = (times[-1] - times[0]).total_seconds()
            if elapsed < self.window:
                # Fenster wächst mit – füllt sich von links
                ax.set_xlim(times[0], times[-1])
            else:
                # Danach: rollendes 5-Minuten-Fenster
                max_time = times[-1]
                min_time = max_time - dt.timedelta(seconds=self.window)
                ax.set_xlim(min_time, max_time)

            # Y-Achse weiterhin automatisch, aber leicht gepuffert
            all_prices = [v for v in (bids + asks) if v is not None]
            if all_prices:
                ymin, ymax = min(all_prices), max(all_prices)
                padding = (ymax - ymin) * 0.02 if ymax > ymin else 0.01
                ax.set_ylim(ymin - padding, ymax + padding)

        lines["fig"].canvas.draw_idle()
        lines["fig"].canvas.flush_events()

        #if len(bids) > 0:
            #print(f"[Chart Debug] {epic} Bid={bids[-1]:.2f} Ask={asks[-1]:.2f} -> Ylim={ax.get_ylim()}")


    # -------------------------------------------------------
    #   Entry-Marker
    # -------------------------------------------------------
    def _mark_entry(self, epic, price):
        if not price or epic not in self.lines:
            return

        # Nur markieren, wenn noch kein Entry vorhanden ist
        existing_x, existing_y = self.lines[epic]["entry_marker"].get_data()
        if existing_x and existing_y:
            return  # bereits gesetzt

        # Zeitpunkt des letzten gültigen Datensatzes
        t = self.data[epic][-1]["time"] if self.data.get(epic) else dt.datetime.now()

        # Marker + Linie setzen
        self.lines[epic]["entry_marker"].set_data([t], [price])
        self.lines[epic]["entry"].set_data([t - dt.timedelta(seconds=1), t + dt.timedelta(seconds=1)], [price, price])

        print(f"[Chart Debug] Entry fixiert {epic} @ {price:.2f}")


    # -------------------------------------------------------
    #   Linien löschen nach Trade-Ende
    # -------------------------------------------------------
    def _clear_trade_lines(self, epic):
        if epic not in self.lines:
            return

        for key in ["entry", "sl", "tp", "ts", "be", "entry_marker"]:
            self.lines[epic][key].set_data([], [])

        # Reset des letzten Zustands
        self.last_trade_state[epic] = False

        print(f"[Chart] Trade beendet → Linien für {epic} entfernt, Bid/Ask bleiben erhalten")
        self.lines[epic]["fig"].canvas.draw_idle()

