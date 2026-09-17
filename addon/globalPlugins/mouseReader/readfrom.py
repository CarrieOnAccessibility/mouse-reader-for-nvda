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

The starting point is found the way NVDA's own mouse tracking finds it: the object under the
point, and the text position at the point within it. In a browse mode document (a web page)
the position is carried into the document so reading continues past the paragraph that was
clicked. When nothing under the pointer has text at all, and the option is on, the window
under the pointer is OCRed (ocr.py) and reading starts from the recognised paragraph nearest
the click, with nothing opened and nothing to close; the snapshot then reads paragraphs on
hover for a few minutes.
"""

import math
import time

import addonHandler
import api
import config
import controlTypes
import keyboardHandler
import locationHelper
import queueHandler
import textInfos
import textUtils
import treeInterceptorHandler
import ui
import winUser
from logHandler import log
from speech import sayAll

from . import ocr

try:
	addonHandler.initTranslation()
except Exception:
	pass

REPEAT_CLICK_SECONDS = 4
REPEAT_CLICK_PX = 40

START_UNITS = {
	"paragraph": textInfos.UNIT_PARAGRAPH,
	"line": textInfos.UNIT_LINE,
	"word": textInfos.UNIT_WORD,
	"point": None,
}

_CONTROL_KEYS = (winUser.VK_CONTROL, winUser.VK_LCONTROL, winUser.VK_RCONTROL)


_SHIFT_KEYS = (winUser.VK_SHIFT, winUser.VK_LSHIFT, winUser.VK_RSHIFT)


def _keyDown(vk) -> bool:
	try:
		return bool(winUser.getKeyState(vk) & 0x8000)
	except Exception:
		return False


def heldCombination():
	"""Which of our click combinations is held: "read" (NVDA+control), "recognize"
	(NVDA+control+shift) or None. The NVDA key never reaches Windows, so only NVDA's own record
	of held modifiers knows about it; control and shift are checked there and with Windows too."""
	mods = set(keyboardHandler.currentModifiers)
	nvda = any(keyboardHandler.isNVDAModifierKey(vk, ext) for vk, ext in mods)
	if not nvda:
		return None
	ctrl = any(vk in _CONTROL_KEYS for vk, ext in mods) or _keyDown(winUser.VK_CONTROL)
	if not ctrl:
		return None
	shift = any(vk in _SHIFT_KEYS for vk, ext in mods) or _keyDown(winUser.VK_SHIFT)
	return "recognize" if shift else "read"


CONTAINER_ROLES = frozenset(
	role
	for role in (
		getattr(controlTypes.Role, name, None)
		for name in (
			"UNKNOWN", "WINDOW", "PANE", "DIALOG", "FRAME", "DOCUMENT", "APPLICATION", "GROUPING",
			"PROPERTYPAGE", "CANVAS", "GLASSPANE", "LAYEREDPANE", "ROOTPANE", "SCROLLPANE",
			"SECTION", "SPLITPANE", "DESKTOPPANE", "PANEL", "LANDMARK", "REGION",
		)
	)
	if role is not None
)


def isBlank(text) -> bool:
	"""NVDA's own test from NVDAObject.event_mouseMove: nothing but whitespace and object marks."""
	if not text:
		return True
	for ch in text:
		if not ch.isspace() and ch != textUtils.OBJ_REPLACEMENT_CHAR:
			return False
	return True


def nvdaHasTextAt(obj, x: int, y: int) -> bool:
	"""Would NVDA's own mouse tracking have something to read for obj at the point? (Mirrors
	NVDAObject.event_mouseMove; the "same chunk as before" silence counts as having text.)"""
	if obj is None:
		return False
	try:
		info = obj.makeTextInfo(locationHelper.Point(x, y))
	except NotImplementedError:
		# Only the object's own label is available: real for a control, meaningless for a container.
		try:
			if obj.role in CONTAINER_ROLES:
				return False
			return not isBlank(obj.name)
		except Exception:
			return False
	except LookupError:
		return False
	except Exception:
		log.debugWarning("mouseReader: text lookup failed; assuming NVDA has text", exc_info=True)
		return True
	try:
		info.expand(info.unit_mouseChunk)
		return not isBlank(info.text)
	except Exception:
		return True


def objectAndTextInfoAt(x: int, y: int):
	"""(object, TextInfo at the point, pointSupported) the way NVDA's mouse tracking sees it.

	info is None when nothing under the point has text at all (the caller may then OCR).
	pointSupported is False when the object cannot map a point to text and info is merely the
	start of its own text (a button's label, say).
	"""
	try:
		obj = api.getDesktopObject().objectFromPoint(x, y)
	except Exception:
		log.debugWarning("mouseReader: objectFromPoint failed", exc_info=True)
		return None, None, False
	while obj and getattr(obj, "beTransparentToMouse", False):
		obj = obj.parent
	if obj is None:
		return None, None, False
	try:
		return obj, obj.makeTextInfo(locationHelper.Point(x, y)), True
	except (NotImplementedError, LookupError):
		pass
	except Exception:
		log.debugWarning("mouseReader: makeTextInfo(Point) failed", exc_info=True)
	# The object cannot map a point to text. Its own text (a label) is still worth reading,
	# unless it is a bare container, whose name says nothing about the spot clicked.
	try:
		if obj.role in CONTAINER_ROLES:
			return obj, None, False
		info = obj.makeTextInfo(textInfos.POSITION_FIRST)
		probe = info.copy()
		probe.expand(textInfos.UNIT_STORY)
		if isBlank(probe.text):
			return obj, None, False
		return obj, info, False
	except Exception:
		return obj, None, False


