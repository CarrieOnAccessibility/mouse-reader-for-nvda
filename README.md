# Mouse Reader for NVDA

An NVDA add-on by [Carrie on Accessibility](https://apps.carrieonaccessibility.com), for the apps where NVDA's mouse tracking goes silent because the app cannot say what is at a point on the screen (Slack and other Electron apps, pictures of text).

Hold NVDA+control and click (or press NVDA+control+enter over the window): the window is recognised with Windows' built-in OCR and the paragraph under the pointer is read. For three minutes afterwards, hovering over that window reads the recognised paragraph under the pointer; scrolling the wheel recognises it again half a second after the wheel stops. The window draws itself for the picture (PrintWindow), so it works under full-screen Magnifier and covers the whole window. Paragraphs are formed from line spacing, column by column, so a sidebar and a message list stay apart. Needs NVDA 2026.1 or later.

The delay before mouse tracking reads is a separate add-on, [Mouse Echo Delay](../mouse-echo-delay-for-nvda/).

## Install

Download the latest `mouseReader-<version>.nvda-addon` from the Releases page and open it; NVDA asks to install it.

## Licence

Copyright (C) 2026 Carrie on Accessibility. Free software under the GNU General Public License, version 2 (the same licence as NVDA) - see [LICENSE](LICENSE).

## Development notes

### Layout

- `addon/` - the add-on itself (what gets zipped). `manifest.ini` + `globalPlugins/mouseReader/` + `doc/en/readme.html`.
  - `__init__.py` - NVDA plumbing: config, the Settings category, the command, the mouse hook wiring, and `event_mouseMove` (inside a recognised window the snapshot answers and NVDA's own tracking is not called).
  - `reader.py` - the triggers: click detection (NVDA tracks held modifiers itself in `keyboardHandler.currentModifiers`, because the NVDA key never reaches Windows; control is also checked with Windows), the wheel, the keyboard command, and the hover hand-off.
  - `ocr.py` - PrintWindow(PW_RENDERFULLCONTENT) capture of the top-level window, Windows OCR through `contentRecog.uwpOcr`, lines grouped into paragraphs (line-pitch rule + horizontal overlap, ordered column by column), the `Snapshot` (paragraph boxes in screen coordinates, window identity check, hover with no-repeat), speaking a paragraph through `SpeechWithoutPauses` so the voice gets sentence-sized pieces, and the wheel's re-recognition timer.
  - `hook.py` - a wrapper around `winInputHook`'s mouse callback: returning False from it swallows the click; wheel messages are reported.
- `build.py` - `python build.py` writes `dist/mouseReader-<version>.nvda-addon`. `python build.py --install` also copies the add-on into `%APPDATA%\nvda\addons\` for testing (restart NVDA after).

### Diagnosing

The add-on logs at info level; in NVDA's log (NVDA+F1) look for lines starting `mouseReader:`: the click, `OCR of window ... captured in N ms`, `OCR engine answered after N ms`, `OCR found N paragraphs (N lines)`, `wheel stopped; recognising the window again`.

### History

0.1.0 bundled a mouse tracking delay and an automatic lookup ladder (UI Automation and display model queries); the ladder blocked NVDA's main thread in browsers and the delay became its own add-on. 0.2 and 0.3 read continuously with NVDA's Say All from the click point, with skip commands; whole-paragraph utterances froze the 32-bit voice bridge, and the continuous reading overlapped NVDA's own OCR, so 0.4 is recognition and hover only.
