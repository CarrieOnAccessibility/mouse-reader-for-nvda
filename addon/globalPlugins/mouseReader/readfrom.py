# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""Read from here.

Hold NVDA+control and click (or press the "read from the mouse position" command) and NVDA
reads continuously from that spot, the way Windows Magnifier's read-aloud does. The click is
swallowed so the app underneath never sees it. Reading uses NVDA's own Say All from the review
cursor, so it stops on any key press, scrolls the document, and follows NVDA's Say All
settings. Because Say All keeps the review cursor on the text being spoken, "skip back" and
"skip forward" are simply: stop, move the review cursor a paragraph, read again.

Finding the starting point runs the fast rungs of the lookup ladder (ladder.py). In a browse
mode document (a web page) the position is carried into the document so reading continues
past the paragraph that was clicked. When nothing under the pointer has text at all, and the
option is on, the window under the pointer is OCRed with NVDA's built-in Windows OCR and
reading starts from the recognised line nearest the click; that result is NVDA's usual OCR
document (Escape leaves it).
"""

from ctypes.wintypes import POINT

import addonHandler
import api
import keyboardHandler
import queueHandler
import textInfos
import textInfos.offsets
import treeInterceptorHandler
import ui
import winUser
from logHandler import log
from speech import sayAll

from . import ladder

try:
	addonHandler.initTranslation()
except Exception:
	pass

START_UNITS = {
	"paragraph": textInfos.UNIT_PARAGRAPH,
	"line": textInfos.UNIT_LINE,
	"word": textInfos.UNIT_WORD,
	"point": None,
}

_CONTROL_KEYS = (winUser.VK_CONTROL, winUser.VK_LCONTROL, winUser.VK_RCONTROL)


def modifiersHeld() -> bool:
	"""Is NVDA+control held right now? NVDA tracks held modifiers itself (the NVDA key never
	reaches Windows, so its key state cannot be asked for)."""
	mods = set(keyboardHandler.currentModifiers)
	nvda = any(keyboardHandler.isNVDAModifierKey(vk, ext) for vk, ext in mods)
	ctrl = any(vk in _CONTROL_KEYS for vk, ext in mods)
	return nvda and ctrl


class ReadFromHere:
	def __init__(self, settings, dwellEngine):
		"""settings: object with readFromClick(), readFromStart(), readFromOcr() callables."""
		self._settings = settings
		self._dwell = dwellEngine
		self._sessionMode = None  # sayAll.CURSOR of the reading we started, or None
		self._ocrDoc = None

	# ---- state ------------------------------------------------------------------------

	def isReading(self) -> bool:
		"""True while a reading we started is still speaking (mouse tracking pauses meanwhile)."""
		if self._sessionMode is None:
			return False
		handler = sayAll.SayAllHandler
		try:
			return bool(handler and handler.isRunning())
		except Exception:
			return False

	def stop(self):
		handler = sayAll.SayAllHandler
		if handler:
			try:
				handler.stop()
			except Exception:
				pass

	# ---- triggers ---------------------------------------------------------------------

	def onButton(self, msg, x, y, injected) -> bool:
		"""Low-level hook (hook thread): True to swallow the click and read from that point."""
		if injected or not self._settings.readFromClick():
			return False
		if not modifiersHeld():
			return False
		queueHandler.queueFunction(queueHandler.eventQueue, self.readFrom, x, y)
		return True

	def readFromMouse(self):
		x, y = winUser.getCursorPos()
		self.readFrom(x, y)

	# ---- starting ----------------------------------------------------------------------

	def readFrom(self, x: int, y: int):
		"""Main thread. Find text at the point and start reading from it."""
		self.stop()
		self._sessionMode = None
		self._dwell.forgetLadder()
		try:
			obj, info, pointSupported = ladder.objectAndTextInfoAt(x, y)
		except Exception:
			log.exception("mouseReader: could not look up the text under the mouse")
			obj, info, pointSupported = None, None, False
		if info is not None:
			try:
				self._startFromInfo(obj, info, pointSupported)
				return
			except Exception:
				log.exception("mouseReader: could not start reading from the text under the mouse")
		if self._settings.readFromOcr():
			self._startOcr(x, y)
		else:
			# Translators: message when Read from here finds nothing to read and OCR is turned off.
			ui.message(_("No text under the mouse"))

	def _startFromInfo(self, obj, info, pointSupported):
		startUnit = START_UNITS.get(self._settings.readFromStart(), textInfos.UNIT_PARAGRAPH)
		docInfo = self._intoDocument(obj, info, pointSupported, startUnit)
		if docInfo is not None:
			info = docInfo
		info.collapse()
		if startUnit is not None:
			try:
				info.expand(startUnit)
				info.collapse()
			except Exception:
				log.debugWarning("mouseReader: could not move to the start of the %s" % startUnit, exc_info=True)
		if not api.setReviewPosition(info, clearNavigatorObject=True):
			return
		self._sessionMode = sayAll.CURSOR.REVIEW
		sayAll.SayAllHandler.readText(sayAll.CURSOR.REVIEW, startedFromScript=True)

	def _intoDocument(self, obj, info, pointSupported, startUnit):
		"""In a browse mode document, a TextInfo on the clicked object only covers that object;
		reading would stop at the end of the paragraph. Carry the position into the document
		instead. Exact for the start of the paragraph (the object's own start); for a spot inside
		it, the characters before the pointer are counted in the object and stepped over in the
		document, which is close but not guaranteed exact where the object contains other objects.
		"""
		ti = getattr(obj, "treeInterceptor", None)
		if not ti or not isinstance(ti, treeInterceptorHandler.DocumentTreeInterceptor):
			return None
		try:
			if not ti.isReady:
				return None
		except Exception:
			return None
		docInfo = None
		node = obj
		for _step in range(6):  # the clicked object may be a text leaf the document flattened away
			if node is None:
				break
			try:
				docInfo = ti.makeTextInfo(node)
				break
			except Exception:
				node = getattr(node, "parent", None)
		if docInfo is None:
			return None
		docInfo.collapse()
		if startUnit == textInfos.UNIT_PARAGRAPH or not pointSupported or node is not obj:
			return docInfo
		try:
			before = obj.makeTextInfo(textInfos.POSITION_FIRST)
			before.setEndPoint(info, "endToStart")
			count = len(before.text)
			if count > 0:
				docInfo.move(textInfos.UNIT_CHARACTER, count)
		except Exception:
			log.debugWarning("mouseReader: could not map the pointer into the document", exc_info=True)
		return docInfo

	# ---- skipping ----------------------------------------------------------------------

	def skip(self, direction: int):
		"""Stop, move a paragraph back (-1) or forward (+1) from where reading is, read again."""
		mode = self._sessionMode if self._sessionMode is not None else sayAll.CURSOR.REVIEW
		self.stop()
		try:
			if mode == sayAll.CURSOR.CARET:
				obj = api.getFocusObject()
				info = obj.makeTextInfo(textInfos.POSITION_CARET)
			else:
				info = api.getReviewPosition().copy()
		except Exception:
			log.debugWarning("mouseReader: no position to skip from", exc_info=True)
			return
		info.collapse()
		try:
			# To the start of the paragraph being read, then one more back if going back.
			info.expand(textInfos.UNIT_PARAGRAPH)
			info.collapse()
			moved = info.move(textInfos.UNIT_PARAGRAPH, direction)
		except Exception:
			log.debugWarning("mouseReader: could not move by paragraph", exc_info=True)
			moved = 0
		if moved == 0:
			# Translators: reported when there is no next / previous paragraph to skip to.
			ui.message(_("Bottom") if direction > 0 else _("Top"))
			if direction > 0:
				return
		try:
			if mode == sayAll.CURSOR.CARET:
				info.updateCaret()
				self._sessionMode = sayAll.CURSOR.CARET
				sayAll.SayAllHandler.readText(sayAll.CURSOR.CARET, startedFromScript=True)
			else:
				if not api.setReviewPosition(info, clearNavigatorObject=True):
					return
				self._sessionMode = sayAll.CURSOR.REVIEW
				sayAll.SayAllHandler.readText(sayAll.CURSOR.REVIEW, startedFromScript=True)
		except Exception:
			log.exception("mouseReader: could not restart reading after a skip")

	# ---- OCR fallback -------------------------------------------------------------------

	def _startOcr(self, x: int, y: int):
		try:
			from contentRecog import RecogImageInfo, recogUi, uwpOcr
		except Exception:
			log.debugWarning("mouseReader: OCR modules unavailable", exc_info=True)
			# Translators: message when Windows OCR cannot be used.
			ui.message(_("OCR is not available"))
			return
		if isinstance(api.getFocusObject(), recogUi.RecogResultNVDAObject):
			# Translators: message when Read from here is used while an OCR result is already open.
			ui.message(_("Already in an OCR result; press Escape to leave it first"))
			return
		try:
			recognizer = uwpOcr.UwpOcr()
		except Exception:
			log.debugWarning("mouseReader: could not create the OCR recognizer", exc_info=True)
			ui.message(_("OCR is not available"))
			return
		rect = _windowRectAt(x, y)
		if rect is None:
			ui.message(_("No text under the mouse"))
			return
		left, top, width, height = rect
		try:
			imgInfo = RecogImageInfo.createFromRecognizer(left, top, width, height, recognizer)
		except ValueError:
			ui.message(_("No text under the mouse"))
			return
		startUnit = START_UNITS.get(self._settings.readFromStart(), textInfos.UNIT_PARAGRAPH)
		previous = self._ocrDoc
		if previous is not None and previous.result is None:
			try:
				previous.recognizer.cancel()  # a second click before the first OCR came back
			except Exception:
				pass
		# Translators: reported while the window under the mouse is being OCRed.
		ui.message(_("Recognizing"))
		doc = _ocrDocumentClass()(recognizer, imgInfo, (x, y), startUnit, self)
		self._ocrDoc = doc
		try:
			doc.start()
		except Exception:
			log.exception("mouseReader: OCR failed to start")
			ui.message(_("OCR is not available"))

	def _ocrReady(self):
		"""Called on the main thread as the OCR document takes focus and starts reading."""
		self._sessionMode = sayAll.CURSOR.CARET


def _windowRectAt(x: int, y: int):
	"""The top-level window under the point, as (left, top, width, height), clipped to the
	screen; the OCR image must not start off-screen."""
	try:
		hwnd = winUser.user32.WindowFromPoint(POINT(x, y))
		if hwnd:
			hwnd = winUser.getAncestor(hwnd, winUser.GA_ROOT) or hwnd
	except Exception:
		hwnd = None
	if not hwnd:
		return None
	r = winUser.RECT()
	if not winUser.user32.GetWindowRect(hwnd, r):
		return None
	import wx

	screenLeft = screenTop = 0
	screenRight = screenBottom = 0
	try:
		for i in range(wx.Display.GetCount()):
			g = wx.Display(i).GetGeometry()
			screenLeft = min(screenLeft, g.GetLeft())
			screenTop = min(screenTop, g.GetTop())
			screenRight = max(screenRight, g.GetRight() + 1)
			screenBottom = max(screenBottom, g.GetBottom() + 1)
	except Exception:
		screenRight, screenBottom = winUser.user32.GetSystemMetrics(0), winUser.user32.GetSystemMetrics(1)
	left = max(r.left, max(screenLeft, 0))
	top = max(r.top, max(screenTop, 0))
	right = min(r.right, screenRight)
	bottom = min(r.bottom, screenBottom)
	if right - left < 8 or bottom - top < 8:
		return None
	return left, top, right - left, bottom - top


def _offsetNearPoint(result, x: int, y: int) -> int:
	"""Offset in a LinesWordsResult of the word under the point, else the nearest word on the
	line under the point, else the first word of the nearest line. For a deliberate click,
	"nearest" is the right answer (the user wants reading to start somewhere sensible)."""
	words = result.words
	if not words:
		return 0
	# Group words into lines using the line end offsets.
	lines = []
	lineEnds = list(result.lines)
	lineIndex = 0
	current = []
	for word in words:
		while lineIndex < len(lineEnds) and word.offset >= lineEnds[lineIndex]:
			lines.append(current)
			current = []
			lineIndex += 1
		current.append(word)
	lines.append(current)
	lines = [line for line in lines if line]
	bestLine = None
	bestDistance = None
	for line in lines:
		top = min(w.top for w in line)
		bottom = max(w.top + w.height for w in line)
		if top <= y < bottom:
			distance = 0
		else:
			distance = min(abs(y - top), abs(y - bottom))
		if bestDistance is None or distance < bestDistance:
			bestDistance = distance
			bestLine = line
	if bestLine is None:
		return 0
	for w in bestLine:
		if w.left <= x < w.left + w.width:
			return w.offset
	nearest = min(bestLine, key=lambda w: min(abs(x - w.left), abs(x - (w.left + w.width))))
	return nearest.offset


_ocrDocumentClassCache = None


def _ocrDocumentClass():
	"""Built on first use so that importing this module never depends on contentRecog."""
	global _ocrDocumentClassCache
	if _ocrDocumentClassCache is not None:
		return _ocrDocumentClassCache
	from contentRecog import recogUi

	class _OcrReadFromDocument(recogUi.RefreshableRecogResultNVDAObject):
		"""NVDA's own OCR result document, opened at the line under the click and read from there.

		The recogniser's callback (another thread) stores the result and queues the focus
		event; the cursor is placed when that event runs on the main thread, so nothing races.
		"""

		def __init__(self, recognizer, imageInfo, point, startUnit, owner):
			self._point = point
			self._startUnit = startUnit
			self._owner = owner
			self._placeCursorOnFocus = True
			super().__init__(recognizer=recognizer, imageInfo=imageInfo)

		def event_gainFocus(self):
			if self._placeCursorOnFocus and self.result:
				self._placeCursorOnFocus = False
				try:
					offset = _offsetNearPoint(self.result, *self._point)
					info = self.makeTextInfo(textInfos.offsets.Offsets(offset, offset))
					if self._startUnit is not None:
						info.expand(self._startUnit)
						info.collapse()
					self._selection = info
				except Exception:
					log.debugWarning("mouseReader: could not place the OCR cursor under the mouse", exc_info=True)
				self._shouldSayAllOnFirstFocus = True
				self._owner._ocrReady()
			super().event_gainFocus()

	_ocrDocumentClassCache = _OcrReadFromDocument
	return _OcrReadFromDocument
