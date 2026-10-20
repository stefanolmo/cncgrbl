# Prompt: ricostruire "CNC Deck" (sender GRBL + convertitore SVG → G-code)

> Da incollare per intero a un'altra IA di programmazione (con accesso a shell e filesystem). Descrive il software
> **così com'è alla versione 0.5.0**: scopo, stack, architettura, comportamenti esatti, convenzioni da non cambiare,
> trappole già incontrate e criteri di accettazione. Le sezioni «Decisioni vincolanti» e «Trappole note» contengono lezioni
> pagate con bug reali: leggile prima di scrivere codice.

---

## 0. Come devi lavorare

1. Scrivi tu tutto il codice, testa tutto, non dichiarare "fatto" senza aver eseguito i test e **guardato** gli screenshot
   dell'interfaccia (vedi §13). Mostra l'output dei test; se qualcosa fallisce dillo.
2. Procedi per moduli, nell'ordine del §3, e dopo ciascuno scrivi/esegui i relativi test. Non passare al successivo con test rossi.
3. Non hai una macchina CNC: usa il **simulatore GRBL** descritto al §6 (lo scrivi tu) per ogni test di streaming.
4. Interfaccia utente e messaggi in **italiano**. Codice, nomi e commenti in inglese. Commenti solo dove spiegano un "perché" non ovvio.
5. Non aggiungere funzioni non richieste. Dove una scelta non è specificata, scegli la più semplice e documentala nel README.
6. Alla fine: README, `install.sh`, `.desktop`, icona, `pyproject.toml`, test, screenshot per il README (§14).

## 1. Prodotto

**CNC Deck** è un'applicazione desktop per Linux (testata su **Ubuntu 26.04 LTS**) che:

- **Modalità 1 – Sender CNC**: carica un file G-code, lo mostra in anteprima 3D e lo invia via porta seriale a una CNC con
  **GRBL 1.1** (macchina di riferimento: **CNC 1610 Pro**, scheda con chip CH340, 115200 baud, area ~160×100×45 mm, mandrino
  PWM). Offre le funzioni di Candle (DRO, jog, zero, probe, override, impostazioni `$$`, console), con grafica più curata.
- **Modalità 2 – Convertitore SVG**: carica un `.svg`, l'utente configura utensile e operazioni (approccio **jscut**),
  vede l'anteprima 3D del percorso utensile, e può **salvare** il `.gcode` oppure **inviarlo al Sender in memoria** con un clic
  ("Invia alla CNC ➜"), senza passare dal disco. L'invio carica soltanto: **non avvia mai** il taglio da solo.
- Un'unica finestra principale; un interruttore a due posizioni nella toolbar ("Sender CNC" | "Convertitore SVG") cambia pagina
  all'istante. La connessione seriale e un lavoro in corso restano attivi anche mentre si è nel convertitore.
- Schermata iniziale animata di 3 secondi.
- Modalità demo **senza macchina**: `cncdeck --sim` collega un simulatore GRBL interno.

Utente tipo: hobbista, macchina "a piatto mobile" sull'asse Y (non scriverlo mai nell'interfaccia), file G-code che arrivano da
jscut e simili con **Y negativa** (zero in alto a sinistra del disegno), spostamenti in aria fatti con `G1` e non `G0`.

## 2. Stack e vincoli

- Python ≥ 3.10 (sviluppato su 3.13). **PySide6** (Qt 6, pacchetto `PySide6-Essentials`), **pyserial**, **numpy**,
  **pyclipper** (binding di Clipper, la stessa libreria geometrica di jscut), **svgelements** (parsing SVG). Test: **pytest**.
- Nessun OpenGL: il viewer 3D usa solo `QPainter` + numpy (robusto su qualunque driver).
- Nessun asset esterno: icone disegnate a codice con `QPainter`; tema via stylesheet Qt (QSS). Nessun file immagine nel pacchetto
  salvo l'icona SVG del desktop entry in `packaging/`.
- Pacchetto `cncdeck/` con entry point `cncdeck = cncdeck.__main__:main` (`pyproject.toml`, setuptools). Versione in
  `cncdeck/__init__.py` (`__version__`) e in `pyproject.toml`, sempre uguali; la versione compare nel titolo finestra
  ("CNC Deck 0.5.0") e nella splash.
- Headless: tutti i test girano con `QT_QPA_PLATFORM=offscreen` (impostato in `tests/conftest.py`).
- Su Ubuntu servono i pacchetti di sistema `libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libdbus-1-3 libfontconfig1`.

## 3. Struttura del repository (ordine di costruzione consigliato)

```
cncdeck/
  __init__.py          __version__
  gcode.py             parser G-code → toolpath, tempi, avvisi, ripresa da riga          (§4)
  grbl.py              protocollo GRBL, thread seriale, streaming                       (§5)
  simulator.py         emulatore GRBL con API tipo pyserial                              (§6)
  theme.py             palette, QSS, icone vettoriali                                    (§7)
  viewer.py            viewer 3D QPainter                                                (§8)
  widgets.py           Card, StateBadge, AxisRow, JogPad, OverrideBox, CodeEditor, Console (§9)
  settings_dialog.py   editor dei `$` di GRBL                                            (§10)
  mainwindow.py        finestra principale, modalità Sender                              (§10)
  svgimport.py         SVG → polilinee in mm                                             (§11)
  cam.py               operazioni jscut + generazione G-code + linguette                 (§12)
  convertpage.py       pagina "Convertitore SVG"                                         (§12)
  splash.py            schermata iniziale                                                (§12)
  __main__.py          argparse, avvio, splash
tests/                 test_gcode, test_controller, test_viewer, test_svg_cam, test_ui   (§13)
examples/              demo.nc (+ make_demo.py), demo.svg, ellipse_cam.gcode (file reale da jscut, Y negativa)
docs/                  immagini del README + make_screenshots.py
packaging/             cncdeck.svg, cncdeck.desktop
install.sh  README.md  pyproject.toml  requirements.txt  .gitignore
```

