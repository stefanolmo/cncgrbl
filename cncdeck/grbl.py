"""GRBL 1.1 protocol: real-time commands, character-counting streaming, status parsing."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

import serial
from PySide6.QtCore import QThread, Signal

RX_BUFFER = 127          # GRBL serial RX buffer (128) minus one, as recommended by the GRBL wiki
SIM_PORT = "simulator"

# real-time command bytes
RT_STATUS = b"?"
RT_HOLD = b"!"
RT_RESUME = b"~"
RT_RESET = b"\x18"
RT_JOG_CANCEL = b"\x85"
RT_FEED_RESET, RT_FEED_PLUS10, RT_FEED_MINUS10, RT_FEED_PLUS1, RT_FEED_MINUS1 = (bytes([b]) for b in (0x90, 0x91, 0x92, 0x93, 0x94))
RT_RAPID_100, RT_RAPID_50, RT_RAPID_25 = (bytes([b]) for b in (0x95, 0x96, 0x97))
RT_SPINDLE_RESET, RT_SPINDLE_PLUS10, RT_SPINDLE_MINUS10, RT_SPINDLE_PLUS1, RT_SPINDLE_MINUS1 = (bytes([b]) for b in (0x99, 0x9A, 0x9B, 0x9C, 0x9D))
RT_SPINDLE_STOP = b"\x9e"

ERRORS = {
    1: "Comando G-code non riconosciuto (manca una lettera?)",
    2: "Valore numerico mancante o malformato",
    3: "Comando '$' non riconosciuto",
    4: "Valore negativo non ammesso",
    5: "Homing non abilitato ($22)",
    6: "Durata minima dell'impulso step troppo breve (< 3 µs)",
    7: "EEPROM: lettura fallita, valori di default ripristinati",
    8: "Comando '$' ammesso solo a macchina ferma (Idle)",
    9: "Comando bloccato: allarme attivo o ciclo di homing in corso",
    10: "Soft limit non abilitabile senza homing",
    11: "Riga troppo lunga (max 80 caratteri)",
    12: "Valore di setting oltre il massimo (frequenza step)",
    13: "Interruttore di sicurezza (door) aperto",
    14: "Riga troppo lunga per essere salvata in EEPROM",
    15: "Jog oltre i limiti di corsa",
    16: "Comando jog non valido",
    17: "Laser mode richiede PWM abilitato",
    20: "Comando G-code non supportato",
    21: "Conflitto di gruppo modale G-code",
    22: "Avanzamento (F) non definito",
    23: "Comando G-code richiede un valore intero",
    24: "Più comandi G-code usano gli assi nella stessa riga",
    25: "Comando G-code ripetuto nella stessa riga",
    26: "Mancano parole asse richieste dal comando",
    27: "Numero di riga (N) non valido",
    28: "Manca una parola richiesta (P/L/R...)",
    29: "Sistema di coordinate G59.x non supportato",
    30: "G53 solo con G0/G1",
    31: "Parole asse non utilizzabili in questo modo",
    32: "G2/G3 richiedono almeno una parola asse nel piano",
    33: "Target di movimento non valido",
    34: "Raggio dell'arco non valido",
    35: "G2/G3 richiedono almeno un offset (IJK) nel piano",
    36: "Parole del G-code non usate nella riga",
    37: "Offset di lunghezza utensile G43.1 su asse non supportato",
    38: "Numero utensile troppo grande",
}

ALARMS = {
    1: "Hard limit attivato: posizione probabilmente persa, rifare homing",
    2: "Movimento oltre i limiti di corsa (soft limit)",
    3: "Reset durante il movimento: posizione persa, rifare homing",
    4: "Probe fallito: sonda non nella posizione iniziale prevista",
    5: "Probe fallito: nessun contatto nella corsa prevista",
    6: "Homing fallito: reset durante il ciclo",
    7: "Homing fallito: porta aperta durante il ciclo",
    8: "Homing fallito: finecorsa non attivato",
    9: "Homing fallito: finecorsa non trovato nella corsa prevista",
}

SETTING_INFO: dict[int, tuple[str, str]] = {
    0: ("Durata impulso step", "µs"), 1: ("Ritardo disattivazione step", "ms"),
    2: ("Inversione porta step (maschera)", "mask"), 3: ("Inversione direzione (maschera)", "mask"),
    4: ("Inverti pin enable step", "bool"), 5: ("Inverti pin limit", "bool"),
    6: ("Inverti pin probe", "bool"), 10: ("Report di stato (maschera)", "mask"),
    11: ("Junction deviation", "mm"), 12: ("Tolleranza arco", "mm"),
    13: ("Report in pollici", "bool"), 20: ("Soft limits", "bool"),
    21: ("Hard limits", "bool"), 22: ("Ciclo di homing", "bool"),
    23: ("Inversione direzione homing (maschera)", "mask"), 24: ("Feed homing (locate)", "mm/min"),
    25: ("Seek homing", "mm/min"), 26: ("Debounce homing", "ms"),
    27: ("Pull-off homing", "mm"), 30: ("Velocità massima mandrino", "RPM"),
    31: ("Velocità minima mandrino", "RPM"), 32: ("Laser mode", "bool"),
    100: ("X passi/mm", "step/mm"), 101: ("Y passi/mm", "step/mm"), 102: ("Z passi/mm", "step/mm"),
    110: ("X velocità massima", "mm/min"), 111: ("Y velocità massima", "mm/min"), 112: ("Z velocità massima", "mm/min"),
    120: ("X accelerazione", "mm/s²"), 121: ("Y accelerazione", "mm/s²"), 122: ("Z accelerazione", "mm/s²"),
    130: ("X corsa massima", "mm"), 131: ("Y corsa massima", "mm"), 132: ("Z corsa massima", "mm"),
}


@dataclass
class Status:
    state: str = "Disconnected"
    mpos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    wpos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    wco: tuple[float, float, float] = (0.0, 0.0, 0.0)
    feed: float = 0.0
    spindle: float = 0.0
    ov_feed: int = 100
    ov_rapid: int = 100
    ov_spindle: int = 100
    pins: str = ""
    planner_free: int = 0
    rx_free: int = 0
    accessories: str = ""

    @property
    def base_state(self) -> str:
        return self.state.split(":", 1)[0]


def parse_status(line: str, prev: Status, report_mpos_default: bool = True) -> Status:
    """Parse `<Idle|MPos:..|FS:..>` into a Status, carrying over fields not present in this report."""
    st = Status(**{**prev.__dict__})
    parts = line.strip()[1:-1].split("|")
    st.state = parts[0]
    st.pins = ""
    got_m = got_w = False
    for p in parts[1:]:
        key, _, val = p.partition(":")
        try:
            if key == "MPos":
                st.mpos = tuple(float(v) for v in val.split(",")[:3])  # type: ignore
                got_m = True
            elif key == "WPos":
                st.wpos = tuple(float(v) for v in val.split(",")[:3])  # type: ignore
                got_w = True
            elif key == "WCO":
                st.wco = tuple(float(v) for v in val.split(",")[:3])  # type: ignore
            elif key == "FS":
                f, s = val.split(",")[:2]
                st.feed, st.spindle = float(f), float(s)
            elif key == "F":
                st.feed = float(val)
            elif key == "Ov":
                a, b, c = (int(v) for v in val.split(",")[:3])
                st.ov_feed, st.ov_rapid, st.ov_spindle = a, b, c
            elif key == "Pn":
                st.pins = val
            elif key == "Bf":
                a, b = (int(v) for v in val.split(",")[:2])
                st.planner_free, st.rx_free = a, b
            elif key == "A":
                st.accessories = val
        except ValueError:
            continue
    if got_m:
        st.wpos = tuple(m - o for m, o in zip(st.mpos, st.wco))  # type: ignore
    elif got_w:
        st.mpos = tuple(w + o for w, o in zip(st.wpos, st.wco))  # type: ignore
    return st


@dataclass
class _Pending:
    nbytes: int
    tag: int          # >=0 program index, -1 manual, -2 internal (silent)
    text: str


class GrblConnection(QThread):
    """Owns the serial port. All I/O happens in this thread; public methods are thread-safe."""

    connectionChanged = Signal(bool, str)
    statusChanged = Signal(object)
    console = Signal(str, str)                 # text, kind: tx rx info error alarm msg
    lineAcked = Signal(int, bool, int)         # program index, ok, error code
    programState = Signal(str)                 # running paused done aborted error
    settingsChanged = Signal(object)
    parserState = Signal(str)
    probeResult = Signal(float, float, float, bool)
    alarmRaised = Signal(int, str)
    versionInfo = Signal(str)

    def __init__(self, port: str, baud: int = 115200, parent=None):
        super().__init__(parent)
        self.port = port
        self.baud = baud
        self._ser = None
        self._lock = threading.Lock()
        self._rt: deque[bytes] = deque()
        self._manual: deque[tuple[str, int]] = deque()
        self._pending: deque[_Pending] = deque()
        self._program: list[tuple[int, str]] = []
        self._pi = 0
        self._running = False
        self._paused = False
        self._acked = 0
        self._stop_thread = False
        self._stopping_at: float | None = None
        self._status = Status()
        self._settings: dict[int, float] = {}
        self._last_poll = 0.0
        self._poll_interval = 0.2
        self._connected = False
        self._rxbuf = b""
        self._error_hold = False
        self._finish_when_idle = False
        self._reset_at = 0.0

    # ---------- public API (main thread) ----------
    @property
    def settings(self) -> dict[int, float]:
        return dict(self._settings)

    @property
    def status(self) -> Status:
        return self._status

    @property
    def is_streaming(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def send_line(self, text: str, silent: bool = False) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock:
            self._manual.append((text, -2 if silent else -1))

    def send_realtime(self, data: bytes) -> None:
        with self._lock:
            self._rt.append(data)

    def start_program(self, commands: list[tuple[int, str]]) -> None:
        with self._lock:
            self._program = list(commands)
            self._pi = 0
            self._acked = 0
            self._running = True
            self._paused = False
            self._error_hold = False
            self._finish_when_idle = False
        self.programState.emit("running")

    def pause(self) -> None:
        with self._lock:
            if self._running and not self._paused:
                self._paused = True
                self._rt.append(RT_HOLD)
        self.programState.emit("paused")

    def resume(self) -> None:
        with self._lock:
            if self._running:
                self._paused = False
                self._error_hold = False
                self._rt.append(RT_RESUME)
        self.programState.emit("running")

    def stop(self) -> None:
        """Abort: feed-hold to decelerate, then soft reset once stopped (keeps position)."""
        with self._lock:
            was = self._running
            self._running = False
            self._paused = False
            self._manual.clear()
            self._program = []
            if self._status.base_state in ("Run", "Jog", "Hold") or was:
                self._rt.append(RT_HOLD)
                self._stopping_at = time.monotonic()
            else:
                self._stopping_at = None
        if was:
            self.programState.emit("aborted")

    def reset(self) -> None:
        with self._lock:
            was = self._running
            self._running = False
            self._paused = False
            self._manual.clear()
            self._program = []
            self._stopping_at = None
            self._rt.append(RT_RESET)
        if was:
            self.programState.emit("aborted")

    def disconnect_port(self) -> None:
        self._stop_thread = True

    # ---------- thread ----------
    def _open(self):
        if self.port == SIM_PORT:
            from .simulator import SimulatedSerial
            return SimulatedSerial()
        return serial.Serial(self.port, self.baud, timeout=0.005, write_timeout=1.0)

    def run(self) -> None:
        try:
            self._ser = self._open()
        except (serial.SerialException, OSError) as e:
            self.connectionChanged.emit(False, f"Impossibile aprire {self.port}: {e}")
            return
        try:
            self._handshake()
            self._loop()
        except (serial.SerialException, OSError) as e:
            self.console.emit(f"Connessione persa: {e}", "error")
            self.connectionChanged.emit(False, f"Connessione persa: {e}")
            self._finalize(False)
            return
        self._finalize(True)

    def _finalize(self, clean: bool) -> None:
        if self._running:
            self._running = False
            self.programState.emit("aborted")
        try:
            if self._ser is not None:
                if clean and self._connected:
                    try:
                        self._ser.write(RT_HOLD if self._status.base_state in ("Run", "Jog") else b"")
                    except Exception:
                        pass
                self._ser.close()
        except Exception:
            pass
        if clean:
            self.connectionChanged.emit(False, "Disconnesso")
        self._connected = False

    def _handshake(self) -> None:
        """Arduino boards reset when the port opens; wait for the banner, nudging if silent."""
        t0 = time.monotonic()
        nudged = probed = False
        while not self._stop_thread:
            self._read_lines(self._handle_line_pre)
            if self._connected:
                break
            dt = time.monotonic() - t0
            if dt > 0.8 and not probed:
                self._ser.write(RT_STATUS)  # board may already be running (no reset on open)
                probed = True
            if dt > 2.0 and not nudged:
                self._ser.write(RT_RESET)
                nudged = True
            if dt > 5.0:
                raise serial.SerialException("Nessuna risposta da GRBL (baudrate o porta errati?)")
        if self._stop_thread:
            return
        self.connectionChanged.emit(True, f"Connesso a {self.port}")
        for cmd in ("$I", "$$", "$G"):
            self._manual.append((cmd, -2))

    def _handle_line_pre(self, line: str) -> None:
        if line.startswith("Grbl") or line.startswith("[VER") or line.startswith("<"):
            self._connected = True
            if line.startswith("Grbl"):
                self._on_banner(line)

    def _loop(self) -> None:
        while not self._stop_thread:
            self._service_realtime()
            self._read_lines(self._handle_line)
            self._poll_status()
            self._check_stopping()
            self._feed()

    # ---------- I/O helpers ----------
    def _service_realtime(self) -> None:
        with self._lock:
            items = list(self._rt)
            self._rt.clear()
        for b in items:
            self._ser.write(b)
            if b == RT_RESET:
                self._pending.clear()
                self._reset_at = time.monotonic()
                self.console.emit("Soft reset (Ctrl-X)", "info")

    def _read_lines(self, handler) -> None:
        data = self._ser.read(max(1, getattr(self._ser, "in_waiting", 0)))
        if not data:
            return
        self._rxbuf += data
        while b"\n" in self._rxbuf:
            raw, self._rxbuf = self._rxbuf.split(b"\n", 1)
            line = raw.decode("ascii", "replace").strip()
            if line:
                handler(line)

    def _poll_status(self) -> None:
        now = time.monotonic()
        if now - self._last_poll >= self._poll_interval:
            self._last_poll = now
            self._ser.write(RT_STATUS)

    def _check_stopping(self) -> None:
        if self._stopping_at is None:
            return
        st = self._status.state
        if st.startswith("Hold:0") or st in ("Idle", "Alarm") or time.monotonic() - self._stopping_at > 4.0:
            self._stopping_at = None
            self._ser.write(RT_RESET)
            self._pending.clear()
            self._reset_at = time.monotonic()
            self.console.emit("Programma interrotto, reset di GRBL", "info")

    def _rx_used(self) -> int:
        return sum(p.nbytes for p in self._pending)

    def _feed(self) -> None:
        """Send as many lines as fit in GRBL's RX buffer (character-counting protocol)."""
        if self._stopping_at is not None:
            return
        while True:
            with self._lock:
                item: tuple[str, int] | None = None
                if self._manual:
                    text, tag = self._manual[0]
                    n = len(text) + 1
                    if self._rx_used() + n <= RX_BUFFER:
                        item = self._manual.popleft()
                elif self._running and not self._paused and not self._error_hold and self._pi < len(self._program):
                    idx, text = self._program[self._pi]
                    n = len(text) + 1
                    if self._rx_used() + n <= RX_BUFFER:
                        item = (text, self._pi)
                        self._pi += 1
            if item is None:
                return
            text, tag = item
            data = (text + "\n").encode("ascii", "replace")
            self._ser.write(data)
            self._pending.append(_Pending(len(data), tag, text))
            if tag == -1:
                self.console.emit(text, "tx")

    # ---------- parsing ----------
    def _on_banner(self, line: str) -> None:
        self._pending.clear()
        self._stopping_at = None
        self.console.emit(line, "info")
        self.versionInfo.emit(line)
        if self._running:
            with self._lock:
                self._running = False
            self.programState.emit("aborted")

    def _handle_line(self, line: str) -> None:
        if line.startswith("<"):
            st = parse_status(line, self._status)
            self._status = st
            self._poll_interval = 0.1 if st.base_state in ("Run", "Jog", "Home") else 0.2
            self.statusChanged.emit(st)
            self._maybe_finish(st)
        elif line == "ok":
            self._on_ack(True, 0)
        elif line.startswith("error:"):
            try:
                code = int(line[6:])
            except ValueError:
                code = -1
            self._on_ack(False, code)
        elif line.startswith("ALARM:"):
            try:
                code = int(line[6:])
            except ValueError:
                code = -1
            msg = ALARMS.get(code, "Allarme sconosciuto")
            self.console.emit(f"ALLARME {code}: {msg}", "alarm")
            self.alarmRaised.emit(code, msg)
            if self._running:
                with self._lock:
                    self._running = False
                self.programState.emit("aborted")
        elif line.startswith("Grbl"):
            self._on_banner(line)
        elif line.startswith("$") and "=" in line:
            try:
                k, v = line[1:].split("=", 1)
                self._settings[int(k)] = float(v.split()[0])
            except ValueError:
                pass
            self.console.emit(line, "rx")
        elif line.startswith("[GC:"):
            self.parserState.emit(line[4:-1])
            self.console.emit(line, "rx")
        elif line.startswith("[PRB:"):
            try:
                coords, ok = line[5:-1].split(":")
                x, y, z = (float(v) for v in coords.split(",")[:3])
                self.probeResult.emit(x, y, z, ok == "1")
            except ValueError:
                pass
            self.console.emit(line, "rx")
        elif line.startswith("[MSG:") or line.startswith("[VER:") or line.startswith("[OPT:"):
            self.console.emit(line, "msg")
        else:
            self.console.emit(line, "rx")

    def _on_ack(self, ok: bool, code: int) -> None:
        if not self._pending:
            if time.monotonic() - self._reset_at > 1.5:  # right after a reset, in-flight replies are expected
                self.console.emit("ok/error senza comando in coda (sincronizzazione persa?)", "error")
            return
        p = self._pending.popleft()
        if p.tag == -1:
            if ok:
                self.console.emit("ok", "rx")
            else:
                self.console.emit(f"error:{code} {ERRORS.get(code, '')}", "error")
        elif p.tag == -2:
            if not ok:
                self.console.emit(f"error:{code} {ERRORS.get(code, '')} ({p.text})", "error")
            elif p.text == "$$":
                self.settingsChanged.emit(dict(self._settings))
        else:
            self._acked = p.tag + 1
            if not ok:
                src = self._program[p.tag][0] if p.tag < len(self._program) else -1
                self.console.emit(f"error:{code} {ERRORS.get(code, '')} - riga {src + 1}: {p.text}", "error")
                with self._lock:
                    self._error_hold = True
                self.send_realtime(RT_HOLD)
                self.programState.emit("error")
            self.lineAcked.emit(p.tag, ok, code)
            if self._acked >= len(self._program) and self._running:
                self._finish_when_idle = True
        if p.tag in (-1, -2) and p.text.startswith("$") and p.text[1:2].isdigit() and "=" in p.text and ok:
            # setting written: refresh cache
            self._manual.append(("$$", -2))

    def _maybe_finish(self, st: Status) -> None:
        if self._finish_when_idle and self._running and st.state == "Idle" and not self._pending:
            with self._lock:
                self._running = False
                self._finish_when_idle = False
            self.programState.emit("done")
