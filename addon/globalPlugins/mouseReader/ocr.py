# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""Recognising a window, and the snapshot that answers the mouse afterwards.

Capture. NVDA's own OCR photographs the screen, and with full-screen Magnifier "the screen"
is the zoomed-in view, so the picture and the pointer do not line up and most of the window
is missing. Here the window itself is asked to render into a picture (PrintWindow with
PW_RENDERFULLCONTENT), which gives the whole window at its real size whatever Magnifier is
doing. The screen is photographed only if the window refuses.

Result. Windows OCR returns lines of words with their positions. Lines are grouped into
paragraphs by the vertical gaps between them and their horizontal overlap (so a sidebar and
a message list stay apart). The snapshot then answers the mouse: while it is fresh, pointing
at a spot in that window reads the recognised paragraph there, once per paragraph, and NVDA's
own mouse tracking stays out of that window; the wheel recognises the window again once it
is still.

Speaking a paragraph goes through NVDA's SpeechWithoutPauses, so the voice receives sentence
sized pieces (one huge utterance is what froze the 32-bit voice bridge) while the paragraph
still sounds like one continuous read.
"""

import ctypes
import time
from ctypes import byref
from ctypes.wintypes import POINT, RECT

import addonHandler
import queueHandler
import speech
import ui
import winGDI
import winUser
import wx
from logHandler import log
from winBindings import gdi32, user32

try:
	addonHandler.initTranslation()
except Exception:
	pass

PW_RENDERFULLCONTENT = 0x00000002
SNAPSHOT_LIFETIME_SECONDS = 180
# After the wheel stops turning over a recognised window, recognise it again this much later.
WHEEL_RERECOGNIZE_MS = 500
# Consecutive lines whose tops are further apart than this many typical line pitches start a
# new paragraph (1 = normal spacing; a blank line between messages is about 2).
PARAGRAPH_BREAK_FACTOR = 1.55

_PrintWindow = ctypes.windll.user32.PrintWindow
_PrintWindow.restype = ctypes.c_int
_PrintWindow.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)


class _WindowImageInfo:
	"""What the recogniser needs to know about the picture: its size, and how to turn picture
	coordinates into screen coordinates. Unlike NVDA's RecogImageInfo this allows a window
	that starts left of or above the primary monitor (negative coordinates)."""

	def __init__(self, screenLeft, screenTop, width, height):
		self.screenLeft = screenLeft
		self.screenTop = screenTop
		self.screenWidth = width
		self.screenHeight = height
		self.recogWidth = width
		self.recogHeight = height

	def convertXToScreen(self, x):
		return self.screenLeft + x

	def convertYToScreen(self, y):
		return self.screenTop + y

	def convertWidthToScreen(self, width):
		return width

	def convertHeightToScreen(self, height):
		return height


def windowAt(x: int, y: int):
	"""(hwnd, (left, top, width, height)) of the top-level window under the point, or None."""
	try:
		hwnd = user32.WindowFromPoint(POINT(x, y))
		if hwnd:
			hwnd = user32.GetAncestor(hwnd, winUser.GA_ROOT) or hwnd
	except Exception:
		log.debugWarning("mouseReader: WindowFromPoint failed", exc_info=True)
		return None
	if not hwnd:
		return None
	r = RECT()
	if not user32.GetWindowRect(hwnd, byref(r)):
		return None
	width, height = r.right - r.left, r.bottom - r.top
	if width < 8 or height < 8:
		return None
	return hwnd, (r.left, r.top, width, height)


def captureWindow(hwnd, rect):
	"""The window's pixels as the RGBQUAD array Windows OCR expects, at the window's real size."""
	left, top, width, height = rect
	screenDC = user32.GetDC(0)
	memDC = gdi32.CreateCompatibleDC(screenDC)
	bitmap = gdi32.CreateCompatibleBitmap(screenDC, width, height)
	oldBitmap = gdi32.SelectObject(memDC, bitmap)
	try:
		rendered = False
		try:
			rendered = bool(_PrintWindow(hwnd, memDC, PW_RENDERFULLCONTENT))
		except Exception:
			log.debugWarning("mouseReader: PrintWindow failed", exc_info=True)
		if not rendered:
			log.info("mouseReader: PrintWindow refused; photographing the screen instead")
			gdi32.StretchBlt(memDC, 0, 0, width, height, screenDC, left, top, width, height, winGDI.SRCCOPY)
		bmInfo = gdi32.BITMAPINFO()
		bmInfo.bmiHeader.biSize = ctypes.sizeof(bmInfo)
		bmInfo.bmiHeader.biWidth = width
		bmInfo.bmiHeader.biHeight = -height
		bmInfo.bmiHeader.biPlanes = 1
		bmInfo.bmiHeader.biBitCount = 32
		bmInfo.bmiHeader.biCompression = winGDI.BI_RGB
		buffer = (gdi32.RGBQUAD * width * height)()
		gdi32.GetDIBits(memDC, bitmap, 0, height, buffer, byref(bmInfo), winGDI.DIB_RGB_COLORS)
		return buffer, rendered
	finally:
		gdi32.SelectObject(memDC, oldBitmap)
		gdi32.DeleteObject(bitmap)
		gdi32.DeleteDC(memDC)
		user32.ReleaseDC(0, screenDC)


class Paragraph:
	__slots__ = ("lines", "words", "left", "top", "right", "bottom")

	def __init__(self, lines):
		self.lines = lines  # lists of word dicts, one per OCR line, in reading order
		self.words = [w for line in lines for w in line]  # picture coordinates
		self.left = min(w["x"] for w in self.words)
		self.top = min(w["y"] for w in self.words)
		self.right = max(w["x"] + w["width"] for w in self.words)
		self.bottom = max(w["y"] + w["height"] for w in self.words)

	@property
	def text(self):
		return " ".join(w["text"] for w in self.words)


def _overlap(aLeft, aRight, bLeft, bRight):
	return max(0, min(aRight, bRight) - max(aLeft, bLeft))


def groupParagraphs(data):
	"""Windows OCR lines (lists of word dicts, picture coordinates) -> list of Paragraph, in
	reading order.

	A line joins the paragraph directly above it when the vertical gap is small *and* the two
	overlap horizontally; a sidebar and a message list at the same heights therefore stay
	apart. Paragraphs are then ordered column by column (left to right), top to bottom within
	a column.
	"""
	lines = []
	for words in data:
		words = [w for w in words if w.get("text")]
		if not words:
			continue
		words = sorted(words, key=lambda w: w["x"])
		top = min(w["y"] for w in words)
		bottom = max(w["y"] + w["height"] for w in words)
		left = words[0]["x"]
		right = max(w["x"] + w["width"] for w in words)
		lines.append({"top": top, "bottom": bottom, "left": left, "right": right, "words": words})
	if not lines:
		return []
	lines.sort(key=lambda line: (line["top"], line["left"]))
	heights = sorted(line["bottom"] - line["top"] for line in lines)
	typical = heights[len(heights) // 2] or 1
	# The typical line pitch (top to top of consecutive, horizontally overlapping lines) is a
	# steadier yardstick than glyph height, which changes with ascenders and descenders.
	pitches = []
	for i in range(len(lines) - 1):
		a, b = lines[i], lines[i + 1]
		if _overlap(a["left"], a["right"], b["left"], b["right"]) > 0 and 0 < b["top"] - a["top"] < typical * 3:
			pitches.append(b["top"] - a["top"])
	pitch = sorted(pitches)[len(pitches) // 2] if pitches else typical * 1.5
	# In a chat made of one-line messages most pairs are message gaps, which would inflate the
	# estimate; normal line spacing is never much more than 1.7 glyph heights.
	pitch = min(pitch, typical * 1.7)
	breakPitch = pitch * PARAGRAPH_BREAK_FACTOR

	paragraphs = []
	for line in lines:
		best = None
		bestOverlap = 0
		for p in paragraphs:
			if line["top"] - p["lastTop"] > breakPitch or line["top"] < p["top"]:
				continue
			overlap = _overlap(line["left"], line["right"], p["left"], p["right"])
			if overlap > bestOverlap:
				best, bestOverlap = p, overlap
		if best is None:
			paragraphs.append(dict(line, lines=[line["words"]], lastTop=line["top"]))
			continue
		best["lines"].append(line["words"])
		best["lastTop"] = max(best["lastTop"], line["top"])
		best["bottom"] = max(best["bottom"], line["bottom"])
		best["left"] = min(best["left"], line["left"])
		best["right"] = max(best["right"], line["right"])

	columns = []
	for p in sorted(paragraphs, key=lambda p: p["left"]):
		for column in columns:
			width = min(p["right"] - p["left"], column["right"] - column["left"]) or 1
			if _overlap(p["left"], p["right"], column["left"], column["right"]) >= width * 0.5:
				column["items"].append(p)
				column["left"] = min(column["left"], p["left"])
				column["right"] = max(column["right"], p["right"])
				break
		else:
			columns.append({"left": p["left"], "right": p["right"], "items": [p]})
	columns.sort(key=lambda c: c["left"])
	ordered = []
	for column in columns:
		ordered.extend(sorted(column["items"], key=lambda p: (p["top"], p["left"])))
	return [Paragraph(p["lines"]) for p in ordered]


def speakParagraph(paragraph):
	"""Speak a whole paragraph, handed to the voice in sentence-sized pieces."""
	from speech.speechWithoutPauses import SpeechWithoutPauses

	speech.cancelSpeech()
	reader = SpeechWithoutPauses(speakFunc=speech.speak)
	for line in paragraph.lines:
		reader.speakWithoutPauses([" ".join(w["text"] for w in line) + " "])
	reader.speakWithoutPauses(None)  # flush whatever is left


class Snapshot:
	"""One recognised window: its paragraphs, and which one was read last."""

	def __init__(self, hwnd, rect, paragraphs):
		self.hwnd = hwnd
		self.rect = rect  # screen: left, top, width, height
		self.paragraphs = paragraphs
		self.created = time.time()
		self._lastSpoken = None

	def isFresh(self) -> bool:
		return time.time() - self.created < SNAPSHOT_LIFETIME_SECONDS

	def contains(self, x: int, y: int) -> bool:
		left, top, width, height = self.rect
		return left <= x < left + width and top <= y < top + height

	def covers(self, x: int, y: int) -> bool:
		"""Is the point inside this snapshot *and* is its window still the one under the point?
		Two maximised windows share the same rectangle, so the rectangle alone is not enough:
		a snapshot of VS Code must not answer for Slack."""
		if not self.contains(x, y):
			return False
		found = windowAt(x, y)
		return found is not None and found[0] == self.hwnd

	def _toPicture(self, x, y):
		return x - self.rect[0], y - self.rect[1]

	def paragraphAt(self, x: int, y: int):
		"""Index of the paragraph whose box contains the point, or None."""
		px, py = self._toPicture(x, y)
		for i, p in enumerate(self.paragraphs):
			if p.left <= px < p.right and p.top <= py < p.bottom:
				return i
		return None

	def nearestParagraph(self, x: int, y: int):
		"""For a click: the paragraph under the point, else the nearest one vertically."""
		found = self.paragraphAt(x, y)
		if found is not None:
			return found
		if not self.paragraphs:
			return None
		px, py = self._toPicture(x, y)

		def distance(p):
			if p.top <= py < p.bottom:
				return 0
			return min(abs(py - p.top), abs(py - p.bottom))

		return min(range(len(self.paragraphs)), key=lambda i: distance(self.paragraphs[i]))

	def speakIndex(self, index):
		self._lastSpoken = index
		speakParagraph(self.paragraphs[index])

	def hover(self, x: int, y: int) -> bool:
		"""Read the paragraph under the point when it is a different one from the last paragraph
		read. Blank space and the paragraph just read leave things alone (so panning Magnifier
		does not repeat it). Returns True when something was read."""
		index = self.paragraphAt(x, y)
		if index is None or index == self._lastSpoken:
			return False
		self.speakIndex(index)
		return True


def buildSnapshot(hwnd, rect, data):
	paragraphs = groupParagraphs(data)
	if not paragraphs:
		return None
	return Snapshot(hwnd, rect, paragraphs)


class OcrReader:
	"""Runs the recognition for a click or the wheel; keeps the snapshot for hovering."""

	def __init__(self):
		self.snapshot = None
		self._pending = None  # recognizer of a recognition still in flight
		self._wheelTimer = None
		self._wheelPos = None

	def shutdown(self):
		if self._wheelTimer is not None:
			try:
				self._wheelTimer.Stop()
			except Exception:
				pass
			self._wheelTimer = None
		if self._pending is not None:
			try:
				self._pending.cancel()
			except Exception:
				pass
			self._pending = None

	def start(self, x: int, y: int, quiet: bool = False) -> bool:
		"""Begin recognising the window under the point. Returns False if there is no window.
		Once recognised, the paragraph under the point is read. quiet: a refresh after
		scrolling, with no "Recognizing" announcement."""
		try:
			from contentRecog import uwpOcr
		except Exception:
			log.debugWarning("mouseReader: OCR modules unavailable", exc_info=True)
			# Translators: message when Windows OCR cannot be used.
			ui.message(_("OCR is not available"))
			return True
		found = windowAt(x, y)
		if found is None:
			return False
		hwnd, rect = found
		try:
			recognizer = uwpOcr.UwpOcr()
		except Exception:
			log.debugWarning("mouseReader: could not create the OCR recognizer", exc_info=True)
			ui.message(_("OCR is not available"))
			return True
		if self._pending is not None:
			try:
				self._pending.cancel()  # a second request before the first came back
			except Exception:
				pass
		started = time.time()
		try:
			pixels, rendered = captureWindow(hwnd, rect)
		except Exception:
			log.exception("mouseReader: could not capture the window")
			return False
		captureMs = int((time.time() - started) * 1000)
		left, top, width, height = rect
		imgInfo = _WindowImageInfo(left, top, width, height)
		if not quiet:
			# Translators: reported while the window under the mouse is being recognised.
			ui.message(_("Recognizing"))
		log.info(
			"mouseReader: OCR of window %s at %r (%s, captured in %d ms)"
			% (hwnd, rect, "rendered by the window" if rendered else "screen photo", captureMs)
		)
		self._pending = recognizer
		sent = time.time()

		def onResult(result):
			# Recogniser thread: hand over to the main thread.
			log.info("mouseReader: OCR engine answered after %d ms" % int((time.time() - sent) * 1000))
			queueHandler.queueFunction(queueHandler.eventQueue, self._onResult, recognizer, hwnd, rect, result, x, y, quiet)

		try:
			recognizer.recognize(pixels, imgInfo, onResult)
		except Exception:
			log.exception("mouseReader: OCR failed to start")
			self._pending = None
			ui.message(_("OCR is not available"))
		return True

	def _onResult(self, recognizer, hwnd, rect, result, x, y, quiet):
		if self._pending is not recognizer:
			return  # superseded by a later request
		self._pending = None
		if isinstance(result, Exception):
			log.error("mouseReader: recognition failed: %s" % result)
			if not quiet:
				# Translators: message when Windows OCR fails.
				ui.message(_("Recognition failed"))
			return
		try:
			snapshot = buildSnapshot(hwnd, rect, result.data)
		except Exception:
			log.exception("mouseReader: could not build the snapshot")
			snapshot = None
		if snapshot is None:
			if not quiet:
				# Translators: message when OCR found no text in the window under the mouse.
				ui.message(_("No text recognized"))
			return
		self.snapshot = snapshot
		log.info(
			"mouseReader: OCR found %d paragraphs (%d lines)"
			% (len(snapshot.paragraphs), sum(len(p.lines) for p in snapshot.paragraphs))
		)
		# Read what is under the pointer now (for a click, the clicked spot; after scrolling,
		# wherever the pointer is): the nearest paragraph for a click, only an exact hit after a scroll.
		if quiet:
			cx, cy = winUser.getCursorPos()
			if snapshot.covers(cx, cy):
				snapshot.hover(cx, cy)
			return
		index = snapshot.nearestParagraph(x, y)
		if index is None:
			ui.message(_("No text recognized"))
			return
		snapshot.speakIndex(index)

	# ---- hover ---------------------------------------------------------------------------

	def claim(self, x: int, y: int) -> bool:
		"""Called for every mouse move NVDA reports. True when a fresh snapshot covers the point;
		the paragraph there is read if it is not the one read last."""
		snapshot = self.snapshot
		if snapshot is None:
			return False
		if not snapshot.isFresh():
			self.snapshot = None
			return False
		if not snapshot.covers(x, y):
			return False
		snapshot.hover(x, y)
		return True

	# ---- the wheel -----------------------------------------------------------------------

	def wheelScrolled(self, x: int, y: int):
		"""Main thread. The wheel turned at the point; if that is over the recognised window,
		recognise it again once the wheel has been quiet for a moment."""
		snapshot = self.snapshot
		if snapshot is None or not snapshot.contains(x, y):
			return
		found = windowAt(x, y)
		if found is None or found[0] != snapshot.hwnd:
			return
		self._wheelPos = (x, y)
		if self._wheelTimer is None:
			self._wheelTimer = wx.CallLater(WHEEL_RERECOGNIZE_MS, self._reRecognize)
		else:
			self._wheelTimer.Start(WHEEL_RERECOGNIZE_MS)

	def _reRecognize(self):
		pos = self._wheelPos
		snapshot = self.snapshot
		if pos is None or snapshot is None:
			return
		found = windowAt(*pos)
		if found is None or found[0] != snapshot.hwnd:
			return
		log.info("mouseReader: wheel stopped; recognising the window again")
		self.start(pos[0], pos[1], quiet=True)
