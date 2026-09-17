# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""OCR for Read from here, and the snapshot it leaves behind for hovering.

Capture. NVDA's own OCR photographs the screen, and with full-screen Magnifier "the screen"
is the zoomed-in view, so the picture and the click do not line up and most of the window is
missing. Here the window itself is asked to render into a picture (PrintWindow with
PW_RENDERFULLCONTENT), which gives the whole window at its real size whatever Magnifier is
doing. The screen is photographed only if the window refuses.

Result. Windows OCR returns lines of words with their positions. Lines are grouped into
paragraphs by the vertical gaps between them, and the paragraphs become the "lines" of an
NVDA recognition result. That result is wrapped in NVDA's recognition-result object, but the
object is never given focus: reading uses NVDA's Say All from the review cursor placed on it,
so any key stops the reading and there is nothing to close afterwards. The same snapshot then
serves the hover: while it is fresh, pointing at a spot in that window where NVDA itself
finds no text reads the recognised paragraph there, once per paragraph.
"""

import ctypes
import time
from ctypes import byref
from ctypes.wintypes import RECT

import api
import queueHandler
import speech
import textInfos
import textInfos.offsets
import ui
import winGDI
from logHandler import log
from speech import sayAll
from winBindings import gdi32, user32

PW_RENDERFULLCONTENT = 0x00000002
SNAPSHOT_LIFETIME_SECONDS = 180
# A gap between two OCR lines larger than this fraction of the typical line height starts a
# new paragraph.
PARAGRAPH_GAP_FACTOR = 0.6

_PrintWindow = ctypes.windll.user32.PrintWindow
_PrintWindow.restype = ctypes.c_int
_PrintWindow.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)


class _WindowImageInfo:
	"""What a recogniser needs to know about the picture: its size, and how to turn picture
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
	from ctypes.wintypes import POINT

	try:
		hwnd = user32.WindowFromPoint(POINT(x, y))
		if hwnd:
			hwnd = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
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
	__slots__ = ("words", "left", "top", "right", "bottom", "offset")

	def __init__(self, words):
		self.words = words  # word dicts in reading order, picture coordinates
		self.left = min(w["x"] for w in words)
		self.top = min(w["y"] for w in words)
		self.right = max(w["x"] + w["width"] for w in words)
		self.bottom = max(w["y"] + w["height"] for w in words)
		self.offset = 0  # start offset in the result text, set once the result is built

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
	a column, so "read onward" walks down the messages instead of hopping across to the sidebar.
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
	gap = typical * PARAGRAPH_GAP_FACTOR

	# Group lines into paragraphs (each a dict with the running box and its words).
	paragraphs = []
	for line in lines:
		best = None
		bestOverlap = 0
		for p in paragraphs:
			if line["top"] - p["bottom"] > gap or line["top"] < p["top"]:
				continue
			overlap = _overlap(line["left"], line["right"], p["left"], p["right"])
			if overlap > bestOverlap:
				best, bestOverlap = p, overlap
		if best is None:
			paragraphs.append(dict(line, words=list(line["words"])))
			continue
		best["words"].extend(line["words"])
		best["bottom"] = max(best["bottom"], line["bottom"])
		best["left"] = min(best["left"], line["left"])
		best["right"] = max(best["right"], line["right"])

	# Columns: paragraphs whose horizontal ranges mostly overlap.
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
	return [Paragraph(p["words"]) for p in ordered]


class Snapshot:
	"""One OCRed window: its paragraphs, and a recognition result object to read them with."""

	def __init__(self, hwnd, rect, paragraphs, result, doc):
		self.hwnd = hwnd
		self.rect = rect  # screen: left, top, width, height
		self.paragraphs = paragraphs
		self.result = result
		self.doc = doc
		self.created = time.time()
		self._lastHovered = None

	def isFresh(self) -> bool:
		return time.time() - self.created < SNAPSHOT_LIFETIME_SECONDS

	def contains(self, x: int, y: int) -> bool:
		left, top, width, height = self.rect
		return left <= x < left + width and top <= y < top + height

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

	def wordOffsetAt(self, x: int, y: int, paragraphIndex: int) -> int:
		"""Offset of the word under the point within the paragraph, else the paragraph start."""
		px, py = self._toPicture(x, y)
		paragraph = self.paragraphs[paragraphIndex]
		offset = paragraph.offset
		for w in paragraph.words:
			if w["x"] <= px < w["x"] + w["width"] and w["y"] <= py < w["y"] + w["height"]:
				return offset
			offset += len(w["text"]) + 1
		return paragraph.offset

	# ---- hover ---------------------------------------------------------------------------

	def hover(self, x: int, y: int) -> bool:
		"""Read the paragraph under the point when it is a different one from the last paragraph
		visited. Blank space and the paragraph already being read leave things alone, so panning
		Magnifier to follow a long message does not cut it off; entering another paragraph stops
		whatever is being read (speech.cancelSpeech ends Say All too) and reads that one.
		Returns True when something was read."""
		index = self.paragraphAt(x, y)
		if index is None or index == self._lastHovered:
			return False
		self._lastHovered = index
		speech.cancelSpeech()
		speech.speakText(self.paragraphs[index].text)
		return True

	def markCurrent(self, index):
		"""The paragraph reading starts from counts as visited, so moving inside it is quiet."""
		self._lastHovered = index


