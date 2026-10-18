# CNC Deck

Sender G-code per CNC **GRBL 1.1** (pensato per la CNC 1610 Pro) con interfaccia scura e viewer 3D.
Python 3 + Qt6 (PySide6) + pyserial. Gira su Ubuntu 22.04/24.04.

## Installazione (Ubuntu)

```bash
./install.sh          # venv in ~/.local/share/cncdeck, launcher "cncdeck", voce nel menu
cncdeck               # oppure: cncdeck examples/demo.nc
cncdeck --sim         # prova tutto senza macchina (simulatore GRBL interno)
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
| Altro | homing, sblocco, reset, check mode, console con cronologia, editor dei `$$` con descrizioni, 6 macro personalizzabili, drag & drop del file, M6/T convertiti in pausa M0 per il cambio utensile manuale |

Non implementato (rispetto a Candle): **height map / autolivellamento**. Il viewer assume programmi in coordinate
assolute (G90) che partono dallo zero pezzo; un programma solo-G91 viene disegnato a partire da 0,0,0.

## Sviluppo

```bash
pip install -e .[dev]
python -m pytest            # 25 test: parser, protocollo/streaming contro il simulatore, UI offscreen
python examples/make_demo.py
```

`cncdeck/simulator.py` emula GRBL 1.1 (buffer RX da 127 byte, planner da 15 blocchi, hold/jog-cancel, WCO, probe,
override) ed è usato sia dalla modalità `--sim` sia dai test: i test verificano che lo streaming non superi
mai il buffer RX.

**Importante**: i test girano sul simulatore; il comportamento sulla scheda reale va verificato con cautela
(prima un giro a vuoto, o *Check mode*, senza fresa e con la mano sull'emergenza).
