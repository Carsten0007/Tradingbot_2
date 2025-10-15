import threading
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import datetime as dt
from collections import deque
import time

# ===============================================
#   ChartManager – Live-Visualisierung pro Instrument
# ===============================================

class ChartManager:
    def __init__(self, window_size_sec=300):
        self.window = window_size_sec  # Standard: 5 Minuten
        self.data = {}   # {epic: deque([...])}
        self.lines = {}  # {epic: dict(matplotlib handles)}
        self.lock = threading.Lock()

        # Hintergrundthread für Live-Redraw
        self._stop_flag = False
        # self.thread = threading.Thread(target=self._redraw_loop, daemon=True)
        # self.thread.start()

    # -------------------------------------------------------
    #   Öffentliche Methode zum Aktualisieren eines Charts
    # -------------------------------------------------------
    def update(self, epic, ts_ms, bar, trend, pos, ema_fast=None, ema_slow=None, hma_fast=None, hma_slow=None):
        # Wird pro Sekunde aus on_candle_forming() aufgerufen.
        # ema_* und hma_* optional, wenn evaluate_trend_signal() sie liefert.
  
        with self.lock:
            if epic not in self.data:
                self._init_chart(epic)

            dq = self.data[epic]
            now = dt.datetime.fromtimestamp(ts_ms / 1000.0)

            # ✅ sicheres Auslesen von Positionswerten
            entry_price = pos.get("entry_price") if isinstance(pos, dict) else None
            trailing_stop = pos.get("trailing_stop") if isinstance(pos, dict) else None
            break_even = pos.get("break_even_level") if isinstance(pos, dict) else None

            dq.append({
                "time": now,
                "close": bar["close"],
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "trend": trend,
                "entry": entry_price,
                "sl": entry_price * (1 - 0.0018) if entry_price else None,
                "tp": entry_price * (1 + 0.0050) if entry_price else None,
                "ts": trailing_stop,
                "be": break_even,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "hma_fast": hma_fast,
                "hma_slow": hma_slow
            })

            # Rolling Window begrenzen (ältere Daten löschen)
            while len(dq) > 2 and (now - dq[0]["time"]).total_seconds() > self.window:
                dq.popleft()

            # 🔁 sofort neu zeichnen (Hauptthread)
            self._refresh_chart(epic)
            plt.pause(0.001)



    # -------------------------------------------------------
    #   Chart initialisieren (neues Instrument)
    # -------------------------------------------------------
    def _init_chart(self, epic):
        dq = deque(maxlen=600)
        self.data[epic] = dq

        fig, ax = plt.subplots()
        ax.set_title(f"Live Chart – {epic}")
        ax.set_xlabel("Zeit")
        ax.set_ylabel("Preis")
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))

        # Linien vorbereiten
        lines = {
            "price": ax.plot([], [], label="Close", color="blue")[0],
            "entry": ax.plot([], [], label="Entry", color="gray", linestyle="--")[0],
            "sl": ax.plot([], [], label="Stop-Loss", color="red", linestyle=":")[0],
            "ts": ax.plot([], [], label="Trailing", color="orange", linestyle="--")[0],
            "tp": ax.plot([], [], label="Take-Profit", color="green", linestyle=":")[0],
            "be": ax.plot([], [], label="Break-Even", color="purple", linestyle="-.")[0],
            "ema_fast": ax.plot([], [], label="EMA Fast", color="cyan")[0],
            "ema_slow": ax.plot([], [], label="EMA Slow", color="magenta")[0],
            "hma_fast": ax.plot([], [], label="HMA Fast", color="deepskyblue")[0],
            "hma_slow": ax.plot([], [], label="HMA Slow", color="violet")[0],
        }

        ax.legend(loc="upper left")
        plt.ion()
        plt.show(block=False)

        self.lines[epic] = {"fig": fig, "ax": ax, **lines}

    # -------------------------------------------------------
    #   Hintergrundthread – regelmäßige Redraws
    # -------------------------------------------------------
    def _redraw_loop(self):
        while not self._stop_flag:
            time.sleep(1.0)
            with self.lock:
                for epic in list(self.data.keys()):
                    self._refresh_chart(epic)

    # -------------------------------------------------------
    #   Einzelnes Chart updaten
    # -------------------------------------------------------
    def _refresh_chart(self, epic):
        dq = self.data[epic]
        if len(dq) < 2:
            return

        lines = self.lines[epic]
        times = [d["time"] for d in dq]
        closes = [d["close"] for d in dq]

        # Preislinie
        lines["price"].set_data(times, closes)

        # horizontale Linien
        for key in ["entry", "sl", "tp", "ts", "be"]:
            vals = [d[key] for d in dq if d[key]]
            if vals:
                lines[key].set_data(times[-len(vals):], [vals[-1]] * len(vals))
            else:
                lines[key].set_data([], [])

        # EMA/HMA Linien
        for key in ["ema_fast", "ema_slow", "hma_fast", "hma_slow"]:
            vals = [d[key] for d in dq if d[key] is not None]
            if vals:
                lines[key].set_data(times[-len(vals):], vals[-len(vals):])
            else:
                lines[key].set_data([], [])

        # Achsen neu skalieren
        ax = lines["ax"]
        ax.relim()
        ax.autoscale_view()

        lines["fig"].canvas.draw_idle()
        lines["fig"].canvas.flush_events()
        plt.pause(0.001)  # ✅ hält das Fenster aktiv und aktualisiert laufend

    # -------------------------------------------------------
    #   Stoppen
    # -------------------------------------------------------
    def stop(self):
        self._stop_flag = True
