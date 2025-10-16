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
            bid = bar.get("bid")
            ask = bar.get("ask")

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
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "ts": ts or trailing_stop,  # Sicherungsfallback
                "be": be or break_even,
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

            if trade_open and not last_state:
                # Neuer Trade erkannt
                self._mark_entry(epic, entry_price)

            elif not trade_open and last_state:
                # 🧹 Trade wurde gerade geschlossen → sofort alles löschen
                self._clear_trade_lines(epic)
                print(f"[Chart] Trade geschlossen → Linien für {epic} entfernt")

            elif not trade_open and not last_state:
                # Kein Trade aktiv → sicherheitshalber auch alle Linien leeren
                self._clear_trade_lines(epic)

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
        ax.set_ylim(3990, 4010)


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
        if len(dq) < 2:
            return

        lines = self.lines[epic]
        times = [d["time"] for d in dq]

        # Bid / Ask nur zeichnen, wenn Werte vorhanden
        bids = [d["bid"] for d in dq if d["bid"] is not None]
        asks = [d["ask"] for d in dq if d["ask"] is not None]
        if bids:
            lines["bid"].set_data(times[-len(bids):], bids[-len(bids):])
        if asks:
            lines["ask"].set_data(times[-len(asks):], asks[-len(asks):])

        # Stops, Trailing, etc. – durchgängig zeichnen
        for key in ["entry", "sl", "tp", "ts", "be"]:
            y = []
            for d in dq:
                val = d.get(key)
                if val is not None:
                    y.append(val)
                elif len(y) > 0:
                    # Wenn kein neuer Wert, letzten Wert fortführen
                    y.append(y[-1])
                else:
                    y.append(None)
            if any(v is not None for v in y):
                lines[key].set_data(times, y)
            else:
                lines[key].set_data([], [])



        # 📈 EMA/HMA-Linien mit Sanity-Check (nur wenn genügend gültige Werte)
        for key in ["ema_fast", "ema_slow", "hma_fast", "hma_slow"]:
            vals = [d[key] for d in dq if isinstance(d.get(key), (int, float))]
            # Zeichne nur, wenn mindestens 3 aufeinanderfolgende Werte vorliegen
            if len(vals) >= 3:
                # Verwende gleiche Zeitlänge wie Werte, aber keine NaNs
                valid_times = [d["time"] for d in dq if isinstance(d.get(key), (int, float))]
                lines[key].set_data(valid_times, vals)
            else:
                # Noch zu wenige Punkte → Linie leer lassen
                lines[key].set_data([], [])


            # Nur zeichnen, wenn es mindestens ein gültiges Segment gibt
            if any(v is not None for v in y):
                lines[key].set_data(times, y)
            else:
                lines[key].set_data([], [])


        # 🕒 X-Achsen-Fenster stabilisieren – immer "rollendes" Zeitfenster
        ax = lines["ax"]

        if bids or asks:
            # Grenzen fest auf das 5-Minuten-Fenster setzen
            min_time = times[0]
            max_time = min_time + dt.timedelta(seconds=self.window)
            ax.set_xlim(min_time, max_time)

            # Y-Achse weiterhin automatisch, aber leicht gepuffert
            all_prices = [v for v in (bids + asks) if v is not None]
            if all_prices:
                ymin, ymax = min(all_prices), max(all_prices)
                padding = (ymax - ymin) * 0.02 if ymax > ymin else 0.01
                ax.set_ylim(ymin - padding, ymax + padding)

        lines["fig"].canvas.draw_idle()
        lines["fig"].canvas.flush_events()

    # -------------------------------------------------------
    #   Entry-Marker
    # -------------------------------------------------------
    def _mark_entry(self, epic, price):
        if epic in self.lines:
            t = dt.datetime.now()
            self.lines[epic]["entry_marker"].set_data([t], [price])

    # -------------------------------------------------------
    #   Linien löschen nach Trade-Ende
    # -------------------------------------------------------
    def _clear_trade_lines(self, epic):
        if epic not in self.lines:
            return
        dq = self.data.get(epic, deque())
        dq.clear()  # <- leert die Datenpunkte
        for key in ["entry", "sl", "tp", "ts", "be", "entry_marker"]:
            self.lines[epic][key].set_data([], [])
        self.lines[epic]["fig"].canvas.draw_idle()
