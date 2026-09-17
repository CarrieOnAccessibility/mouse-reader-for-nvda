# Mouse Reader for NVDA

An NVDA add-on by [Carrie on Accessibility](https://apps.carrieonaccessibility.com) for people who find their way around the screen with the mouse.

- **A delay before reading.** NVDA's mouse tracking reads whatever passes under the pointer the instant it passes. Mouse Reader waits until the mouse has stopped (100 ms to 2 s on a slider, or any custom value), so sweeping across the screen is quiet and only the spot you settle on is read. Everything else about mouse tracking is untouched: no repeats inside a paragraph, reading continues over a blank spot, NVDA's text unit setting still applies. An optional "stop speech when the mouse moves" check box for those who want it.
- **Read from here.** Hold NVDA+control and click in a piece of text and NVDA reads on from there until you press a key (Windows Magnifier's read-aloud, in NVDA). Start at the beginning of the paragraph, line or word, or the exact spot. NVDA+alt+comma / NVDA+alt+period skip back / forward a paragraph while it reads. NVDA+shift+R reads from the mouse position without a click.
- **When NVDA finds no text under the pointer**, a lookup ladder tries harder: UI Automation straight from the screen point (fixes Chromium, Electron and WinUI apps), then NVDA's display model. Each rung may only report text that sits *under* the pointer, never text nearby, so blank spots stay blank. Read from here adds one more rung: Windows OCR of the window under the click, reading from the recognised line nearest the pointer.
- Needs NVDA 2026.1 or later.

NV Access has an open feature request for the delay ([nvaccess/nvda#19372](https://github.com/nvaccess/nvda/issues/19372)). Until it lands in NVDA itself, this add-on fills the gap.

## Install

Download the latest `mouseReader-<version>.nvda-addon` from the Releases page and open it; NVDA asks to install it.

## Licence

Copyright (C) 2026 Carrie on Accessibility. Free software under the GNU General Public License, version 2 (the same licence as NVDA) - see [LICENSE](LICENSE).

## Development notes

### Layout

- `addon/` - the add-on itself (what gets zipped). `manifest.ini` + `globalPlugins/mouseReader/` + `doc/en/readme.html` (the help page NVDA opens from the Add-on Store).
  - `__init__.py` - NVDA plumbing: config spec, the Settings category, the commands, and wiring the two features to the one mouse hook.
  - `dwell.py` - the delay. Replaces `mouseHandler.executeMouseMoveEvent` with a debounce that calls NVDA's original once the mouse has been still; then runs the ladder if NVDA had nothing to read. Also "stop speech when the mouse moves".
  - `ladder.py` - the lookup ladder: `nvdaFoundText` mirrors `NVDAObject.event_mouseMove` to decide whether NVDA had text; `uiaTextAt` (rung 2) and `displayModelTextAt` (rung 3) must prove the point is inside what they found. `objectAndTextInfoAt` is the Read from here starting-point lookup.
  - `readfrom.py` - Read from here: the click detection (NVDA tracks held modifiers itself; `keyboardHandler.currentModifiers`), positioning (into the browse mode document where there is one), NVDA's Say All from the review cursor, skip back/forward, and the OCR fallback (a subclass of NVDA's own OCR result document that opens at the line under the click).
  - `hook.py` - one wrapper around `winInputHook`'s mouse callback, shared by both features; returning False from it swallows a click.
- `build.py` - `python build.py` writes `dist/mouseReader-<version>.nvda-addon`. `python build.py --install` also copies the add-on into `%APPDATA%\nvda\addons\` for testing (restart NVDA after).

### How the delay keeps NVDA's behaviour

`mouseHandler.pumpAll` calls `executeMouseMoveEvent(x, y)` with the latest pointer position once per NVDA core cycle. The replacement only records the position and restarts a `wx.CallLater`; when it fires, NVDA's original runs with the final position. What NVDA reads, and when it chooses to stay quiet (same chunk, blank text), is untouched, just later. With the delay off (or 0) the original runs at once.

### Testing

The delay and the ladder need the mouse. Things to try after `python build.py --install` and an NVDA restart:

- Sweep across a web page: silent until you stop; then the paragraph. Move inside it: no repeat. Move onto the margin: it keeps reading.
- VS Code or Windows Terminal with stock NVDA vs with the add-on: the ladder's rung 2 should find the text.
- NVDA+control+click in a Word document, a web page, and Notepad: reading continues past the clicked paragraph; comma/period skip.
- NVDA+control+click on an image of text (a screenshot): "Recognizing", then reading from the nearest line; Escape leaves the OCR document.
- Check the NVDA log (NVDA+F1) for lines starting `mouseReader:`; there should be none at warning level.
