# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""The lookup ladder.

NVDA's own mouse tracking (rung 1) asks the app for the object under the pointer and reads
the text there. Some apps answer with a bare container and no text, so the pointer goes
silent even though there is plainly text on screen. When that happens, the next rungs ask in
other ways, each of them cheap:

  rung 2  UI Automation, straight from the screen point. NVDA normally ignores UIA in windows
          it does not class as "UIA windows" (Chromium, Electron and friends); this rung asks
          anyway, which is exactly what fixes VS Code, Teams and Windows Terminal.
  rung 3  NVDA's display model: its own record of the text that older desktop apps drew on
          screen (what screen review reads).

One rule for every rung: it may only report text that sits under the pointer, never text
that is merely nearby. A blank spot must stay blank, so that whatever NVDA was reading keeps
going. OCR is deliberately not on this ladder; it is a "Read from here" fallback only
(readfrom.py), because it costs a fraction of a second and only makes sense once the user
has asked for something to be read.
"""

import api
import controlTypes
from ctypes.wintypes import POINT
from comtypes import COMError
import displayModel
import globalVars
import locationHelper
from logHandler import log
from NVDAObjects import NVDAObjectTextInfo
import speech
import textInfos
import textUtils

try:
	import UIAHandler
	from NVDAObjects.UIA import UIA
except Exception:  # UIA unavailable; rung 2 just does nothing
	UIAHandler = None
	UIA = None

Role = controlTypes.Role

# Roles whose *name* is not "text under the pointer": a pane called "Chrome Legacy Window" or a
# document titled "Untitled" tells the user nothing about the spot they are pointing at.
CONTAINER_ROLES = frozenset(
	role
	for role in (
		getattr(Role, name, None)
		for name in (
			"UNKNOWN",
			"WINDOW",
			"PANE",
			"DIALOG",
			"FRAME",
			"DOCUMENT",
			"APPLICATION",
			"GROUPING",
			"PROPERTYPAGE",
			"CANVAS",
			"EMBEDDEDOBJECT",
			"GLASSPANE",
			"LAYEREDPANE",
			"ROOTPANE",
			"SCROLLPANE",
			"SECTION",
			"SPLITPANE",
			"TEXTFRAME",
			"INTERNALFRAME",
			"DESKTOPPANE",
			"PANEL",
			"LANDMARK",
			"ARTICLE",
			"REGION",
			"FIGURE",
			"LIST",
			"TABLE",
			"TREEVIEW",
			"TABCONTROL",
			"TOOLBAR",
			"MENUBAR",
			"STATUSBAR",
			"SCROLLBAR",
			"TITLEBAR",
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


def nvdaFoundText(obj, x: int, y: int) -> bool:
	"""Did NVDA's own mouse tracking have something to read for obj at (x, y)?

	Mirrors NVDAObject.event_mouseMove. "Found" includes the times NVDA chose to stay quiet
	because the pointer is still in the same chunk; the ladder must not second-guess that.
	"""
	if obj is None:
		return False
	pointSupported = True
	try:
		info = obj.makeTextInfo(locationHelper.Point(x, y))
	except NotImplementedError:
		pointSupported = False
		try:
			info = NVDAObjectTextInfo(obj, textInfos.POSITION_FIRST)
		except Exception:
			return False
	except LookupError:
		return False
	except Exception:
		log.debugWarning("mouseReader: text lookup failed; assuming NVDA handled it", exc_info=True)
		return True
	try:
		info.expand(info.unit_mouseChunk)
		text = info.text
	except Exception:
		return pointSupported
	if isBlank(text):
		return False
	if pointSupported:
		return True
	# Only the object's own label was available. For a real control (a button, a link) that is
	# what NVDA reads on entry, and it is right. For a container it is not text under the pointer.
	return obj.role not in CONTAINER_ROLES


def _rectsContain(info, x: int, y: int):
	"""True / False when the TextInfo can say whether it covers the point; None when it cannot."""
	try:
		rects = info.boundingRects
	except (NotImplementedError, LookupError):
		return None
	except Exception:
		log.debugWarning("mouseReader: boundingRects failed", exc_info=True)
		return None
	if not rects:
		return None
	point = locationHelper.Point(x, y)
	return any(point in rect for rect in rects)


class Found:
	"""What a rung found: either a chunk of text or a control to describe."""

	__slots__ = ("text", "obj", "key")

	def __init__(self, text=None, obj=None, key=None):
		self.text = text
		self.obj = obj
		self.key = key

	def speak(self):
		speech.cancelSpeech()
		if self.text is not None:
			speech.speakText(self.text)
		elif self.obj is not None:
			speech.speakObject(self.obj, reason=controlTypes.OutputReason.MOUSE)


def _textUnderPointer(obj, x: int, y: int, requireRects: bool):
	"""The mouse chunk of obj's text that sits under the point, or None.

	requireRects: insist that the TextInfo proves the point is inside the chunk. UIA's
	RangeFromPoint returns the *nearest* text when the point is on nothing, so for rung 2 the
	proof is mandatory; the display model already hit-tests character by character.
	"""
	try:
		info = obj.makeTextInfo(locationHelper.Point(x, y))
	except (NotImplementedError, LookupError):
		return None
	except Exception:
		log.debugWarning("mouseReader: makeTextInfo(Point) failed", exc_info=True)
		return None
	try:
		info.expand(info.unit_mouseChunk)
		text = info.text
	except Exception:
		return None
	if isBlank(text):
		return None
	contains = _rectsContain(info, x, y)
	if contains is False or (contains is None and requireRects):
		return None
	return text


def uiaTextAt(x: int, y: int):
	"""Rung 2: ask UI Automation for the element at the point, ignoring NVDA's non-UIA-window rule."""
	if UIAHandler is None or UIA is None or not getattr(UIAHandler, "handler", None):
		return None
	handler = UIAHandler.handler
	try:
		element = handler.clientObject.ElementFromPointBuildCache(POINT(x, y), handler.baseCacheRequest)
	except COMError:
		return None
	if not element:
		return None
	try:
		if element.cachedProcessId == globalVars.appPid:
			return None  # NVDA's own windows; never inspect them from here
	except COMError:
		return None
	try:
		obj = UIA(UIAElement=element)
	except Exception:
		return None
	text = _textUnderPointer(obj, x, y, requireRects=True)
	if text is not None:
		return Found(text=text, key=("text", text))
	# No text pattern under the point. A named leaf control is still worth reporting.
	try:
		role = obj.role
		name = obj.name
		location = obj.location
	except Exception:
		return None
	if role in CONTAINER_ROLES or isBlank(name):
		return None
	if location:
		try:
			if locationHelper.Point(x, y) not in location:
				return None
		except Exception:
			pass
	return Found(obj=obj, key=("obj", role, name, tuple(location) if location else None))


