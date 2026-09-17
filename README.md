# Mouse Reader for NVDA

An NVDA add-on by [Carrie on Accessibility](https://apps.carrieonaccessibility.com).

**Read from here.** Hold NVDA+control and click in a piece of text and NVDA reads on from there until you press a key (Windows Magnifier's read-aloud, in NVDA). Start at the beginning of the paragraph, line or word, or the exact spot. NVDA+alt+comma / NVDA+alt+period skip back / forward a paragraph while it reads. NVDA+shift+R reads from the mouse position without a click. If nothing under the click has text (Slack's message list, a picture of text), the window is recognised with Windows' built-in OCR and reading starts from the recognised paragraph nearest the pointer, with nothing opened; for a few minutes afterwards, hovering over that window reads the recognised paragraph under the pointer. The window draws itself for the picture (PrintWindow), so it works under full-screen Magnifier. Needs NVDA 2026.1 or later.

The delay before mouse tracking reads is a separate add-on, [Mouse Echo Delay](../mouse-echo-delay-for-nvda/).

## Install

Download the latest `mouseReader-<version>.nvda-addon` from the Releases page and open it; NVDA asks to install it.

## Licence

Copyright (C) 2026 Carrie on Accessibility. Free software under the GNU General Public License, version 2 (the same licence as NVDA) - see [LICENSE](LICENSE).

## Development notes

### Layout

- `addon/` - the add-on itself (what gets zipped). `manifest.ini` + `globalPlugins/mouseReader/` + `doc/en/readme.html`.
  - `__init__.py` - NVDA plumbing: config spec, the Settings category, the commands, and wiring the click to the mouse hook.
  - `readfrom.py` - Read from here: click detection (NVDA tracks held modifiers itself in `keyboardHandler.currentModifiers`, because the NVDA key never reaches Windows), the starting-point lookup (the same object-at-point and text-at-point calls NVDA's mouse tracking makes), positioning into the browse mode document where there is one, NVDA's Say All from the review cursor, skip back/forward (stop, move the review cursor a paragraph, read again), and the OCR fallback (a subclass of NVDA's own OCR result document that opens at the line under the click).
  - `ocr.py` - the OCR fallback: PrintWindow capture of the top-level window, Windows OCR through `contentRecog.uwpOcr`, lines grouped into paragraphs (vertical gap + horizontal overlap, ordered column by column), wrapped in NVDA's `RecogResultNVDAObject` without ever focusing it so Say All from the review cursor can read it; the snapshot answers `event_mouseMove` for three minutes.
  - `hook.py` - a wrapper around `winInputHook`'s mouse callback; returning False from it swallows the click so the app never sees it.
- `build.py` - `python build.py` writes `dist/mouseReader-<version>.nvda-addon`. `python build.py --install` also copies the add-on into `%APPDATA%\nvda\addons\` for testing (restart NVDA after).

### Diagnosing "the click does nothing"

The add-on logs at info level. In NVDA's log (NVDA+F1) look for lines starting `mouseReader:`:
- `NVDA+control+click at (x, y)` - the hook saw the click with both modifiers held. If this line is missing, the modifiers were not detected (or the click trigger is off in settings). NVDA+shift+R bypasses the hook entirely and is the quickest way to tell click detection from everything else.
- `read from (x, y): object ..., text at the point | from its start | none` - what the lookup found. "none" leads to OCR (if enabled) or "No text under the mouse".
- If both lines are present and nothing is heard, check for synth errors (`result expired`) in the same log: when the voice bridge is wedged, Say All starts but cannot speak.

### History

0.1.0 bundled a mouse tracking delay and an automatic "lookup ladder" (UI Automation and display model queries when NVDA found no text under the pointer). The ladder blocked NVDA's main thread in browsers, so it was removed; the delay became its own add-on. The ladder may return later, inside Read from here only, and never for Chromium windows.