## 4. `gcode.py` — parsing

**`clean_line(raw)`**: rimuove commenti `(...)` e `;...`, tutti gli spazi, converte in maiuscolo, toglie `/` iniziale; una riga che
inizia con `%` diventa vuota; toglie il numero di riga `N\d+` iniziale; se la riga **non** inizia con `$`: rimuove le parole `T\d+` e
trasforma `M6`/`M06` (non `M60`…) in `M0` (GRBL 1.1 rifiuta T/M6 con `error:20`; M0 = pausa per il cambio utensile manuale).

**`parse_program(text, rapid_rates=(500,500,500), default_feed=0) -> Program`**

- `Program`: `source` (righe originali), `commands` = lista `(indice_riga_sorgente, riga_pulita)` delle sole righe non vuote,
  `path` (`Toolpath`), `bounds_min/max` (3-tuple), `feed_length`, `rapid_length`, `line_time` (s per riga sorgente), `cum_time`
  (cumulato, lunghezza n+1), `max_spindle`, `warnings`; metodi/proprietà `total_time`, `size`, `time_remaining(riga)`.
- `Toolpath`: array numpy `start`, `end` (M×3 float32, **mm**), `kind` (uint8: `RAPID=0`, `FEED=1`), `line` (int32, riga sorgente;
  non decrescente).
- Stato modale: movimento G0/G1/G2/G3 (anche G38.2–38.5 come avanzamento), G4 (dwell `P` secondi → tempo), piani G17/18/19,
  unità G20/G21 (tutto convertito in mm; il feed in unità/min × 25.4 se pollici), G90/G91, `F`, `S` (massimo → `max_spindle`).
  G53/G28/G30/G10/G92/G54–59/G93/G94/G90.1/G91.1/G40/G49/G43.1/G80/G61 non producono movimento nel disegno e non danno avviso.
  Altri G → avviso "Riga N: G41 non supportato da GRBL 1.1" (una sola volta per codice).
- Posizione iniziale **assunta (0,0,0)** (la reale è ignota).
- **Archi** G2/G3 con `I J K` (formato centro) o `R`. Mappatura piano come GRBL: G17 assi (X,Y) lineare Z offset (I,J); G18 assi
  (Z,X) lineare Y offset (K,I); G19 assi (Y,Z) lineare X offset (J,K). Formato R con la formula di GRBL
  (`h_x2_div_d = -sqrt(4r²-x²-y²)/hypot(x,y)`, segni per CCW e R negativo). Angolo: CW se `travel >= -eps` → `-2π`, CCW se
  `travel <= eps` → `+2π`; inizio=fine con IJ ⇒ cerchio intero. Interpolazione lineare dell'asse libero (elica). Suddivisione
  con tolleranza 0.02 mm (`step = 2·acos(1 - tol/r)`, min 2, max 2000 segmenti), l'ultimo punto coincide esattamente col target.
- **Classificazione per il disegno**: G0 → RAPID. G1 → FEED, **tranne** (a) il **primo** movimento reale del programma
  (parte da posizione ignota) e (b) i G1 con **entrambi** gli estremi a Z > 0 (i CAM tipo jscut posizionano con G1 sopra il
  pezzo): quelli sono RAPID **solo a livello di disegno**; tempo e lunghezze restano quelli del comando (`motion == 0` ⇒ rapido).
- **Limiti (`bounds`)**: calcolati **solo sui segmenti FEED** (ingombro del pezzo, non dei movimenti in aria); se non ce ne sono,
  su tutti.
- **Tempi**: rapidi `max_i(|d_i| / rate_i) · 60` con le velocità massime `$110–112` (default 500); avanzamenti `dist/feed·60`;
  dwell sommato. Nessuna accelerazione (stima).
- **Avvisi** (testo italiano, prefisso "Riga N: "): G1/arco senza F; riga > 79 caratteri dopo la pulizia (limite GRBL 80);
  G non supportato; raggio arco non valido; **raggio quasi uguale a metà corda** (`4r²-d² < 0.01`: GRBL può rifiutarlo con
  `error:34`); M6 convertito in pausa M0.
- **`preamble_for(program, riga)`**: rigioca lo stato modale fino a `riga` e restituisce i comandi per riprendere in sicurezza:
  `G21|G20`, `G{piano}`, `G90`, `M3|M4 S..` se il mandrino era acceso, `G1F{feed}`, `G0Z{safe}` (safe = max Z visto + 5, o 5),
  `G0X..Y..`, `G1Z..`, e `G91` se era relativo.
- `format_duration(s)` → `mm:ss` o `h:mm:ss`.

## 5. `grbl.py` — protocollo e thread seriale

**Costanti**: `RX_BUFFER = 127`; `SIM_PORT = "simulator"`; byte real-time: `?` `!` `~` `0x18` (reset), `0x85` (jog cancel),
override avanzamento `0x90`(100%) `0x91`(+10) `0x92`(−10) `0x93`(+1) `0x94`(−1), rapidi `0x95/0x96/0x97` (100/50/25%), mandrino
`0x99`(100%) `0x9A/9B/9C/9D` (+10/−10/+1/−1), `0x9E` toggle stop mandrino. Tabelle `ERRORS` (1–38) e `ALARMS` (1–9) con testi
**italiani**; `SETTING_INFO` ($0–$132: descrizione italiana + unità).

**`Status`** (dataclass): `state` ("Idle", "Run", "Hold:0", …; "Disconnected"/"Connecting" lato UI), `mpos`, `wpos`, `wco`,
`feed`, `spindle`, `ov_feed/ov_rapid/ov_spindle`, `pins`, `planner_free`, `rx_free`, `accessories`, proprietà `base_state`
(prima dei `:`). **`parse_status(line, prev)`** analizza `<Idle|MPos:..|WPos:..|WCO:..|FS:..|F:..|Ov:..|Pn:..|Bf:..|A:..>`
portando avanti i campi assenti; se arriva MPos calcola `wpos = mpos − wco`, se arriva WPos calcola `mpos = wpos + wco`.