class ReadFromHere:
	def __init__(self, settings):
		"""settings: object with readFromClick(), readFromStart(), readFromOcr() callables."""
		self._settings = settings
		self._sessionMode = None  # sayAll.CURSOR of the reading we started, or None
		self._ocr = ocr.OcrReader(self)
		self._lastStart = None  # (x, y, time) of the last click that started a reading

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

	def shutdown(self):
		self._ocr.shutdown()

	def stop(self):
		handler = sayAll.SayAllHandler
		if handler:
			try:
				handler.stop()
			except Exception:
				pass

	def sessionStarted(self, mode):
		self._sessionMode = mode

	def onMouseMove(self, obj, x: int, y: int):
		"""Every mouse move NVDA reports (after any delay add-on has had its say). If a fresh OCR
		snapshot covers the spot and NVDA itself has no text there, read the snapshot's
		paragraph under the pointer."""
		if self._ocr.snapshot is None:
			return
		if not self._ocr.snapshot.contains(x, y):
			return  # cheap rectangle test first; the window check happens inside hover()
		if nvdaHasTextAt(obj, x, y):
			return
		self._ocr.hover(x, y)

	# ---- triggers ---------------------------------------------------------------------

	def onButton(self, msg, x, y, injected) -> bool:
		"""Low-level hook (hook thread): True to swallow the click and read from that point."""
		if not self._settings.readFromClick():
			return False
		if injected and config.conf["mouse"]["ignoreInjectedMouseInput"]:
			return False  # NVDA's own rule for clicks generated by other software
		combination = heldCombination()
		if combination is None:
			return False
		if combination == "recognize":
			log.info("mouseReader: NVDA+control+shift+click at (%d, %d)" % (x, y))
			queueHandler.queueFunction(queueHandler.eventQueue, self.recognize, x, y)
		else:
			log.info("mouseReader: NVDA+control+click at (%d, %d)" % (x, y))
			queueHandler.queueFunction(queueHandler.eventQueue, self.readFrom, x, y)
		return True

	def _isRepeatClick(self, x: int, y: int) -> bool:
		"""Another click within a few seconds, close to where reading started, while it is still
		reading: an impatient second click, not a request to start over."""
		last = self._lastStart
		if last is None or not self.isReading():
			return False
		lx, ly, when = last
		return time.time() - when < REPEAT_CLICK_SECONDS and math.hypot(x - lx, y - ly) < REPEAT_CLICK_PX

	def onWheel(self, x, y, injected):
		"""Low-level hook (hook thread): the wheel turned. A recognised window may have scrolled."""
		queueHandler.queueFunction(queueHandler.eventQueue, self._ocr.wheelScrolled, x, y)

	def readFromMouse(self):
		x, y = winUser.getCursorPos()
		self.readFrom(x, y)

	def recognizeAtMouse(self):
		x, y = winUser.getCursorPos()
		self.recognize(x, y)

	def moveReviewToMouse(self):
		"""Put the review cursor on the text under the mouse and say nothing more; NVDA's own
		"say all from review cursor" then reads from there."""
		x, y = winUser.getCursorPos()
		startUnit = START_UNITS.get(self._settings.readFromStart(), textInfos.UNIT_PARAGRAPH)
		obj, info, pointSupported = objectAndTextInfoAt(x, y)
		if info is None and self._ocr.snapshotFor(x, y) is not None:
			info = self._ocr.textInfoAt(x, y, startUnit)
			obj = None
		if info is None:
			ui.message(_("No text under the mouse"))
			return
		docInfo = self._intoDocument(obj, info, pointSupported, startUnit) if obj is not None else None
		if docInfo is not None:
			info = docInfo
		info.collapse()
		if startUnit is not None:
			try:
				info.expand(startUnit)
				info.collapse()
			except Exception:
				pass
		if api.setReviewPosition(info, clearNavigatorObject=True):
			# Translators: reported after the review cursor has been moved to the mouse position.
			ui.message(_("Review cursor at mouse"))

	def recognize(self, x: int, y: int):
		"""Main thread. OCR the window under the point for hovering; reads nothing."""
		self.stop()
		if not self._ocr.start(x, y, None, readAfter=False):
			ui.message(_("No text under the mouse"))

	# ---- starting ----------------------------------------------------------------------

	def readFrom(self, x: int, y: int):
		"""Main thread. Find text at the point and start reading from it."""
		if self._isRepeatClick(x, y):
			log.info("mouseReader: repeat click on the spot being read; letting it carry on")
			return
		self._lastStart = (x, y, time.time())
		self.stop()
		self._sessionMode = None
		try:
			obj, info, pointSupported = objectAndTextInfoAt(x, y)
		except Exception:
			log.exception("mouseReader: could not look up the text under the mouse")
			obj, info, pointSupported = None, None, False
		log.info(
			"mouseReader: read from (%d, %d): object %r, text %s"
			% (x, y, obj, "at the point" if pointSupported else ("from its start" if info is not None else "none"))
		)
		if info is not None:
			try:
				self._startFromInfo(obj, info, pointSupported)
				return
			except Exception:
				log.exception("mouseReader: could not start reading from the text under the mouse")
		# No real text here. Always recognise afresh (the window may have scrolled since the
		# last time); the new snapshot then serves hovering. Recognition takes well under a second.
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
		startUnit = START_UNITS.get(self._settings.readFromStart(), textInfos.UNIT_PARAGRAPH)
		if not self._ocr.start(x, y, startUnit):
			ui.message(_("No text under the mouse"))
