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

Reading on ("read all"): the recognised lines, in reading order, are wrapped in NVDA's own
recognition-result object, never focused, and NVDA's Say All reads it from the review cursor
placed at the unit under the pointer. Say All stops on any key press (NVDA's rule) and the
add-on stops it on a click; the mouse is ignored while it reads, and the wheel does not stop it.

Documents first. When the click lands in a browse-mode document (a web page, a PDF in Chrome
or Edge) NVDA already holds the exact text, so no picture is taken: document.py answers the
mouse from the document itself, through the same snapshot shape. OCR is for everything else.
"""

import ctypes
import re
import time
from ctypes import byref
from ctypes.wintypes import POINT, RECT

import addonHandler
import api
import queueHandler
import speech
import textInfos.offsets
import tones
import ui
import winGDI
import winUser
import wx
from logHandler import log
from speech import sayAll
from winBindings import gdi32, user32

try:
	addonHandler.initTranslation()
except Exception:
	pass

LEVEL_LINE = "line"
LEVEL_PARAGRAPH = "paragraph"
LEVEL_BLOCK = "block"
LEVELS = (LEVEL_LINE, LEVEL_PARAGRAPH, LEVEL_BLOCK)

# Where the text comes from: the page's own text when it has paragraphs, else OCR; or OCR only.
SOURCE_AUTO = "auto"
SOURCE_OCR = "ocr"
SOURCES = (SOURCE_AUTO, SOURCE_OCR)
# A window whose text turned out to have no paragraph structure is read with OCR for this long
# before its text is tried again.
UNSTRUCTURED_MEMORY_SECONDS = 600

PW_RENDERFULLCONTENT = 0x00000002
SNAPSHOT_LIFETIME_SECONDS = 180
# After the wheel stops turning over a recognised window, recognise it again this much later.
WHEEL_RERECOGNIZE_MS = 500
# While NVDA builds its copy of the page under the mouse: how often to look, and for how long
# before OCR takes over.
DOCUMENT_POLL_MS = 150
DOCUMENT_WAIT_MS = 4000
# The short soft beep that marks a window being recognised with OCR (when the setting is on):
# pitch, length, and volume out of 100 (NVDA's own beeps are 50).
OCR_BEEP_HZ = 660
OCR_BEEP_MS = 60
OCR_BEEP_VOLUME = 35


def ocrBeep():
	try:
		tones.beep(OCR_BEEP_HZ, OCR_BEEP_MS, OCR_BEEP_VOLUME, OCR_BEEP_VOLUME)
	except Exception:
		pass
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


# A line that starts with one of these, or with a numbering like "1." "2)" "(3)" "a." "iv.",
# is a list item and starts a paragraph of its own.
_BULLETS = frozenset("•·▪▫◦‣⁃●○■□◆◇➢➤►▶-–—*»>.°º")  # "." and "°": what OCR makes of a bullet dot
# A line ending before this fraction of its column's width is the last line of something (a
# list item, a short paragraph); the next line starts a new paragraph. Wrapped lines fill the
# width, so they are left alone.
SHORT_LINE_FRACTION = 0.75
# Two OCR lines are pieces of one row (an italic run, a bold word, a font change: Windows OCR
# often returns such a line in fragments) when their boxes overlap vertically by this share
# of the shorter one, are of similar height (the taller at most this many times the shorter:
# a column of bullet dots comes back as one tall "line", and must not swallow the rows beside
# it), and the horizontal gap between them is at most this many glyph heights. Columns stand
# further apart than that.
ROW_OVERLAP_FRACTION = 0.5
ROW_HEIGHT_RATIO = 2.5
ROW_GAP_HEIGHTS = 1.5
# An OCR "line" whose box is taller than this many times its typical word height is words
# stacked on several rows (that column of dots): each word becomes a line of its own.
STACKED_LINE_FACTOR = 1.8
# A line reaching this fraction of its column's width is a full line: a sentence ending there,
# followed by a capital on the next line at normal line pitch (within this factor of it), is a
# sentence boundary inside a paragraph, not a paragraph break. Separate chat messages sit
# further apart than the lines within one.
FULL_LINE_FRACTION = 0.97
SENTENCE_NEAR_PITCH_FACTOR = 1.15
_NUMBERING = re.compile(r"^\(?(\d{1,3}|[a-zA-Z]|[ivxlcIVXLC]{1,5})[.)]$")
# A line ending like this ends a sentence; if the next line then starts like a new sentence
# (capital, digit, opening quote or bracket) the line break is a paragraph break.
_SENTENCE_END = re.compile(r"[.!?:;…]['\"\u201d\u2019)\]]*$")
_SENTENCE_START = frozenset("\"\u201c\u2018'([")


# A line ending before this fraction of its column's width, when the next line starts like a
# sentence, is the end of a list item or paragraph even without punctuation: wrapped text
# fills the width and carries on in lowercase. A line that reaches this fraction is a full
# line: in book text a sentence ends and the next begins, capital and all, in the middle of a
# paragraph, so a full line ending a sentence goes on with its paragraph.
CAPITAL_AFTER_SHORT_FRACTION = 0.9


def startsItem(words) -> bool:
	first = words[0]["text"]
	if first in _BULLETS or bool(_NUMBERING.match(first)):
		return True
	if len(first) > 1 and first[0] in _BULLETS and first[1:2].isalnum():
		return True  # the bullet glued onto the first word: "•Restore"
	# What OCR makes of a hollow or small bullet, when the line goes on: "o Bring questions".
	return first in ("o", "O", "0") and len(words) > 1


def _startsSentence(words) -> bool:
	ch = words[0]["text"][0]
	return ch.isupper() or ch.isdigit() or ch in _SENTENCE_START


def breaksAfter(previousWords, words, previousShort=False, previousFull=False) -> bool:
	"""Should the line `words` start a new paragraph rather than join the one ending with
	`previousWords`? True for a list item; for a sentence start after a line that ended short
	of the width (previousShort); and for a sentence end followed by a sentence start, unless
	the line that ended the sentence is a full one (previousFull): book text ends a sentence
	and starts the next, capital and all, at a line start in the middle of a paragraph."""
	if startsItem(words):
		return True
	if not _startsSentence(words):
		return False
	if previousShort:
		return True
	if previousFull:
		return False
	last = previousWords[-1]["text"]
	return bool(_SENTENCE_END.search(last))


def _lineOfWords(words):
	return {
		"top": min(w["y"] for w in words),
		"bottom": max(w["y"] + w["height"] for w in words),
		"left": min(w["x"] for w in words),
		"right": max(w["x"] + w["width"] for w in words),
		"words": sorted(words, key=lambda w: w["x"]),
	}


def unstackLines(lines):
	"""An OCR "line" much taller than its words is words stacked on several rows (a column of
	bullet dots): each word becomes a line of its own."""
	out = []
	for line in lines:
		heights = sorted(w["height"] for w in line["words"])
		typical = heights[len(heights) // 2] or 1
		if len(line["words"]) > 1 and line["bottom"] - line["top"] > STACKED_LINE_FACTOR * typical:
			out.extend(_lineOfWords([w]) for w in line["words"])
		else:
			out.append(line)
	return out


def mergeRows(lines):
	"""Join OCR lines that are fragments of one row (see ROW_OVERLAP_FRACTION): a paragraph
	rule looking at a fragment ending in a full stop, or at a fragment's short right edge,
	would break where nothing breaks. Lines are dicts with top/bottom/left/right/words."""
	merged = []
	for line in sorted(unstackLines(lines), key=lambda l: (l["top"], l["left"])):
		height = line["bottom"] - line["top"]
		for row in merged:
			rowHeight = row["bottom"] - row["top"]
			if max(height, rowHeight) > ROW_HEIGHT_RATIO * max(min(height, rowHeight), 1):
				continue
			overlap = min(line["bottom"], row["bottom"]) - max(line["top"], row["top"])
			if overlap <= 0 or overlap < ROW_OVERLAP_FRACTION * min(height, rowHeight):
				continue
			gap = line["left"] - row["right"] if line["left"] >= row["left"] else row["left"] - line["right"]
			if gap > ROW_GAP_HEIGHTS * max(height, rowHeight):
				continue
			row["words"] = sorted(row["words"] + line["words"], key=lambda w: w["x"])
			row["top"] = min(row["top"], line["top"])
			row["bottom"] = max(row["bottom"], line["bottom"])
			row["left"] = min(row["left"], line["left"])
			row["right"] = max(row["right"], line["right"])
			break
		else:
			merged.append(dict(line))
	return merged


def groupUnits(data, level):
	"""Windows OCR lines (lists of word dicts, picture coordinates) -> list of Paragraph (the
	reading units for the level), in reading order.

	LEVEL_BLOCK: a line joins the block directly above it when the vertical gap is small *and*
	the two overlap horizontally, so a whole message or section is one unit (a sidebar and a
	message list at the same heights stay apart).
	LEVEL_PARAGRAPH: as a block, but a list item, or a sentence end followed by a sentence
	start (see breaksAfter), also starts a new unit.
	LEVEL_LINE: every recognised line is its own unit.
	Units are ordered column by column (left to right), top to bottom within a column.
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
	lines = mergeRows(lines)
	lines.sort(key=lambda line: (line["top"], line["left"]))
	# The right edge of each line's column: the furthest right of the lines it overlaps
	# horizontally, so a short line can be told from a full one.
	for line in lines:
		line["columnRight"] = max(
			other["right"] for other in lines if _overlap(line["left"], line["right"], other["left"], other["right"]) > 0
		)
	heights = sorted(line["bottom"] - line["top"] for line in lines)
	typical = heights[len(heights) // 2] or 1
	# The typical line pitch (top to top of consecutive, horizontally overlapping lines) is a
	# steadier yardstick than glyph height, which changes with ascenders and descenders.
	# Each line's nearest neighbour below in its own column (the next line in top order may be
	# in another column). Pairs whose upper line fills its column are wrapped lines of one
	# paragraph, and give the true line pitch however generous the leading is.
	pitches = []
	fullPitches = []
	for a in lines:
		below = None
		for b in lines:
			if b is a or b["top"] <= a["top"] or _overlap(a["left"], a["right"], b["left"], b["right"]) <= 0:
				continue
			if below is None or b["top"] < below["top"]:
				below = b
		if below is None or not 0 < below["top"] - a["top"] < typical * 3:
			continue
		pitches.append(below["top"] - a["top"])
		if a["right"] >= a["columnRight"] * CAPITAL_AFTER_SHORT_FRACTION:
			fullPitches.append(below["top"] - a["top"])
	if len(fullPitches) >= 3:
		# Three or more: a wrapped paragraph, not a coincidence of chat lengths. The lower median,
		# so a padded gap between two full chat lines cannot pass for the line pitch.
		pitch = sorted(fullPitches)[(len(fullPitches) - 1) // 2]
	else:
		pitch = sorted(pitches)[len(pitches) // 2] if pitches else typical * 1.5
		# In a chat made of one-line messages most pairs are message gaps, which would inflate
		# the estimate; normal line spacing is never much more than 1.7 glyph heights.
		pitch = min(pitch, typical * 1.7)
	breakPitch = pitch * PARAGRAPH_BREAK_FACTOR

	paragraphs = []
	for line in lines:
		best = None
		bestOverlap = 0
		for p in paragraphs if level != LEVEL_LINE else ():
			if line["top"] - p["lastTop"] > breakPitch or line["top"] < p["top"]:
				continue
			overlap = _overlap(line["left"], line["right"], p["left"], p["right"])
			if overlap > bestOverlap:
				best, bestOverlap = p, overlap
		if best is not None and level == LEVEL_PARAGRAPH:
			if startsItem(line["words"]):
				best = None  # a new list item, whatever came before
			elif best["isItem"]:
				# Inside a numbered or bulleted item. Two layouts:
				# - hanging indent: continuation lines sit at the item's text edge, right of the
				#   marker. Unambiguous, so they join whatever they start with;
				# - flush: continuation lines start at the marker's own edge, where a following
				#   paragraph would start too, so the ordinary rules decide (a short last line, or
				#   a sentence end followed by a sentence start, ends the item).
				# A line further left than the marker has left the list altogether.
				if line["left"] >= best["contentLeft"] - typical * 0.6:
					pass  # hanging continuation
				elif line["left"] < best["itemLeft"] - typical * 0.6:
					best = None
				else:
					shortLast = best["lastRight"] < best["columnRight"] * SHORT_LINE_FRACTION
					shortish = best["lastRight"] < best["columnRight"] * CAPITAL_AFTER_SHORT_FRACTION
					full = best["lastRight"] >= best["columnRight"] * FULL_LINE_FRACTION and line["top"] - best["lastTop"] <= pitch * SENTENCE_NEAR_PITCH_FACTOR
					if shortLast or breaksAfter(best["lines"][-1], line["words"], previousShort=shortish, previousFull=full):
						best = None
			else:
				shortLast = best["lastRight"] < best["columnRight"] * SHORT_LINE_FRACTION
				shortish = best["lastRight"] < best["columnRight"] * CAPITAL_AFTER_SHORT_FRACTION
				full = best["lastRight"] >= best["columnRight"] * FULL_LINE_FRACTION and line["top"] - best["lastTop"] <= pitch * SENTENCE_NEAR_PITCH_FACTOR
				indented = abs(line["left"] - best["lastLeft"]) > typical  # a list under its intro line, or back out of it
				if shortLast or indented or breaksAfter(best["lines"][-1], line["words"], previousShort=shortish, previousFull=full):
					best = None
		if best is None:
			words = line["words"]
			isItem = level == LEVEL_PARAGRAPH and startsItem(words)
			# Where the item's text starts: after the marker word, if the marker is its own word.
			contentLeft = words[1]["x"] if isItem and len(words) > 1 and (words[0]["text"] in _BULLETS or _NUMBERING.match(words[0]["text"])) else line["left"]
			paragraphs.append(dict(line, lines=[words], lastTop=line["top"], lastRight=line["right"], lastLeft=line["left"], isItem=isItem, contentLeft=contentLeft, itemLeft=line["left"]))
			continue
		best["lines"].append(line["words"])
		best["lastTop"] = max(best["lastTop"], line["top"])
		best["lastRight"] = line["right"]
		best["lastLeft"] = line["left"]
		best["columnRight"] = max(best["columnRight"], line["columnRight"])
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


# Speech calls slower than this are logged, to tell a slow voice from a slow app.
SLOW_SPEECH_MS = 150

# Translators: message when a click in a document (a web page, a PDF) lands on no text at all.
NO_TEXT_UNDER_MOUSE = _("No text under the mouse")


def speakLines(lines):
	"""Speak lines of text as one continuous read, handed to the voice in sentence-sized pieces."""
	from speech.speechWithoutPauses import SpeechWithoutPauses

	started = time.time()
	speech.cancelSpeech()
	cancelMs = int((time.time() - started) * 1000)
	reader = SpeechWithoutPauses(speakFunc=speech.speak)
	count = 0
	for line in lines:
		line = line.strip()
		if not line:
			continue
		reader.speakWithoutPauses([line + " "])
		count += 1
	reader.speakWithoutPauses(None)  # flush whatever is left
	totalMs = int((time.time() - started) * 1000)
	if totalMs > SLOW_SPEECH_MS:
		log.info("mouseReader: speaking a paragraph took %d ms (cancel %d ms, %d lines)" % (totalMs, cancelMs, count))


def speakParagraph(paragraph):
	"""Speak a whole recognised paragraph, line by line."""
	speakLines(" ".join(w["text"] for w in line) for line in paragraph.lines)


def isReadingAll() -> bool:
	try:
		return bool(sayAll.SayAllHandler and sayAll.SayAllHandler.isRunning())
	except Exception:
		return False


def stopReadingAll():
	try:
		if sayAll.SayAllHandler:
			sayAll.SayAllHandler.stop()
	except Exception:
		pass


class WindowSnapshot:
	"""What answers the mouse over one window: where the window is, how long the answer is
	trusted for, and whether the window under the pointer is still the same one."""

	live = False  # True when the source answers each hover itself (a document): a scroll needs no new recognition

	def __init__(self, hwnd, rect):
		self.hwnd = hwnd
		self.rect = rect  # screen: left, top, width, height
		self.created = time.time()

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


class Snapshot(WindowSnapshot):
	"""One recognised window: its reading units at every level, and which was read last."""

	def __init__(self, hwnd, rect, unitsByLevel):
		super().__init__(hwnd, rect)
		self.unitsByLevel = unitsByLevel  # level -> list of Paragraph
		self._lastSpoken = None  # (level, index)
		self._doc = None  # built on first "read all"
		self._lineOffsets = {}

	def units(self, level):
		return self.unitsByLevel.get(level) or self.unitsByLevel[LEVEL_PARAGRAPH]

	# ---- read all --------------------------------------------------------------------------

	def _lineKey(self, lineWords):
		return (lineWords[0]["y"], lineWords[0]["x"])

	def _buildDocument(self):
		"""NVDA's recognition-result object over the lines in reading order (paragraph order),
		never focused: it only gives Say All a text to read, chunked by line for the voice."""
		from contentRecog import LinesWordsResult, recogUi

		data = []
		offset = 0
		self._lineOffsets = {}
		for unit in self.units(LEVEL_PARAGRAPH):
			for line in unit.lines:
				self._lineOffsets[self._lineKey(line)] = offset
				data.append(line)
				offset += sum(len(w["text"]) for w in line) + max(len(line) - 1, 0) + 1  # spaces + newline
		left, top, width, height = self.rect
		self._doc = recogUi.RecogResultNVDAObject(result=LinesWordsResult(data, _WindowImageInfo(left, top, width, height)))

	def readAllFrom(self, x: int, y: int, level, obj=None) -> bool:
		"""Start NVDA's Say All at the unit under (or nearest) the point. Returns False if nothing to read."""
		index = self.nearestUnit(x, y, level)
		if index is None:
			return False
		unit = self.units(level)[index]
		try:
			if self._doc is None:
				self._buildDocument()
			offset = self._lineOffsets.get(self._lineKey(unit.lines[0]), 0)
			info = self._doc.makeTextInfo(textInfos.offsets.Offsets(offset, offset))
			if not api.setReviewPosition(info, clearNavigatorObject=True):
				return False
			self._lastSpoken = (level, index)
			speech.cancelSpeech()
			speech.pauseSpeech(False)  # shift in NVDA+shift+click can leave the voice paused
			sayAll.SayAllHandler.readText(sayAll.CURSOR.REVIEW, startedFromScript=True)
			log.info("mouseReader: reading all from unit %d" % (index + 1))
			return True
		except Exception:
			log.exception("mouseReader: could not start reading all")
			return False

	def _toPicture(self, x, y):
		return x - self.rect[0], y - self.rect[1]

	def unitAt(self, x: int, y: int, level):
		"""Index of the unit (at the level) whose box contains the point, or None."""
		px, py = self._toPicture(x, y)
		for i, p in enumerate(self.units(level)):
			if p.left <= px < p.right and p.top <= py < p.bottom:
				return i
		return None

	def nearestUnit(self, x: int, y: int, level):
		"""For a click: the unit under the point, else the nearest one vertically."""
		found = self.unitAt(x, y, level)
		if found is not None:
			return found
		units = self.units(level)
		if not units:
			return None
		px, py = self._toPicture(x, y)

		def distance(p):
			if p.top <= py < p.bottom:
				return 0
			return min(abs(py - p.top), abs(py - p.bottom))

		return min(range(len(units)), key=lambda i: distance(units[i]))

	def speakIndex(self, index, level):
		self._lastSpoken = (level, index)
		speakParagraph(self.units(level)[index])

	def hover(self, x: int, y: int, level, obj=None) -> bool:
		"""Read the unit under the point when it is a different one from the unit read last.
		Blank space and the unit just read leave things alone (so panning Magnifier does not
		repeat it). Returns True when something was read."""
		index = self.unitAt(x, y, level)
		if index is None or (level, index) == self._lastSpoken:
			return False
		self.speakIndex(index, level)
		return True


def buildSnapshot(hwnd, rect, data):
	unitsByLevel = {level: groupUnits(data, level) for level in LEVELS}
	if not unitsByLevel[LEVEL_PARAGRAPH]:
		return None
	return Snapshot(hwnd, rect, unitsByLevel)


class OcrReader:
	"""Runs the recognition for a click or the wheel; keeps the snapshot for hovering."""

	def __init__(self, levelFunc, beepFunc=None, sourceFunc=None):
		"""levelFunc: callable returning the current level (LEVEL_LINE / _PARAGRAPH / _BLOCK).
		beepFunc: callable returning whether a beep should mark each OCR recognition.
		sourceFunc: callable returning SOURCE_AUTO or SOURCE_OCR."""
		self._level = levelFunc
		self._beep = beepFunc or (lambda: False)
		self._source = sourceFunc or (lambda: SOURCE_AUTO)
		self._unstructured = {}  # hwnd -> time its text was found to have no paragraph structure
		self.snapshot = None
		self._pending = None  # recognizer of a recognition still in flight
		self._wheelTimer = None
		self._wheelPos = None
		self._request = 0  # counts starts, so a wait for a loading document knows when it is stale

	def _setSnapshot(self, snapshot):
		"""Replace the snapshot, letting the old one stop any timer of its own first."""
		old = self.snapshot
		if old is not None and old is not snapshot:
			close = getattr(old, "close", None)
			if close is not None:
				try:
					close()
				except Exception:
					pass
		self.snapshot = snapshot

	def shutdown(self):
		self._setSnapshot(None)
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

	def start(self, x: int, y: int, quiet: bool = False, readAllAfter: bool = False, allowDocument: bool = True) -> bool:
		"""Begin reading the window under the point. Returns False if there is no window. A
		browse-mode document under the point answers itself (document.py); anything else is
		recognised with OCR. Once known, the paragraph under the point is read, or, with
		readAllAfter, NVDA's Say All reads on from it. quiet: a refresh after scrolling, with no
		"Recognizing" announcement."""
		found = windowAt(x, y)
		if found is None:
			return False
		hwnd, rect = found
		self._request += 1
		if allowDocument and self._source() == SOURCE_AUTO and self._startDocument(x, y, quiet, readAllAfter, hwnd=hwnd):
			return True
		return self._startOcr(x, y, hwnd, rect, quiet, readAllAfter)

	def _startOcr(self, x, y, hwnd, rect, quiet, readAllAfter) -> bool:
		try:
			from contentRecog import uwpOcr
		except Exception:
			log.debugWarning("mouseReader: OCR modules unavailable", exc_info=True)
			# Translators: message when Windows OCR cannot be used.
			ui.message(_("OCR is not available"))
			return True
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
		if self._beep():
			ocrBeep()  # OCR, as opposed to a document read: audible even when speech is cut short
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
			queueHandler.queueFunction(queueHandler.eventQueue, self._onResult, recognizer, hwnd, rect, result, x, y, quiet, readAllAfter)

		try:
			recognizer.recognize(pixels, imgInfo, onResult)
		except Exception:
			log.exception("mouseReader: OCR failed to start")
			self._pending = None
			ui.message(_("OCR is not available"))
		return True

	def _startDocument(self, x: int, y: int, quiet: bool, readAllAfter: bool, waiting=None, waited: int = 0, hwnd=None) -> bool:
		"""If the point is in a browse-mode document, let the document answer the mouse (no OCR)
		and read what is under the point now. While NVDA is still building its copy of the page,
		look again shortly (OCR takes over if it never finishes). False when there is no such
		document, or when the document's text turned out to have no paragraph structure (then
		the window is read with OCR for a while)."""
		from . import document

		if hwnd is not None and time.time() - self._unstructured.get(hwnd, 0) < UNSTRUCTURED_MEMORY_SECONDS:
			return False
		try:
			found = document.documentAt(x, y, build=waiting is None, known=waiting.ti if waiting is not None else None)
		except Exception:
			log.exception("mouseReader: could not look for a document under the mouse")
			return False
		if found is None:
			return False
		if isinstance(found, document.Loading):
			if waited == 0 and not quiet:
				# Translators: reported while NVDA loads the page under the mouse before reading it.
				ui.message(_("Loading"))
			if waited >= DOCUMENT_WAIT_MS or not found.alive():
				log.info("mouseReader: the page under the mouse did not finish loading in %d ms; using OCR" % waited)
				return False
			request = self._request
			wx.CallLater(DOCUMENT_POLL_MS, self._documentTick, request, x, y, quiet, readAllAfter, found, waited + DOCUMENT_POLL_MS)
			return True
		snapshot, obj = found
		if waited:
			log.info("mouseReader: the page under the mouse loaded after about %d ms" % waited)
		if self._pending is not None:
			try:
				self._pending.cancel()  # an OCR still in flight would overwrite this snapshot
			except Exception:
				pass
			self._pending = None
		self._setSnapshot(snapshot)
		log.info("mouseReader: document under the mouse (%s); reading from it, no OCR" % snapshot.kind)
		level = self._level()
		if readAllAfter:
			done = snapshot.readAllFrom(x, y, level, obj)
		elif quiet:
			if not isReadingAll():
				cx, cy = winUser.getCursorPos()
				if snapshot.covers(cx, cy):
					snapshot.hover(cx, cy, level, document.objectAt(cx, cy))
			return True
		else:
			done = snapshot.speakAt(x, y, level, obj)
		if not done:
			if snapshot.unstructured:
				# A text layer of placed snippets with no paragraphs (a pdf.js viewer, say): the
				# picture is the better source for this window, for a while.
				log.info("mouseReader: the page's text has no paragraph structure here; using OCR for this window")
				self._unstructured[snapshot.hwnd] = time.time()
				self._setSnapshot(None)
				return False
			ui.message(NO_TEXT_UNDER_MOUSE)  # a control or the bare margin: nothing to ask again about
		return True

	def _documentTick(self, request, x, y, quiet, readAllAfter, waiting, waited):
		"""A moment later: is NVDA's copy of the page ready? Then read from it; else wait more,
		or give up and recognise the window instead."""
		if request != self._request:
			return  # a newer click or scroll has taken over
		found = windowAt(x, y)
		if found is None:
			return
		hwnd, rect = found
		if self._startDocument(x, y, quiet, readAllAfter, waiting=waiting, waited=waited, hwnd=hwnd):
			return
		self._startOcr(x, y, hwnd, rect, quiet, readAllAfter)

	def _onResult(self, recognizer, hwnd, rect, result, x, y, quiet, readAllAfter=False):
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
		self._setSnapshot(snapshot)
		log.info(
			"mouseReader: OCR found %d lines, %d paragraphs, %d blocks"
			% tuple(len(snapshot.units(level)) for level in LEVELS)
		)
		level = self._level()
		if readAllAfter:
			if not snapshot.readAllFrom(x, y, level):
				ui.message(_("No text recognized"))
			return
		# Read what is under the pointer now (for a click, the clicked spot; after scrolling,
		# wherever the pointer is): the nearest unit for a click, only an exact hit after a scroll.
		if quiet:
			if isReadingAll():
				return  # the reading carries on; the fresh snapshot waits for the next hover
			cx, cy = winUser.getCursorPos()
			if snapshot.covers(cx, cy):
				snapshot.hover(cx, cy, level)
			return
		index = snapshot.nearestUnit(x, y, level)
		if index is None:
			ui.message(_("No text recognized"))
			return
		snapshot.speakIndex(index, level)

	def readAll(self, x: int, y: int) -> bool:
		"""Read on from the point: from the fresh snapshot if it covers the point, else after
		recognising the window. Returns False if there is no window under the point."""
		stopReadingAll()
		snapshot = self.snapshot
		if snapshot is not None and snapshot.isFresh() and snapshot.covers(x, y):
			if snapshot.readAllFrom(x, y, self._level()):
				return True
		return self.start(x, y, readAllAfter=True)

	# ---- hover ---------------------------------------------------------------------------

	def claim(self, x: int, y: int, obj=None) -> bool:
		"""Called for every mouse move NVDA reports (obj: what NVDA found under the pointer). True
		when a fresh snapshot covers the point; the paragraph there is read if it is not the one
		read last. A document only answers for its own text and blank space, so its toolbar and
		its controls still get NVDA's usual reading."""
		snapshot = self.snapshot
		if snapshot is None:
			return False
		if not snapshot.isFresh():
			self._setSnapshot(None)
			return False
		started = time.time()
		if not snapshot.covers(x, y):
			return False
		if snapshot.live and not snapshot.claims(obj):
			return False
		checkMs = int((time.time() - started) * 1000)
		if checkMs > SLOW_SPEECH_MS:
			log.info("mouseReader: the window check took %d ms" % checkMs)
		if isReadingAll():
			return True  # the mouse is ignored while reading all; NVDA's tracking stays out too
		snapshot.hover(x, y, self._level(), obj)
		return True

	# ---- the wheel -----------------------------------------------------------------------

	def wheelScrolled(self, x: int, y: int):
		"""Main thread. The wheel turned at the point; if that is over the recognised window,
		recognise it again once the wheel has been quiet for a moment."""
		snapshot = self.snapshot
		if snapshot is None or not snapshot.contains(x, y):
			return
		if snapshot.live:
			snapshot.scrolled()  # a document answers each hover itself; only its rectangles need refreshing
			return
		found = windowAt(x, y)
		if found is None or found[0] != snapshot.hwnd:
			return
		self._wheelPos = (x, y)
		if self._wheelTimer is None:
			self._wheelTimer = wx.CallLater(WHEEL_RERECOGNIZE_MS, self._reRecognize)
		else:
			self._wheelTimer.Start(WHEEL_RERECOGNIZE_MS)

	def forget(self):
		stopReadingAll()
		self._setSnapshot(None)
		if self._wheelTimer is not None:
			try:
				self._wheelTimer.Stop()
			except Exception:
				pass

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