**`GrblConnection(QThread)`** — possiede la porta; tutti i metodi pubblici sono thread-safe (lock + code):
- Segnali: `connectionChanged(bool,str)`, `statusChanged(object)`, `console(str,str kind)` (kind: tx rx info error alarm msg),
  `lineAcked(int tag,bool ok,int code)`, `programState(str)` (running paused done aborted error), `settingsChanged(object)`
  (**`object`, non `dict`**: un `Signal(dict)` con chiavi int non è convertibile in C++), `parserState`, `probeResult(x,y,z,ok)`,
  `alarmRaised(int,str)`, `versionInfo`.
- API: `send_line(text, silent=False)`, `send_realtime(bytes)`, `start_program(commands)`, `pause()`, `resume()`, `stop()`,
  `reset()`, `disconnect_port()`, proprietà `settings`, `status`.
- **Apertura**: `serial.Serial(port, baud, timeout=0.005, write_timeout=1.0)`; per `SIM_PORT` usa `SimulatedSerial`.
  **Handshake**: le Arduino si resettano all'apertura → attendi il banner `Grbl ...`; a 0.8 s invia `?` (la scheda potrebbe già
  essere viva), a 2 s invia reset `0x18`, a 5 s errore "Nessuna risposta da GRBL". Poi accoda in silenzio `$I`, `$$`, `$G`.
- **Streaming a conteggio caratteri**: `rx_used = somma byte (riga+"\n") dei comandi in attesa di ack`; si invia una riga solo se
  `rx_used + n <= 127`. Le righe **manuali** (console, jog, zero…) hanno priorità su quelle del programma. Tag: `>=0` indice nel
  programma, `-1` manuale (eco nella console), `-2` interno silenzioso. Real-time (`?`, `!`…) non contano nel buffer.