def displayModelTextAt(obj, x: int, y: int):
	"""Rung 3: NVDA's display model for the window that obj lives in."""
	if obj is None or not getattr(obj, "windowHandle", None):
		return None
	try:
		info = displayModel.DisplayModelTextInfo(obj, locationHelper.Point(x, y))
		info.expand(info.unit_mouseChunk)
		text = info.text
	except (LookupError, NotImplementedError):
		return None
	except Exception:
		log.debugWarning("mouseReader: display model lookup failed", exc_info=True)
		return None
	if isBlank(text):
		return None
	return Found(text=text, key=("text", text))


def climb(x: int, y: int):
	"""Run the extra rungs for the point NVDA just handled.

	Returns (nvdaHandled, found): nvdaHandled is True when NVDA's own tracking had text there
	(so the ladder stayed out of it); otherwise found is a Found from a later rung, or None
	when nothing under the pointer has text.
	"""
	obj = api.getMouseObject()
	if nvdaFoundText(obj, x, y):
		return True, None
	for rung in (lambda: uiaTextAt(x, y), lambda: displayModelTextAt(obj, x, y)):
		try:
			found = rung()
		except Exception:
			log.debugWarning("mouseReader: ladder rung failed", exc_info=True)
			found = None
		if found is not None:
			return False, found
	return False, None


def objectAndTextInfoAt(x: int, y: int):
	"""For "Read from here": the best (object, TextInfo at the point) the fast rungs can give.

	Returns (obj, info, pointSupported). info is None when nothing under the point has text at
	all; the caller may then try OCR. pointSupported is False when info is only the object's
	first position rather than the pointer's position.
	"""
	desktop = api.getDesktopObject()
	try:
		obj = desktop.objectFromPoint(x, y)
	except Exception:
		log.debugWarning("mouseReader: objectFromPoint failed", exc_info=True)
		obj = None
	while obj and getattr(obj, "beTransparentToMouse", False):
		obj = obj.parent
	candidates = []
	if obj is not None:
		candidates.append(obj)
	# Rung 2 as a second candidate, and first when NVDA only found a container.
	uiaObj = None
	if UIAHandler is not None and UIA is not None and getattr(UIAHandler, "handler", None):
		handler = UIAHandler.handler
		try:
			element = handler.clientObject.ElementFromPointBuildCache(POINT(x, y), handler.baseCacheRequest)
			if element and element.cachedProcessId != globalVars.appPid:
				uiaObj = UIA(UIAElement=element)
		except Exception:
			uiaObj = None
	if uiaObj is not None:
		if obj is not None and obj.role in CONTAINER_ROLES:
			candidates.insert(0, uiaObj)
		else:
			candidates.append(uiaObj)
	for candidate in candidates:
		try:
			info = candidate.makeTextInfo(locationHelper.Point(x, y))
		except (NotImplementedError, LookupError):
			continue
		except Exception:
			log.debugWarning("mouseReader: makeTextInfo(Point) failed", exc_info=True)
			continue
		return candidate, info, True
	# Nothing can map the point to text. Fall back to the first object with any text at all.
	for candidate in candidates:
		try:
			info = candidate.makeTextInfo(textInfos.POSITION_FIRST)
			probe = info.copy()
			probe.expand(textInfos.UNIT_STORY)
			if not isBlank(probe.text) and candidate.role not in CONTAINER_ROLES:
				return candidate, info, False
		except Exception:
			continue
	return obj, None, False