def buildSnapshot(hwnd, rect, data, imgInfo):
	"""Turn Windows OCR output into a Snapshot whose result has one line per paragraph."""
	from contentRecog import LinesWordsResult, recogUi

	paragraphs = groupParagraphs(data)
	if not paragraphs:
		return None
	result = LinesWordsResult([p.words for p in paragraphs], imgInfo)
	offset = 0
	for p in paragraphs:
		p.offset = offset
		offset += len(p.text) + 1  # the newline LinesWordsResult adds after each line
	# NVDA's own recognition-result object, never focused: it only gives Say All a text to read.
	doc = recogUi.RecogResultNVDAObject(result=result)
	return Snapshot(hwnd, rect, paragraphs, result, doc)


class OcrReader:
	"""Runs the OCR for a click and starts reading; keeps the snapshot for hovering."""

	def __init__(self, owner):
		self._owner = owner  # ReadFromHere: provides start unit and the session mode
		self.snapshot = None
		self._pending = None  # recognizer of an OCR still in flight

	def start(self, x: int, y: int, startUnit, readAfter: bool = True) -> bool:
		"""Begin OCR of the window under the point. Returns False if there is nothing to OCR.
		readAfter: start reading from the paragraph nearest the point once recognised;
		otherwise just announce the result and leave the snapshot for hovering."""
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
				self._pending.cancel()  # a second click before the first OCR came back
			except Exception:
				pass
		try:
			pixels, rendered = captureWindow(hwnd, rect)
		except Exception:
			log.exception("mouseReader: could not capture the window")
			return False
		left, top, width, height = rect
		imgInfo = _WindowImageInfo(left, top, width, height)
		# Translators: reported while the window under the mouse is being OCRed.
		ui.message(_("Recognizing"))
		log.info(
			"mouseReader: OCR of window %s at %r (%s)" % (hwnd, rect, "rendered by the window" if rendered else "screen photo")
		)
		self._pending = recognizer

		def onResult(result):
			# Recogniser thread: hand over to the main thread.
			queueHandler.queueFunction(queueHandler.eventQueue, self._onResult, recognizer, hwnd, rect, result, imgInfo, x, y, startUnit, readAfter)

		try:
			recognizer.recognize(pixels, imgInfo, onResult)
		except Exception:
			log.exception("mouseReader: OCR failed to start")
			self._pending = None
			ui.message(_("OCR is not available"))
		return True

	def _onResult(self, recognizer, hwnd, rect, result, imgInfo, x, y, startUnit, readAfter):
		if self._pending is not recognizer:
			return  # superseded by a later click
		self._pending = None
		if isinstance(result, Exception):
			log.error("mouseReader: recognition failed: %s" % result)
			# Translators: message when Windows OCR fails.
			ui.message(_("Recognition failed"))
			return
		try:
			snapshot = buildSnapshot(hwnd, rect, result.data, imgInfo)
		except Exception:
			log.exception("mouseReader: could not build the OCR snapshot")
			snapshot = None
		if snapshot is None:
			# Translators: message when OCR found no text in the window under the mouse.
			ui.message(_("No text recognized"))
			return
		self.snapshot = snapshot
		log.info("mouseReader: OCR found %d paragraphs" % len(snapshot.paragraphs))
		if not readAfter:
			# Translators: reported after a window has been recognised; {count} paragraphs were found.
			ui.message(_("Recognized {count} paragraphs; hover to read them").format(count=len(snapshot.paragraphs)))
			return
		if not self.readFromSnapshot(x, y, startUnit):
			ui.message(_("No text recognized"))

	def textInfoAt(self, x: int, y: int, startUnit):
		"""A TextInfo on the snapshot's result at the paragraph (or word) under the point."""
		snapshot = self.snapshot
		if snapshot is None:
			return None
		index = snapshot.nearestParagraph(x, y)
		if index is None:
			return None
		if startUnit in (textInfos.UNIT_WORD, None):
			offset = snapshot.wordOffsetAt(x, y, index)
		else:
			offset = snapshot.paragraphs[index].offset
		try:
			return snapshot.doc.makeTextInfo(textInfos.offsets.Offsets(offset, offset))
		except Exception:
			log.debugWarning("mouseReader: could not place a cursor in the OCR result", exc_info=True)
			return None

	def readFromSnapshot(self, x: int, y: int, startUnit) -> bool:
		"""Say All over the snapshot from the paragraph nearest the point."""
		info = self.textInfoAt(x, y, startUnit)
		if info is None:
			return False
		index = self.snapshot.nearestParagraph(x, y)
		log.info("mouseReader: reading the OCR result from paragraph %d" % (index + 1))
		self.snapshot.markCurrent(index)
		try:
			if not api.setReviewPosition(info, clearNavigatorObject=True):
				return False
			self._owner.sessionStarted(sayAll.CURSOR.REVIEW)
			sayAll.SayAllHandler.readText(sayAll.CURSOR.REVIEW, startedFromScript=True)
			return True
		except Exception:
			log.exception("mouseReader: could not start reading the OCR result")
			return False

	# ---- hover ---------------------------------------------------------------------------

	def hover(self, x: int, y: int) -> bool:
		"""Called for every mouse move NVDA reports. True if the snapshot spoke for this spot."""
		snapshot = self.snapshot
		if snapshot is None:
			return False
		if not snapshot.isFresh():
			self.snapshot = None
			return False
		if not snapshot.contains(x, y):
			return False
		return snapshot.hover(x, y)

	def forget(self):
		self.snapshot = None