- **Polling stato**: `?` ogni 0.2 s (0.1 s in Run/Jog/Home).
- **ok / error:N**: estrae il comando più vecchio in attesa. Su errore di una riga di **programma**: imposta `_error_hold`,
  invia `!` (feed hold), emette `programState("error")`; il programma riprende solo con `resume()`. Un `ok/error` senza
  comando in attesa è normale entro 1.5 s da un reset (non emettere l'errore "sincronizzazione persa" in quel caso).
- **Fine programma**: tutte le righe riconosciute **e** stato `Idle` **e** nessun comando pendente ⇒ `programState("done")`.
- **pause()**: `!` e smette di inviare righe di programma. **resume()**: `~`, azzera `_paused`/`_error_hold`.
- **stop()**: svuota code, invia `!`, e quando lo stato diventa `Hold:0` (o Idle/Alarm, o dopo 4 s) invia il **soft reset** `0x18`
  (la posizione resta valida: il reset da Hold non genera allarme); emette `aborted`. **reset()**: reset immediato.
- **Banner** `Grbl ...` (dopo reset): svuota i pendenti, annulla un programma in corso (`aborted`).
- **Impostazioni**: righe `$N=v` → cache; l'`ok` di `$$` emette `settingsChanged`; dopo ogni scrittura `$N=v` riuscita rilancia `$$`.
  `[PRB:x,y,z:1]` → `probeResult`; `ALARM:n` → `alarmRaised` + `aborted`; `[GC:..]` → `parserState`.
- Errori di porta (`SerialException`/`OSError`) → `connectionChanged(False, "Connessione persa…")` e chiusura pulita.
- In chiusura pulita, se in Run/Jog, invia `!` prima di chiudere la porta.

## 6. `simulator.py` — emulatore GRBL 1.1

`SimulatedSerial(speed=6.0, boot_delay=0.2)` con API tipo pyserial (`write`, `read(size)` con timeout, `in_waiting`, `close`,
`timeout`, `is_open`) e un thread interno. Deve modellare ciò che serve a un sender:
- buffer RX da 127 byte (contatori `overflows` e `max_rx_used`: i test verificano che lo streaming **non lo superi mai**);
  il byte è liberato quando la riga entra nel planner; planner da **15 blocchi**: `ok` solo quando la riga è accettata;
- banner `Grbl 1.1h ['$' for help]` dopo `boot_delay`; report di stato `<Stato|MPos:..|Bf:..|FS:..>` (con `WCO:` ogni 10 report,
  `Ov:` ogni 5); stati Idle/Run/Jog/Hold:0/Alarm/Home/Check;
- `!` `~` `0x18` (da movimento → ALARM:3) `0x85` (annulla il jog) e tutti gli override (`0x90–0x9E`);
- comandi: G0–G3 (archi trattati come moto verso il target), G4, G20/21, G90/91, G54–59, G10 L20, G92, G38.2 (contatto virtuale
  a Z macchina −18 → `[PRB:x,y,z:1]`, altrimenti ALARM:5), M0 (pausa: porta in `Hold:0` finché non arriva `~`), M3/M4/M5, `$J=`
  (errore 8 se non Idle, 22 senza F), `$$` (impostazioni di default tipiche 1610 Pro: `$110=2000 $111=2000 $112=800`
  `$130=160 $131=100 $132=45 $30=1000 $20=0 $22=0 …`), `$G`, `$I`, `$X`, `$H`, `$C`, `$N=v`;
- errori: `error:22` (G1/G2/G3 senza F), `error:20` (G non supportato), `error:34` (arco con raggio ≤ metà corda), `error:3`,
  `error:9` in allarme;
- `speed` = fattore di accelerazione del tempo simulato (i test lo alzano o abbassano a seconda se devono osservare stati).

## 7. `theme.py` — aspetto

Palette (hex): `BG #0e1217`, `PANEL #141a21`, `CARD #1a222c`, `CARD_HI #222c38`, `BORDER #2a3544`, `TEXT #d9e1ea`,
`MUTED #7f8d9f`, `ACCENT #2dd4bf`, `ACCENT_DIM #14766b`, `OK #34d399`, `RUN #4aa8ff`, `WARN #fbbf24`, `DANGER #f87171`,
assi `X #f87171`, `Y #4ade80`, `Z #60a5fa`. Font: sistema; monospazio `JetBrains Mono, Ubuntu Mono, DejaVu Sans Mono,
Liberation Mono, monospace`. Tema scuro con bordi arrotondati (card 10 px, pulsanti 7 px), pulsanti `#primary` (accento pieno),
`#danger`, `QToolButton#run` (verde) e `#stopb` (rosso) nella toolbar, `QToolButton#jog`, stile per QComboBox/QSpinBox/
QLineEdit/QSlider/QTabBar/QScrollBar/QProgressBar/QStatusBar/QToolBar/QMenu/**QListWidget** (selezione `ACCENT_DIM`,
indicatore di spunta a tema).
**Trappola QSS**: non usare un selettore discendente largo (`QFrame#card QWidget {background: transparent}`): per specificità
batte `QPushButton#primary` e rende trasparenti i pulsanti. Rendi trasparenti solo `QLabel/QCheckBox/QSlider` nelle card.
`STATE_COLORS` per il badge di stato. Icone vettoriali con `QPainter` (`icon(name, color)`; versione disabilitata grigia):
play pause stop open home unlock reset gear probe check up down left right target spindle save reload skip cube fit hold.

## 8. `viewer.py` — `ToolpathView(QWidget)` (QPainter + numpy, proiezione ortografica)

**Convenzione assi (VINCOLANTE, vedi §15)**: vista **destrogira**: Z verso l'alto sullo schermo; vista dall'alto con **X a
destra e Y verso l'alto**. Matrice: `q = Rx(pitch) · Rz(yaw) · (p − centro)` con
`Rz = [[cz,−sz,0],[sz,cz,0],[0,0,1]]` e `Rx = [[1,0,0],[0,cx,sx],[0,−sx,cx]]` (segno di `sx` fondamentale: con l'opposto Z va in
basso). Schermo: `x = W/2 + pan_x + qx·scala`, `y = H/2 + pan_y − qy·scala`.
- Viste: `iso (yaw −35, pitch 55)`, `top (0,0)`, `front (0,90)`, `right (−90,90)`, `left (90,90)`.
- **Orbita con mouse a "presa"** (`orbit(dx,dy)`): `yaw += dx·0.4`, `pitch = clamp(pitch − dy·0.4, 0, 180)`: trascinando a destra la
  superficie segue il cursore a destra; trascinando in basso il bordo vicino scende. Tasto destro/centrale = pan; rotella = zoom
  verso il cursore (`1.0015**delta`); doppio clic = inquadra tutto.
- Disegno: sfondo sfumato, griglia sul piano Z=0 (passo adattivo, assi con etichette), assi XYZ colorati all'origine, bounding box
  tratteggiato con quote, area macchina opzionale, **linee guida** (`set_guides`: polilinee (N,3) tenui, es. il disegno SVG),
  percorso: **FEED colorato per quota Z** (24 bucket, rampa arancio→giallo→verde→azzurro; legenda a destra), **RAPID tratteggiati**
  tenui (nascondibili), parte **già eseguita in grigio** (`set_progress_line(riga)` → indice via `searchsorted` su `path.line`),
  **marker utensile** = asta bianca + punta ciano con alone, ombra tratteggiata verso Z=0.
- Prestazioni: layer in cache (`QImage`) ricalcolato solo quando cambia la camera; durante l'interazione decima i segmenti
  (max ~25.000); layer "eseguito" incrementale. `set_program(prog, fit=True)`; se non c'è programma e ci sono guide, i limiti
  scena sono quelli delle guide; scena vuota: `lo=(−10,−50,−5) hi=(50,10,10)` (spazio verso −Y dove stanno i pezzi con Y negativa).
- `empty_message` personalizzabile (testo al centro quando non c'è percorso). HUD in basso: `X a…b Y c…d Z e…f mm`.
- Il **marker segue esattamente `WPos`** nelle stesse coordinate del percorso: nessuna inversione.
- `add_view_buttons(view)` (in coda al modulo): barra flottante 3D/Alto/Fronte/Lato/Adatta/Rapidi per la pagina convertitore.

## 9. `widgets.py`

`Card(titolo)` (QFrame `#card` con titolo maiuscolo 11 px spaziato); `StateBadge` (pillola colorata per stato, etichette italiane:
Non connesso/Connessione…/Pronto/In esecuzione/In pausa/Jog/ALLARME/Homing/Check mode/Porta aperta/Sleep); `AxisRow(asse)`:
etichetta colorata, **posizione di lavoro grande monospazio**, posizione macchina piccola ("M x.xxx"), pulsante `X0` (azzera) e
pulsante mirino (vai a 0); `JogPad`: croce XY (↑ ← → ↓ da 52 px) + colonna Z; segnali `pressed(dx,dy,dz)` e `released`; **freccia su =
(0,+1,0) = Y+, freccia giù = Y−**, tooltip "Y+ (↑)" / "Y− (↓)", Z "Pag↑/Pag↓"; `OverrideBox`/`RapidBox` (−10 −1 100 +1 +10 / 25 50 100);
`CodeEditor(QPlainTextEdit)` con numeri di riga a margine, evidenziazione sintattica G-code (G viola, M giallo, X/Y/Z nei colori
asse, IJKR azzurro, F arancio, S verde, commenti grigi corsivo, `$` giallo), riga in esecuzione evidenziata (`set_exec_line`),
righe con avviso in rosso, menu contestuale "Avvia da riga N…"; `Console` (log HTML colorato per tipo, max 3000 righe, campo
comando con cronologia ↑↓, pulsante Pulisci).

## 10. `mainwindow.py` — modalità Sender

**Struttura**: `QStackedWidget` centrale (pagina 0 = Sender, 1 = `ConverterPage`). Toolbar: a sinistra l'interruttore a due
segmenti "Sender CNC | Convertitore SVG"; poi (visibili **solo** in modalità Sender): Apri, Ricarica, Salva | Avvia, Pausa/Riprendi,
Stop | Home, Sblocca, Reset, Check, Tocca Z; in fondo sempre visibile: GRBL (impostazioni). Barra di stato: stato/porta/pin, buffer
(`Buffer: planner N rx N`), tempo (`trascorso / restano ≈`), barra di avanzamento 0–1000.

**Pagina Sender**: a sinistra colonna scorrevole (410–440 px) con card: **Connessione** (combo porta + baud
[115200 default, 57600, 38400, 19200, 9600, 250000], pulsante Connetti/Disconnetti, badge stato; elenco porte da
`serial.tools.list_ports`, prima `ttyUSB*/ttyACM*`, ultima voce "Simulatore GRBL (demo)"; rinfresco automatico ogni 3 s se non connesso);
**Posizione** (3 `AxisRow`, "Azzera tutto" = `G10L20P0X0Y0Z0`, "Vai a XY 0" = `G90G0X0Y0`, "Z sicura" = `G90G0Z{safeZ}`, riga
"Avanzamento … Mandrino …"); **Jog** (pad, passo [0.01, 0.1, 1 (default), 5, 10, 50 mm, Continuo], velocità spin default 800 mm/min
(max = `$110/$111`), casella "Tastiera: frecce XY, Pag↑/↓ Z" (solo in modalità Sender, ignorata se il focus è su campi di testo),
spin "Z sicura" default 5 mm); **Mandrino** (toggle `M3 S..`/`M5`, slider+spin S fino a `$30`); **Macro** (6 pulsanti,
tasto destro per definire nome e comandi, salvate in QSettings, più comandi separati da `;`); **Override** (avanzamento,
rapidi, mandrino). A destra: viewer 3D con barra viste + banner **allarme** rosso (Sblocca `$X` / Homing `$H`) + banner **errore
GRBL** ambra + tab **Programma** (riga riassuntiva: nome, n. comandi, ingombro, lavoro mm, tempo stimato, n. avvisi; editor) e
**Console**.

**Comportamenti**:
- **Jog**: passo → `$J=G91G21{assi}F{feed}` (silenzioso); per Z il feed è limitato a `$112`. **Continuo**: distanza = corsa massima
  dell'asse (`$130–132`, fallback 150/45 mm) e al rilascio `0x85`; se `$20` (soft limits) è attivo usa `$J=G53G21G90{asse}{target}`
  con target dentro il limite (−1 mm dal bordo, spazio macchina [−corsa, 0]). Mai con programma in esecuzione o stato diverso da Idle/Jog.
- **Zero**: `G10L20P0{asse}0` (persistente nel sistema di coordinate corrente). **Probe Z**: dialogo spessore piastrina (default 15 mm,
  ricordato) + conferma, poi `G91G38.2Z-30F25` e `G90`; su `[PRB:..:1]`: `G10L20P0Z{spessore}`, `G91G0Z{max(5,safeZ)}`, `G90`.
- **Avvio**: abilitato solo connessi, Idle/Check, programma caricato. Se ci sono avvisi chiede conferma. **Avvia da riga**: `preamble_for` +
  comandi dalla riga scelta. **Pausa**: `!`; **Riprendi** invia `~` (anche se GRBL è in Hold da un M0 del programma, non un secondo `!`).
  **Stop**: hold + reset. Programma in esecuzione: editor in sola lettura, jog/zero disabilitati.
- **Errori GRBL durante il programma**: **barra non modale** (mai `QMessageBox` bloccanti nello slot: uno per riga rifiutata si
  impilano e bloccano la finestra e la chiusura) con la **prima** riga rifiutata ("GRBL ha rifiutato la riga N: CMD → error:34 …
  Programma in pausa." + "(+K altre righe rifiutate)") e pulsanti Riprendi / Interrompi.
- **Avanzamento sul disegno**: GRBL manda `ok` quando la riga entra nel planner (fino a ~15 blocchi di anticipo): la riga "in esecuzione" si
  ricava agganciando la posizione reale (`wpos`) al **segmento più vicino** del percorso tra le ultime ~30 righe inviate (soglia 2 mm,
  a parità l'ultimo); aggiornamento UI ogni 80 ms; tempo residuo = `time_remaining(riga) · 100 / override avanzamento`.
- Il file si carica da dialogo, trascinamento o `load_file`; **`load_gcode_text(text, nome)`** carica da memoria (usato dal
  convertitore): `file_path=None`, titolo "nome — CNC Deck x.y.z", rifiutato se un programma è in esecuzione. Il parsing avviene in un
  thread (`ParseThread`); modifiche all'editor ri-analizzano dopo 700 ms. Avvisi nella console; avviso se il programma eccede la corsa `$130–132`.
- Impostazioni salvate con `QSettings("cncdeck","cncdeck")` (geometria, porta, baud, jog, Z sicura, S, macro, ultime cartelle…).
- `closeEvent`: se un programma è in esecuzione chiede conferma; salva le impostazioni; rimuove il filtro eventi dell'applicazione;
  `disconnect_port()` + `wait(3000)` e, se il thread non termina, `terminate()` (l'app deve **sempre** poter uscire).

**`settings_dialog.py`**: tabella `$ | Descrizione | Valore | Unità` (solo il valore è modificabile), "Rileggi", "Scrivi modifiche"
(invia solo i valori cambiati, con conferma). Un ricaricamento della tabella (risposta tardiva a `$$`) **conserva le celle già modificate**.

## 11. `svgimport.py`

`parse_svg(text, dpi=96.0, scale=1.0, tolerance=0.02) -> SvgDoc` con `svgelements.SVG.parse(reify=True, ppi=dpi)`: gestisce
trasformazioni annidate, viewBox, unità, `<use>`. Ogni forma (rect, cerchio, ellisse, linea, polilinea, poligono, path; archi
convertiti in cubiche) diventa `SvgShape(sid, label, kind, subpaths[Subpath(points Nx2 mm, closed)], fill_rule)`; sid = `id` o
`kind+N`, univoci. Coordinate in **mm con Y verso il basso** (origine angolo pagina SVG). Curve campionate a passo ≤ 0.25 mm
(min 8 punti per tratto curvo) e poi semplificate con **Douglas-Peucker** (tolleranza 0.02 mm, anelli chiusi mantenendo il primo
punto). Elementi `display:none`/`visibility:hidden` saltati; `<text>` ignorato con avviso ("convertilo in tracciati"); SVG non
valido → avviso e lista vuota. `SvgDoc.bounds(ids)`.
**Trappola prestazioni**: `seg.length()` di svgelements è un'integrazione numerica lentissima (~20 s per file): stima la
lunghezza campionando 16 punti.

## 12. Convertitore: `cam.py`, `convertpage.py`, `splash.py`

### 12.1 CAM (approccio jscut con pyclipper)
Coordinate Clipper intere con `SCALE = 10000` unità/mm; offset con `JT_ROUND`, `ArcTolerance 0.005 mm`, miter 2.
- `shape_polygons(shape)`: sottotracciati chiusi uniti con la regola di riempimento della forma (`evenodd`/`nonzero`).
- `combine(poligoni_per_forma, modo)`: unione (default), intersezione, differenza (la prima meno le altre), XOR.
- **outline(geometria, utensile, interno?, larghezza, step_over, climb)** (port di `Cam.outline`): offset ±utensile/2; `bounds` = banda
  tra gli offset (vuota con larghezza = utensile); anelli concentrici a passo `utensile·step_over`; poi `merge_paths`.
- **pocket**: offset interno utensile/2, poi passi `−utensile·step_over` fino a vuoto, anello più interno per primo.
- **engrave**: la linea centrale delle forme, anche aperta; chiusi: si ripassa il primo punto.
- **merge_paths(bounds, anelli)**: vicino-più-vicino; due anelli si uniscono con un collegamento rettilineo **solo se tutto il
  segmento sta dentro `bounds`** (campionato ogni 0.2 mm con `PointInPolygon`, pari/dispari). **Con `bounds` vuoto non si unisce mai**
  (non sostituire `bounds` con l'interno del pezzo: genererebbe traversate **attraverso il materiale**, bug realmente occorso).
- **Verso**: con mandrino orario (M3) "convenzionale" = **esterno in senso orario, interno/tasca antiorario**, "concorde" il contrario.
  Poiché Clipper lavora in orientamento SVG (Y giù) e l'output è specchiato, nel codice si **inverte** il flag `climb`: c'è un test
  parametrico sul segno dell'area del percorso.
- `Operation`: `name, kind(inside|outside|pocket|engrave), shapes(None=tutte), combine, climb, cut_depth, pass_depth, step_over(0.4),
  width(0=utensile), margin, ramp, tab_mode(none|auto|shapes), tab_count(4), tab_width(4), tab_height(1), tab_shapes`.
  `Job`: `tool_diameter 3.175, cut_feed 500, plunge_feed 150, safe_z 5, approach_z 1, spindle False, spindle_rpm 1000,
  units mm|in, origin top-left|bottom-left|svg, tolerance 0.01`. Default pensati per la 1610 Pro (profondità 1 mm, passata 0.5 mm).
- Funzioni di gestione di vuoti: input vuoto a Clipper (`Execute` con soggetto vuoto) solleva eccezione → protezione esplicita.
  Utensile troppo grande → avviso "l'utensile da X mm è troppo grande per questa forma"; tracciato aperto in Esterno/Interno/Tasca
  → ignorato con avviso (usare Incisione); nessuna forma selezionata → avviso.

### 12.2 Generazione G-code (formato esatto)
Convenzione **jscut**: `Y_gcode = −Y_svg`. Origine: `top-left` (default: angolo alto-sinistro del disegno → pezzo in **Y negativa**,
sotto l'origine, come i file reali), `bottom-left` (pezzo in Y positiva), `svg` (origine pagina). Z=0 = superficie del materiale,
tagli a Z negativa. Unità di output mm (`G21`) o pollici (`G20`, tutto diviso 25.4). Numeri con max 4 decimali senza zeri finali.

```
; CNC Deck - conversione SVG -> G-code (approccio jscut) - demo.svg
; Utensile 3.175 mm, avanzamento 500, tuffo 150 mm/min
G21
G90
G94
G0 Z5                       (+ "M3 S1000" solo se spindle=True)
; Operazione 1: Taglio (outside), profondita 3, passata 1.5
; Percorso 0 [(con linguette)]
G0 X72.2477 Y1.5837         spostamento rapido in XY a Z sicura
G0 Z1                       discesa rapida alla quota di avvicinamento (se < Z sicura)
G1 Z-1.5 F150               tuffo alla velocità di tuffo
G1 X73.376 Y1.4857 F500     taglio (F solo quando cambia: G94 modale)
G1 X74.0043 Y1.375
...
G0 Z5                       risalita a fine percorso
M2                          (preceduto da "M5" se spindle=True)
```
Regole: **rapidi sempre `G0`** (mai G1 con feed enormi), nessun punto duplicato (<1e-4 mm) o allineato (DP 0.01 mm), righe corte
(<40 caratteri), nessun G2/G3 (solo poligonali), nessun M6/T/`N`. Passate: profondità divisa per `pass_depth`
(`-0.4,-0.8,-1` ecc.). Percorsi chiusi: ogni passata fa un giro; **aperti**: avanti e indietro alternati senza risalire.
**Rampa** (jscut): in ogni passata Z scende linearmente lungo un giro; a profondità finale un giro piano (chiuso) o ritorno piano (aperto).
**Se nessun percorso viene generato il G-code è la stringa vuota** (mai un programma con la sola intestazione); `CamResult` ha
`gcode`, `guides` (contorni del disegno in coordinate G-code, Z=0), `warnings`, `n_paths`. Un'unica funzione interna `emit_pass`
produce tutte le passate (con/senza rampa, con/senza linguette): l'output senza linguette deve restare invariato.

### 12.3 Linguette di fissaggio (solo Esterno/Interno)
Lungo il taglio, sulle passate più profonde di `bottom + tab_height`, la fresa resta a `tab_z = −cut_depth + tab_height`.
`auto`: N linguette di larghezza W equidistanti (centri a `(k+0.5)·L/N`) sul **contorno di ogni pezzo**; i **fori** (percorsi con
profondità di annidamento dispari) non ne hanno. `shapes`: forme SVG marcate come linguetta (**non fresate**), ingrandite del raggio
utensile (un segnaposto centrato sul bordo copre il percorso, che corre a un raggio di distanza). Il percorso viene spezzato
esattamente dove entra/esce dalla linguetta (campionamento 0.2 mm + bisezione), i salti di quota sono **solo verticali** (alla
velocità di tuffo). Condizioni/avvisi: altezza ≥ profondità o ≤ 0 → ignorate con avviso; larghezza banda > utensile → avviso e ignorate;
contorno troppo corto (N·W > 0.5·L → N ridotto, se < 1 avviso); nessun marker/nessun contatto → avviso. Compatibili con la rampa.

### 12.4 `ConverterPage`
Colonna sinistra scorrevole (410–450 px): **File SVG** (Apri, nome/forme/dimensioni, risoluzione px/pollice default 96, scala %);
**Operazioni** (lista, ＋ Aggiungi, Duplica, Rimuovi, ▲, ▼; **la lista parte VUOTA** come in jscut e si può rimuovere anche l'ultima;
modulo: nome, tipo, combina, direzione, profondità, per passata, step over %, larghezza, margine, rampa, **linguette** [modalità,
numero, larghezza, altezza, elenco forme-segnaposto visibile solo in modalità "da forme"], elenco forme usate con caselle di spunta);
**Utensile e velocità** (Ø, avanzamento, tuffo, Z sicura, M3/M5 + S, unità G-code, posizione dello zero; ricordati in QSettings);
**G-code** (Genera, Salva .gcode…, **Invia alla CNC ➜**, riepilogo). A destra: anteprima 3D (con il **disegno originale** sotto il
percorso come guida) e tab "G-code generato" (sola lettura) / "Messaggi" (avvisi SVG, CAM, parser + riepilogo).
Comportamento: ogni modifica rigenera dopo 350 ms in un `QThread` (`GenThread`, risultati vecchi scartati via numero di serie) e
**disabilita Salva/Invia finché il G-code non è aggiornato** (anche premendo "Genera" a mano); senza operazioni mostra solo il disegno
con il messaggio "Aggiungi un'operazione…", G-code vuoto e pulsanti spenti; il segnale `sendRequested(gcode, nome)` è gestito dalla
finestra principale (`load_gcode_text` + passaggio alla modalità Sender + messaggio nella barra di stato).

### 12.5 `splash.py` e avvio
`SplashScreen(version, duration_ms=3000)`: finestra 780×440 senza bordi, `WA_TranslucentBackground`, angoli arrotondati, centrata, sempre
in primo piano; animazione a 60 fps con `QElapsedTimer`; dissolvenza in entrata (350 ms) e in uscita (ultimi 300 ms) via `setOpacity` del
painter (non `windowOpacity`); `finished` dopo esattamente la durata. Disegno: sfondo sfumato scuro con alone ciano, **griglia isometrica**
sfumata verso il testo, **percorso utensile a spirale "squircle"** (7.5 giri) che si traccia progressivamente, colorato per profondità
come il viewer (`depth_color`), contorno arancione, ombra tenue, **fresa** (asta bianca + punta ciano con alone) che segue la spirale, assi
XYZ in basso, **barra di avanzamento** sottile ciano→azzurro. Testo a sinistra: **"CNC"** (bianco) **"Deck"** (accento) grande e nero,
filetto con sfumatura, sotto in piccolo **"Stefano Ciurleo & Claude Code - 2026"**, poi **"Versione {x.y.z}"** in accento, e in basso una
scritta tenue "Sender GRBL · Convertitore SVG → G-code". `set_time(ms)` congela l'animazione (screenshot/test).
`__main__.main(argv)`: opzioni `file`, `--sim` (auto-connessione al simulatore), `--no-splash` (o env `CNCDECK_NO_SPLASH`),
`--screenshot PNG` (sviluppo: salva e chiude, senza splash). Con splash: la splash compare **subito**, la `MainWindow` si prepara dietro e
si mostra al segnale `finished`. `signal.SIGINT` → default; stylesheet applicato all'`QApplication`.

## 13. Test e criteri di accettazione (≈87 test, tutti verdi, ~2 minuti)

Esegui `python -m pytest -q`. Scrivi test che verifichino **comportamenti**, non implementazione; i principali:

- **gcode**: pulizia righe; bounds e modalità (G90/G91); unità pollici; archi IJ e R coerenti (G3 ⇒ stessa lunghezza), cerchio intero,
  elica; stima tempo (100 mm a F600 = 10 s); avvisi; mappatura comandi↔righe; preamble di ripresa; M6/T → M0; G1 sopra il pezzo e primo
  movimento come aria; ingombro solo da movimenti di lavoro (sul file reale `examples/ellipse_cam.gcode`).
- **controller (simulatore)**: connessione e `$$`; programma lungo (400 righe) **senza mai superare 127 byte**, tutti gli `ok`, posizione finale;
  jog + cancel; zero WCO; pausa/riprendi/stop (posizione conservata, nessun "sincronizzazione persa"); errore → hold.
- **viewer**: Z su in tutte le viste; vista dall'alto X destra/Y su; orbita "a presa" (punto vicino a destra col drag destro, giù col drag giù);
  il marker coincide con i punti del percorso (proiezione identica, viste top e iso) e Y+ lo muove verso l'alto.
- **svg/cam**: import (unità, trasformazioni, viewBox, dpi 96/72, curve/archi, testo, SVG rotto); offset esterno/interno esatti;
  copertura tasca ≤ raggio utensile; incisione avanti/indietro; fori con evenodd e **nessun taglio che attraversi il materiale**;
  combine unione/intersezione; direzioni di fresatura (6 casi); origini (3); G-code pulito per il sender (nessun avviso del parser, righe
  corte, niente G2/G3/M6/T, Z entro i limiti); passate; rampa; pollici = millimetri; mandrino opzionale; **nessun percorso ⇒ G-code vuoto**;
  linguette (lunghezza totale, distanza, passate alte senza linguette, salti solo verticali, modalità forme, fori esclusi, casi ignorati,
  rampa); **prestazioni** (parse SVG < 1 s, tasca 150×100 < 20 s).
- **UI (offscreen)**: switch di modalità; jog a passo e continuo con arresto al rilascio; frecce su = Y+; caricamento ed esecuzione
  fino alla fine; probe Z; scrittura `$110`; console; macro; pausa dopo M0; errore → barra non modale e uscita **senza blocchi** (anche
  in pausa per errore); convertitore con lista vuota e pulsanti spenti; aggiunta/rimozione operazione; invio al Sender **in memoria**
  (nessun file scritto, programma identico, nulla parte da solo); **G-code generato trasmesso al simulatore senza errori né overflow**
  e che termina a Z sicura; splash di 3 s con i testi richiesti; `--no-splash`.
- Nei test UI: chiudi sempre le finestre create (altrimenti il filtro eventi applicazione dà errori a fine test), usa `QApplication`
  (non `QCoreApplication`), simula le `QMessageBox`, accelera/rallenta il simulatore a seconda di cosa osservi.

**Verifica visiva obbligatoria**: genera screenshot con `widget.grab().save()` (offscreen) di Sender in esecuzione, convertitore con
operazioni e linguette, splash, dialogo impostazioni, e **guardali**: layout non tagliato, pulsanti con sfondo, testo leggibile.

## 14. Packaging, documentazione, rilascio

- `install.sh` (Ubuntu): `apt` per le librerie di sistema, venv in `~/.local/share/cncdeck/venv`, `pip install .`, collegamento
  `~/.local/bin/cncdeck`, `.desktop` e icona SVG in `~/.local/share`, aggiunge l'utente al gruppo `dialout` (serve logout) e propone di
  rimuovere `brltty` (ruba le schede CH340: la porta `/dev/ttyUSB0` sparisce).
- README (italiano): immagine splash in cima, sezione "Com'è fatto" con screenshot di Sender e Convertitore, installazione, porta
  seriale su Ubuntu, tabella funzioni, sezione Convertitore (con le ottimizzazioni rispetto a jscut e i non implementati), sviluppo,
  avvertenza di provare la macchina vera con cautela. Frase di compatibilità: **"Testato su Ubuntu 26.04 LTS."**
- `docs/make_screenshots.py` rigenera splash/sender/convertitore per il README.
- Git: branch `claude/main`; messaggi di commit chiari; **un commit per funzione**; mai lasciare test rossi nei commit.

## 15. Decisioni vincolanti (non cambiarle senza chiedere)

1. **Assi**: vista destrogira, Z su, dall'alto X destra e **Y su**; marker = `WPos` senza inversioni; **freccia su = Y+**, giù = Y−.
   (Esiste una storia di tentativi con viste specchiate e marker invertiti: tutti sbagliati. I file con Y negativa semplicemente
   compaiono **sotto** l'origine, che quindi sta in alto a sinistra del pezzo.) Nessun selettore di orientamento, nessuna opzione
   "inverti Y", nessun riferimento al "piatto" nell'interfaccia.
2. Il convertitore usa la convenzione jscut `Y = −Y_svg`, zero di default in alto a sinistra del disegno.
3. "Invia alla CNC" **non avvia** il lavoro. Il mandrino **non** si accende di default nel G-code generato.
4. Nessun dialogo modale per errori di streaming; nessun G-code "solo intestazione".
5. Lista operazioni del convertitore **vuota** all'avvio.
6. Streaming sempre a conteggio caratteri con buffer 127; stop = hold + reset; pausa = `!`; riprendi = `~`.

## 16. Trappole note (già incontrate)

- `Signal(dict)` con chiavi intere → errore di conversione: usa `Signal(object)`.
- Un `QMessageBox` per ogni riga rifiutata, aperto dentro uno slot, si impila e blocca finestra e chiusura.
- QSS: selettore discendente troppo generico rende trasparenti i pulsanti con `#id` (specificità).
- Segno di `sx` nella matrice di proiezione: sbagliato ⇒ Z in basso e scena vista "da sotto".
- `mergePaths` con `bounds` vuoto: non usare l'interno del pezzo come area sicura (traversata nel materiale).
- Clipper solleva eccezione con soggetto vuoto: proteggi `difference`/offset.
- `svgelements` `length()` lentissimo: stima per campionamento.
- Orientamento Clipper (Y giù) ⇒ verso di fresatura invertito: correggi e testa.
- Dopo un reset arrivano `ok` già in volo: non segnalarli come errore per 1.5 s.
- Tabella impostazioni: un ricaricamento tardivo cancellava i valori digitati.
- Thread seriale che non termina bloccava l'uscita: `terminate()` dopo il timeout.
- Nei test: un secondo `QApplication` nello stesso processo abortisce; `QCoreApplication` non basta per i widget; finestre non chiuse lasciano
  il filtro eventi attivo su widget distrutti.
- Comandi concatenati con `&&` e `| tail` non si fermano se i test falliscono: controlla l'esito dei test prima del commit.
- Arco con raggio == metà corda → `error:34` sulla scheda reale (arrotondamenti in virgola mobile): evita nei file demo e avvisa.

## 17. Non implementato (dichiaralo nel README, non inventarlo)

Height map / autolivellamento; V-pocket; Z origin dal fondo del materiale; conversione di testi SVG (vanno convertiti in tracciati);
adattamento a macOS/Windows (il progetto è testato solo su Ubuntu 26.04 LTS); ordinamento manuale dei percorsi dentro una stessa
operazione (l'ordine è tra le operazioni, con ▲/▼; dentro un'operazione è vicino-più-vicino).
