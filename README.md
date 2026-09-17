# Mouse Reader for NVDA

An NVDA add-on by [Carrie on Accessibility](https://apps.carrieonaccessibility.com), for the places where NVDA's mouse tracking goes silent or stumbles: apps that cannot say what is at a point on the screen (Slack and other Electron apps, pictures of text), and PDFs whose font makes a mess of OCR.

Hold NVDA+control and click (or press NVDA+control+enter over the window) and the paragraph under the pointer is read; for three minutes afterwards, hovering over that window reads the paragraph under the pointer. On a web page or a PDF in Chrome or Edge the text comes from NVDA's own copy of the page (exact words, list items one by one, the browse cursor follows the click); everywhere else, and on pages whose text has no paragraphs, the window draws itself into a picture (PrintWindow, so it works under full-screen Magnifier and covers the whole window) and Windows' built-in OCR reads it, with paragraphs formed from line spacing, column by column, and bullet dots found in the picture. Scrolling the wheel recognises an OCR window again half a second after the wheel stops. Needs NVDA 2026.1 or later.

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
  - `ocr.py` - PrintWindow(PW_RENDERFULLCONTENT) capture of the top-level window, Windows OCR through `contentRecog.uwpOcr`, bullet dots found in the picture (`findBullets`), same-row fragments merged, lines grouped into paragraphs (line-pitch rule + horizontal overlap, list items, sentence and short-line rules, ordered column by column), the `Snapshot` (paragraph boxes in screen coordinates, window identity check, hover with no-repeat), speaking through `SpeechWithoutPauses` so the voice gets sentence-sized pieces, the wheel's re-recognition timer, and the order of sources (a document first in Automatic, OCR otherwise; windows whose text has no paragraphs go to OCR for ten minutes).
  - `document.py` - a browse-mode document under the mouse, read from NVDA's own buffer: the element under the pointer (Chromium's hit test stops at the box round a PDF, so the add-on walks down by the children's rectangles), the buffer paragraph it belongs to (a paragraph element read whole; a bulleted or numbered one split into items picked by the pointer's height), remembered boxes for cheap hovering, the browse caret moved to the click, Say All through the document, and NVDA asked to build the buffer when it has none yet. Windows NVDA reads through UI Automation are left to OCR.
  - `hook.py` - a wrapper around `winInputHook`'s mouse callback: returning False from it swallows the click; wheel messages are reported.
- `build.py` - `python build.py` writes `dist/mouseReader-<version>.nvda-addon`. `python build.py --install` also copies the add-on into `%APPDATA%\nvda\addons\` for testing (restart NVDA after).
- `dev/layout_test.py [git ref]` - replays synthetic layouts and every real capture in `dev/captures/*.json` through the OCR paragraph rules of a git ref (main by default) and of the working copy, side by side. Captures come from the add-on itself with the hidden setting `debugDump = True` in the `[mouseReader]` section of nvda.ini (they land in `%TEMP%\mouseReader-last-ocr.json`).

### Diagnosing

The add-on logs at info level; in NVDA's log (NVDA+F1) look for lines starting `mouseReader:`: the click; `document under the mouse (ChromeVBuf); reading from it, no OCR` or `no document under the mouse ...; using OCR`; `NVDA reads the window under the mouse through UIA ...; using OCR` (with a `why UIA:` line naming NVDA's reasons); `the page's text has no paragraph structure here; using OCR for this window`; `click landed on <element> -> <text>`; `walked down from ...` (the first two per click); `OCR of window ... captured in N ms`, `OCR engine answered after N ms`, `N bullet dots found in the picture`, `OCR found N lines, N paragraphs, N blocks`; `wheel stopped; recognising the window again`; and any lookup or speech call slower than 150 ms.

### History

0.1.0 bundled a mouse tracking delay and an automatic lookup ladder (UI Automation and display model queries); the ladder blocked NVDA's main thread in browsers and the delay became its own add-on. 0.2 and 0.3 read continuously with NVDA's Say All from the click point, with skip commands; whole-paragraph utterances froze the 32-bit voice bridge, and the continuous reading overlapped NVDA's own OCR, so 0.4 is recognition and hover only. 0.5 added hover levels, list items, and the site release.

0.6.0 reads a document's own text before OCR. Found on the way: Chromium's hit test never enters the PDF viewer's box (so the add-on walks down by rectangles); a PDF paragraph is one text element with line breaks inside, and a whole list can be one such element (so paragraph elements are read whole and split at bullet lines); NVDA falls back to UI Automation for a Chromium browser it could not hook into, which is slow and coarse (such windows go to OCR, and the readme says to exit and reopen the browser); Windows OCR reports no bullet dots at all and reads an avatar as a capital O (so dots are found in the picture and a capital O is not a bullet); Windows OCR returns mixed-font lines in fragments (merged by row before any rule runs). `dev/layout_test.py` replays real captures so rule changes are measured, not hoped for.
