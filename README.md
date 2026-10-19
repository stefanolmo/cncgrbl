# CNC Deck

Sender G-code per CNC **GRBL 1.1** (pensato per la CNC 1610 Pro) con interfaccia scura e viewer 3D.
Python 3 + Qt6 (PySide6) + pyserial. Gira su Ubuntu 22.04/24.04.

## Installazione (Ubuntu)

```bash
./install.sh          # venv in ~/.local/share/cncdeck, launcher "cncdeck", voce nel menu
cncdeck               # oppure: cncdeck examples/demo.nc
cncdeck --sim         # prova tutto senza macchina (simulatore GRBL interno)
cncdeck --no-splash   # senza la schermata iniziale di 3 secondi (o CNCDECK_NO_SPLASH=1)
```

Senza installare: `pip install -r requirements.txt && python3 -m cncdeck`.

**Porta seriale su Ubuntu** (cause classiche di "non vedo la porta"):
- l'utente deve stare nel gruppo `dialout`: `sudo usermod -aG dialout $USER`, poi logout/login;
- `brltty` ruba le schede CH340 (la 1610 Pro ne usa una): `sudo apt remove brltty`.

## Funzioni

| | |
|---|---|
| Streaming | protocollo *character-counting* (buffer RX 127 byte), senza pause tra le righe; pausa = feed hold `!`, stop = hold + soft reset (la posizione resta valida) |
| Viewer 3D | orbita (sx), pan (dx/centrale), zoom (rotella), doppio clic = inquadra; colore per quota Z, rapidi tratteggiati, parte già eseguita in grigio, utensile in tempo reale, ingombro, area macchina ($130-132) |
| Programma | editor con evidenziazione, riga in esecuzione, stima del tempo (anche residuo, corretta per l'override), avvisi (G1 senza F, righe > 80 caratteri, G-code non supportati, corsa superata) |
| Riprendi | tasto destro su una riga → *Avvia da riga…* ripristina unità/avanzamento/mandrino, sale, va in XY e scende |
| DRO | coordinate di lavoro e macchina, azzeramento per asse (`G10 L20 P0`, persistente), vai a zero, Z sicura |
| Jog | passo 0.01–50 mm o continuo (si ferma subito al rilascio con jog-cancel), tastiera opzionale (frecce, Pag↑/↓) |
| Probe Z | discesa lenta `G38.2` su piastrina di tocco, azzera Z con lo spessore indicato e risale |
| Override | avanzamento, rapidi, mandrino (comandi real-time) |
| Mandrino | M3/M5 con S regolabile |
| Convertitore SVG | modalità «Convertitore SVG» (interruttore in alto a sinistra): carica un `.svg`, scegli operazioni e utensile, anteprima 3D live, **Salva .gcode** oppure **Invia alla CNC ➜** (carica il G-code nel Sender senza passare dal disco). Vedi sotto |
| Altro | homing, sblocco, reset, check mode, console con cronologia, editor dei `$$` con descrizioni, 6 macro personalizzabili, drag & drop del file, M6/T convertiti in pausa M0 per il cambio utensile manuale |

## Convertitore SVG → G-code

Approccio di [jscut](https://github.com/tbfleming/jscut), riscritto in Python con `pyclipper` (binding di Clipper, la
stessa libreria geometrica di jscut) e `svgelements` (trasformazioni, unità, viewBox):

- **Operazioni** (si parte con la lista vuota, come in jscut: le crei tu con «＋ Aggiungi»; anche più d'una, in sequenza): *Esterno*, *Interno*, *Tasca*, *Incisione* (linea centrale, anche tracciati aperti);
  per ognuna: forme usate, combinazione (unione / intersezione / differenza / XOR), direzione (convenzionale / concorde, per mandrino
  orario), profondità totale e per passata, step over, larghezza della fascia, margine, entrata a rampa.
- **Utensile**: diametro, avanzamento di taglio e di tuffo, Z di sicurezza, M3/M5 opzionali, unità del G-code (mm o pollici), posizione dello zero.
- **Convenzione Y come jscut**: Y_gcode = −Y_svg. Con «angolo alto-sinistro del disegno» (predefinito) lo zero sta in alto a sinistra del
  pezzo e il lavoro si sviluppa in Y negativa, esattamente come i file che già usi. Il tuo zero pezzo deve coincidere con quel punto.
- **Ottimizzazioni rispetto all'output jscut**: spostamenti con `G0` (non `G1` con feed enormi), nessun punto duplicato o allineato,
  avanzamento scritto solo quando cambia, righe corte, intestazione `G21 G90 G94`, fine con `G0` a Z sicura e `M2`. Il G-code generato
  passa dallo stesso parser e dallo stesso streaming del Sender: un test lo trasmette al simulatore GRBL senza errori.
- Il mandrino **non** viene acceso di default (come jscut): usa il pulsante Mandrino del Sender oppure attiva M3/M5 nel convertitore.
- «Invia alla CNC» **non avvia** il lavoro: porta il programma nel Sender e passa a quella modalità; controlla lo zero e premi Avvia.

Non implementati rispetto a jscut: *V-pocket*, *tabs* di fissaggio, Z origin dal fondo del materiale, conversione da testo SVG (converti in tracciati).

Non implementato (rispetto a Candle): **height map / autolivellamento**. Il viewer assume programmi in coordinate
assolute (G90) che partono dallo zero pezzo; un programma solo-G91 viene disegnato a partire da 0,0,0.

## Sviluppo

```bash
pip install -e .[dev]
python -m pytest            # test: parser, protocollo/streaming contro il simulatore, conversione SVG/CAM, UI offscreen
python examples/make_demo.py
```

`cncdeck/simulator.py` emula GRBL 1.1 (buffer RX da 127 byte, planner da 15 blocchi, hold/jog-cancel, WCO, probe,
override) ed è usato sia dalla modalità `--sim` sia dai test: i test verificano che lo streaming non superi
mai il buffer RX.

**Importante**: i test girano sul simulatore; il comportamento sulla scheda reale va verificato con cautela
(prima un giro a vuoto, o *Check mode*, senza fresa e con la mano sull'emergenza).
