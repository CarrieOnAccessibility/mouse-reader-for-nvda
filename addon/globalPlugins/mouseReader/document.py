# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""A browse-mode document under the mouse, read from NVDA's own copy of it instead of a picture.

A web page, or a PDF open in Chrome or Edge, is text NVDA has already taken in (the buffer
that browse mode reads from): the words are exact whatever the font looks like on screen, so
there is nothing for OCR to add and plenty for it to get wrong. When NVDA+control+click lands
in such a document, the document answers the mouse instead of a snapshot picture.

Finding the text under the pointer goes the way NVDA's own mouse tracking goes: the element
under the pointer (NVDA hands it to every mouse move; the click asks for it once), then the
nearest element from there upwards that the buffer knows as a node (text leaves are folded
into their parent's text), then the buffer paragraph that node begins. No UIA, and nothing
that was not already happening on every mouse move with tracking on.

Asking twice, then walking down. Chromium answers "what is under this point?" from a cached
guess the first time it is asked about a point, and works out the exact answer in the
background, ready for the next ask at the same point; so a container answer is asked about
again, and the mouse resting on a spot asks again too. Inside Chrome's PDF viewer, though,
every answer is the box that holds the PDF (a section with one child): the hit test never
goes inside until the browse caret has been moved in there by keyboard. The elements inside
still know their own rectangles, so from that box the add-on walks down itself: which child
holds the point, then which of its children, until it reaches a paragraph.

Levels. Paragraph is the buffer's paragraph (a <p>, a list item, a heading, a PDF paragraph).
Line is the visual line of the element under the pointer, as the app reports it. Block is
the node one up from the paragraph's (a list, a section, a table cell, a PDF page region).

Windows NVDA reads through UI Automation are left alone entirely: for Chromium that is NVDA's
fallback for when it could not hook into the browser process (typically after NVDA was
restarted while the browser stayed open), it is slow to answer, and merely walking its
elements froze NVDA for seconds. Such a window goes straight to OCR, and the log says so;
exiting and reopening the browser puts it back on the normal route.

NVDA builds its copy of a page only when the page gets focus, so right after NVDA starts, or
for a window that has not been focused since, there is none. Then the add-on asks NVDA to
build it, exactly as focus would, waits for it to load, and carries on. OCR is only for a
window that is not a document at all, or one whose copy never finishes loading.

Nothing is frozen: hovering asks the live document each time, so scrolling needs no second
recognition. Blank space inside the document (margins, gaps between paragraphs) stays quiet,
as it does over a picture; controls (a button, an edit field) and anything outside the
document, such as the toolbar above it, are left to NVDA to read its own way.
"""

import math
import time

import addonHandler
import api
import controlTypes
import speech
import textInfos
import ui
import winUser
import wx
from logHandler import log
from speech import sayAll

from . import ocr

try:
	addonHandler.initTranslation()
except Exception:
	pass

# How far up from the element under the pointer to look for an element the buffer knows.
MAX_ANCESTORS = 8
# Lookups slower than this are logged, to tell a slow app from a slow voice.
SLOW_LOOKUP_MS = 150
# A click whose first answer is a container asks again this much later.
CLICK_RETRY_MS = 120
# The mouse resting on a spot (moved less than REST_PX since) asks again this much later.
REST_MS = 200
REST_PX = 4
# Hover misses logged per snapshot, so the log shows what Chromium answered without flooding.
HOVER_LOG_LIMIT = 8
# How many levels down to walk by rectangles from the container the app stopped at, and how
# many of those walks to log per snapshot.
MAX_DESCENT = 10
DESCENT_LOG_LIMIT = 4
# How far up from the element under the mouse to look for the page it is in, and for how long.
MAX_ROOT_SEARCH = 25
ROOT_SEARCH_BUDGET_MS = 300

# Roles whose element, or whose text leaf, counts as text under the pointer. Anything else with
# children is a container (blank space); anything else without children is a control.
_TEXT_ROLES = frozenset(
	role
	for role in (
		getattr(controlTypes.Role, name, None)
		for name in ("STATICTEXT", "PARAGRAPH", "HEADING", "LINK", "LISTITEM", "TEXTFRAME", "BLOCKQUOTE", "CAPTION", "LABEL")
	)
	if role is not None
)


def objectAt(x: int, y: int):
	"""The NVDA object under the point, found the way NVDA's mouse tracking finds it (UIA only
	where NVDA itself already uses UIA for that window)."""
	try:
		return api.getDesktopObject().objectFromPoint(x, y)
	except Exception:
		log.debugWarning("mouseReader: objectFromPoint failed", exc_info=True)
		return None


def describe(obj) -> str:
	"""One line about an element, for the log: role, name, children."""
	if obj is None:
		return "nothing"
	try:
		role = obj.role.name if hasattr(obj.role, "name") else str(obj.role)
	except Exception:
		role = "?"
	try:
		name = (obj.name or "")[:40]
	except Exception:
		name = ""
	try:
		children = obj.childCount
	except Exception:
		children = "?"
	return "%s %r (%s children)" % (role, name, children)


class Loading:
	"""NVDA is building its copy of the page under the mouse; ask again shortly."""

	def __init__(self, ti):
		self.ti = ti

	def ready(self) -> bool:
		try:
			return bool(self.ti.isAlive) and not getattr(self.ti, "isLoading", False) and bool(self.ti.isReady)
		except Exception:
			return False

	def alive(self) -> bool:
		try:
			return bool(self.ti.isAlive)
		except Exception:
			return False


def _isBuffer(ti) -> bool:
	from virtualBuffers import VirtualBuffer

	return isinstance(ti, VirtualBuffer)


def isUIAWindowAt(x: int, y: int, topHwnd) -> bool:
	"""Does NVDA read the window under the point through UI Automation?"""
	try:
		import UIAHandler
		from ctypes.wintypes import POINT

		from winBindings import user32

		handler = UIAHandler.handler
		if handler is None:
			return False
		child = user32.WindowFromPoint(POINT(x, y))
		for hwnd in (child, topHwnd):
			if hwnd and handler.isUIAWindow(hwnd):
				return True
		return False
	except Exception:
		log.debugWarning("mouseReader: could not tell whether the window uses UIA", exc_info=True)
		return False


def bufferOf(obj):
	"""The browse-mode copy of the page holding obj: a ready in-process buffer, or Loading while
	NVDA is still building it, or None when there is none (or it is not an in-process one)."""
	if obj is None:
		return None
	try:
		ti = obj.treeInterceptor
		if ti is None:
			return None
		if not _isBuffer(ti):
			log.info("mouseReader: the document under the mouse is not an in-process buffer (%s); using OCR" % type(ti).__name__)
			return None
		loading = Loading(ti)
		return ti if loading.ready() else loading
	except Exception:
		log.debugWarning("mouseReader: could not look for a document under the mouse", exc_info=True)
		return None


def buildBufferFor(obj):
	"""No copy of the page yet: ask NVDA to build one for the outermost page holding obj, the
	way it does when focus enters a page. The buffer (probably still loading), or None when
	NVDA would not treat that window as a document."""
	import treeInterceptorHandler

	started = time.time()
	root = None
	o = obj
	for _ in range(MAX_ROOT_SEARCH):
		if o is None:
			break
		try:
			if o.treeInterceptorClass is not None:
				root = o
		except Exception:
			pass
		if (time.time() - started) * 1000 > ROOT_SEARCH_BUDGET_MS:
			log.info("mouseReader: looking for the page root took too long (%d ms); using OCR" % int((time.time() - started) * 1000))
			return None
		try:
			o = o.parent
		except Exception:
			break
	if root is None:
		return None
	try:
		ti = treeInterceptorHandler.update(root)
	except Exception:
		log.debugWarning("mouseReader: NVDA could not build a document for the page under the mouse", exc_info=True)
		return None
	if ti is None or not _isBuffer(ti):
		return None
	return ti


def documentAt(x: int, y: int, build: bool = True, known=None):
	"""What is under the point: (DocumentSnapshot, element) for a ready browse-mode document,
	a Loading while NVDA builds its copy of the page (asked for when build is True), or None
	when the point is not in a document NVDA can read. known: a buffer already asked for, to
	look at again instead of searching (while it loads, the element does not yet say it is in
	it)."""
	found = ocr.windowAt(x, y)
	if found is None:
		return None
	if known is None and isUIAWindowAt(x, y, found[0]):
		log.info(
			"mouseReader: NVDA reads the window under the mouse through UIA (for a browser: it could not hook into "
			"the process, usually after an NVDA restart; exit and reopen the browser to fix); using OCR"
		)
		return None
	obj = objectAt(x, y)
	if known is not None:
		loading = Loading(known)
		if not loading.alive():
			return None
		if not loading.ready():
			return loading
		ti = known
	else:
		ti = bufferOf(obj)
		if ti is None and build and obj is not None:
			ti = buildBufferFor(obj)
			if ti is None:
				log.info("mouseReader: no document under the mouse (%s); using OCR" % describe(obj))
				return None
			log.info("mouseReader: NVDA had not loaded the page under the mouse yet; asked it to (%s)" % type(ti).__name__)
			loading = Loading(ti)
			if not loading.ready():
				return loading
		if ti is None:
			return None
		if isinstance(ti, Loading):
			return ti
	hwnd, rect = found
	return DocumentSnapshot(hwnd, rect, ti), obj


def _clean(text) -> str:
	return " ".join((text or "").replace("\ufffc", " ").split())


def _isText(obj) -> bool:
	try:
		return obj.role in _TEXT_ROLES
	except Exception:
		return False


def _isContainer(obj) -> bool:
	try:
		return obj.childCount > 0
	except Exception:
		return False


def _contains(loc, x, y) -> bool:
	try:
		return (
			loc is not None
			and loc.width > 0
			and loc.height > 0
			and loc.left <= x < loc.left + loc.width
			and loc.top <= y < loc.top + loc.height
		)
	except Exception:
		return False


def _rectText(loc) -> str:
	try:
		return "%d,%d %dx%d" % (loc.left, loc.top, loc.width, loc.height)
	except Exception:
		return "no rect"


class Unit:
	"""One reading unit of the document: what to say, and where it is (info: the buffer range;
	None for a line, which is measured in the element rather than the buffer)."""

	__slots__ = ("key", "text", "info")

	def __init__(self, key, text, info=None):
		self.key = key
		self.text = text
		self.info = info


class DocumentSnapshot(ocr.WindowSnapshot):
	"""A browse-mode document answering the mouse. Same shape as the OCR snapshot, but live."""

	live = True

	def __init__(self, hwnd, rect, ti):
		super().__init__(hwnd, rect)
		self._ti = ti
		self._lastSpoken = None  # key of the unit read last
		self._cache = {}  # (level, element id) -> Unit, for the life of the snapshot
		self._restTimer = None
		self._restAt = None  # (x, y, level) of the last container answer
		self._closed = False
		self._hoverLogs = 0
		self._descentLogs = 0
		self._rects = {}  # element id -> [(rectangle, child)], until the document scrolls

	@property
	def kind(self) -> str:
		return type(self._ti).__name__

	def close(self):
		"""The snapshot is being dropped or replaced: no timer of its own may speak later."""
		self._closed = True
		if self._restTimer is not None:
			try:
				self._restTimer.Stop()
			except Exception:
				pass
			self._restTimer = None

	def isFresh(self) -> bool:
		if self._closed or not super().isFresh():
			return False
		try:
			return bool(self._ti.isAlive)
		except Exception:
			return False

	def scrolled(self):
		"""The wheel turned over the document: its elements have moved, so their rectangles are
		fetched afresh next time."""
		self._rects.clear()

	# ---- what the pointer is over ----------------------------------------------------------

	def inDocument(self, obj) -> bool:
		"""Is the element under the pointer part of this document (not, say, the toolbar above it)?"""
		if obj is None:
			return False
		try:
			return obj.treeInterceptor is self._ti
		except Exception:
			return False

	def claims(self, obj) -> bool:
		"""Should the document answer this hover at all? Its text and its blank space, yes; a
		control, or anything outside the document, no: NVDA reads those its own way."""
		return self.inDocument(obj) and (_isText(obj) or _isContainer(obj))

	@staticmethod
	def _identity(obj):
		try:
			return obj.IA2UniqueID
		except Exception:
			return None

	def _isRoot(self, obj) -> bool:
		try:
			return obj == self._ti.rootNVDAObject
		except Exception:
			return False

	def _nodeInfo(self, obj):
		"""(buffer range, element) of the nearest element, from obj upwards, that the buffer
		knows as a node; None if none within reach."""
		o = obj
		for _ in range(MAX_ANCESTORS):
			if o is None:
				return None
			try:
				return self._ti.makeTextInfo(o), o
			except LookupError:
				pass
			except Exception:
				log.debugWarning("mouseReader: buffer lookup failed", exc_info=True)
				return None
			try:
				o = o.parent
			except Exception:
				return None
		return None

	def _paragraph(self, info):
		"""The buffer paragraph at the start of info, or None when it is empty."""
		info = info.copy()
		info.collapse()
		info.expand(textInfos.UNIT_PARAGRAPH)
		text = _clean(info.text)
		if not text:
			return None
		bookmark = info.bookmark
		return Unit((ocr.LEVEL_PARAGRAPH, bookmark.startOffset, bookmark.endOffset), text, info)

	def _block(self, info, node):
		"""The node one up from the paragraph's: a list, a section, a table cell, a PDF page
		region. The paragraph itself when the next node up is the whole document."""
		paragraph = self._paragraph(info)
		if paragraph is None:
			return None
		o = node
		for _ in range(4):
			try:
				o = o.parent
			except Exception:
				return paragraph
			if o is None or self._isRoot(o):
				return paragraph
			try:
				outer = self._ti.makeTextInfo(o)
			except LookupError:
				continue
			except Exception:
				return paragraph
			inner = paragraph.info
			startCmp = outer.compareEndPoints(inner, "startToStart")
			endCmp = outer.compareEndPoints(inner, "endToEnd")
			if startCmp <= 0 and endCmp >= 0:
				if startCmp == 0 and endCmp == 0:
					continue  # the same text, one wrapper up; keep looking for something bigger
				text = _clean(outer.text)
				if text:
					bookmark = outer.bookmark
					return Unit((ocr.LEVEL_BLOCK, bookmark.startOffset, bookmark.endOffset), text, outer)
		return paragraph

	def _line(self, x, y, obj):
		"""The visual line of the element under the point, as the app reports it."""
		try:
			info = obj.makeTextInfo(textInfos.Point(x, y))
			info.expand(textInfos.UNIT_LINE)
		except (NotImplementedError, LookupError, RuntimeError):
			return None
		except Exception:
			log.debugWarning("mouseReader: line lookup failed", exc_info=True)
			return None
		text = _clean(info.text)
		if not text:
			return None
		try:
			start = info.bookmark.startOffset
		except Exception:
			start = 0
		return Unit((ocr.LEVEL_LINE, self._identity(obj), start), text)

	def _elementText(self, obj, level):
		"""All the element's own text: for a text leaf hanging straight off the document root,
		where the buffer has no smaller node to measure a paragraph in."""
		try:
			text = _clean(obj.makeTextInfo(textInfos.POSITION_ALL).text)
		except Exception:
			return None
		if not text:
			return None
		return Unit((level, self._identity(obj)), text)

	def unitAt(self, x, y, level, obj):
		"""The unit under the pointer at the level, or None over blank space, a container or a
		control. A container is never read from its first paragraph: in Chrome's PDF viewer the
		whole PDF comes back as the answer while Chromium is still working out the real one, and
		its first paragraph is Chrome's own "this PDF is inaccessible" status line."""
		if not self.inDocument(obj) or not _isText(obj):
			return None
		if level == ocr.LEVEL_LINE:
			return self._line(x, y, obj)
		ident = self._identity(obj)
		cacheKey = (level, ident) if ident is not None else None
		if cacheKey is not None and cacheKey in self._cache:
			return self._cache[cacheKey]
		found = self._nodeInfo(obj)
		if found is None:
			return None
		info, node = found
		if self._isRoot(node):
			unit = self._elementText(obj, level)
		elif level == ocr.LEVEL_BLOCK:
			unit = self._block(info, node)
		else:
			unit = self._paragraph(info)
		if cacheKey is not None and unit is not None:
			self._cache[cacheKey] = unit
		return unit

	def _childRects(self, obj, refresh=False):
		"""[(screen rectangle, child)] for the children of obj, remembered until a scroll."""
		ident = self._identity(obj)
		if not refresh and ident is not None and ident in self._rects:
			return self._rects[ident]
		rects = []
		try:
			children = obj.children
		except Exception:
			children = []
		for child in children:
			try:
				loc = child.location
			except Exception:
				loc = None
			rects.append((loc, child))
		if ident is not None:
			self._rects[ident] = rects
		return rects

	def _descend(self, obj, x, y, trail=None):
		"""From a container the app's hit test stopped at, walk down by the children's own screen
		rectangles to the deepest element under the point. A child that is text wins over one
		that is not; a sole child is entered whatever its rectangle says (it fills its parent);
		rectangles that place no child under the point are fetched once more in case the
		document moved without the wheel. Returns the deepest element reached (obj itself if
		nothing under the point)."""
		current = obj
		for _ in range(MAX_DESCENT):
			if _isText(current) and not _isContainer(current):
				break
			rects = self._childRects(current)
			hit = hitLoc = None
			for attempt in range(2):
				for loc, child in rects:
					if _contains(loc, x, y):
						hit, hitLoc = child, loc
						if _isText(child):
							break
				if hit is not None or attempt == 1:
					break
				if len(rects) == 1:
					hitLoc, hit = rects[0]
					if trail is not None:
						trail.append("(sole child, entered anyway)")
					break
				rects = self._childRects(current, refresh=True)
			if hit is None:
				if trail is not None:
					trail.append("none of %d children holds the point: %s" % (len(rects), ", ".join("%s %s" % (describe(c), _rectText(l)) for l, c in rects[:6])))
				break
			if trail is not None:
				trail.append("%s %s" % (describe(hit), _rectText(hitLoc)))
			current = hit
		return current

	def unitAtSecondAsk(self, x, y, level, obj):
		"""The unit under the pointer. When the element NVDA found there is a container, the app
		is asked about the point again (the second answer is the exact one, see the module note);
		when that is a container too, the add-on walks down by rectangles from it. Returns
		(unit, element the unit came from)."""
		unit = self.unitAt(x, y, level, obj)
		if unit is not None or not self.inDocument(obj) or not _isContainer(obj):
			return unit, obj
		again = objectAt(x, y)
		if again is not None and again is not obj and _isText(again):
			unit = self.unitAt(x, y, level, again)
			if unit is not None:
				return unit, again
		top = again if (again is not None and _isContainer(again)) else obj
		trail = [] if self._descentLogs < DESCENT_LOG_LIMIT else None
		deep = self._descend(top, x, y, trail)
		if trail is not None:
			self._descentLogs += 1
			log.info("mouseReader: walked down from %s: %s" % (describe(top), " > ".join(trail) if trail else "nowhere"))
		if deep is not None and deep is not top and _isText(deep):
			return self.unitAt(x, y, level, deep), deep
		return None, deep if deep is not None else obj

	# ---- speaking -------------------------------------------------------------------------

	def _speak(self, unit):
		self._lastSpoken = unit.key
		ocr.speakLines([unit.text])

	def _isFocused(self) -> bool:
		"""Is this the document with the system focus, in browse mode? Then the browse-mode caret
		can be moved to what was clicked, and reading on can move it, which brings the page along."""
		try:
			ti = api.getFocusObject().treeInterceptor
			return ti is self._ti and not ti.passThrough
		except Exception:
			return False

	def _moveCaretTo(self, unit):
		"""Put the browse-mode caret at the start of the unit, so the keyboard carries on from
		what was clicked (and Chromium refreshes its idea of where that text is)."""
		if unit.info is None or not self._isFocused():
			return
		try:
			start = unit.info.copy()
			start.collapse()
			self._ti.selection = start
			log.info("mouseReader: browse caret moved to the clicked paragraph")
		except Exception:
			log.debugWarning("mouseReader: could not move the browse caret", exc_info=True)

	def speakAt(self, x, y, level, obj, retry=True) -> bool:
		"""For a click: read the unit under the point and move the browse caret there. When the
		app answered with a container and retry is on, ask again in a moment; the caller hears
		True either way. False only when there is nothing to read and no retry."""
		unit, source = self.unitAtSecondAsk(x, y, level, obj)
		log.info(
			"mouseReader: click landed on %s%s -> %s"
			% (describe(obj), "" if source is obj else ", second ask %s" % describe(source), ("%r" % unit.text[:60]) if unit else "no text")
		)
		if unit is not None:
			self._speak(unit)
			self._moveCaretTo(unit)
			return True
		if retry and self.inDocument(obj) and _isContainer(obj):
			wx.CallLater(CLICK_RETRY_MS, self._retryClick, x, y, level)
			return True
		return False

	def _retryClick(self, x, y, level):
		if not self.isFresh():
			return
		obj = objectAt(x, y)
		if not self.speakAt(x, y, level, obj, retry=False):
			ui.message(ocr.NO_TEXT_UNDER_MOUSE)

	def hover(self, x, y, level, obj=None) -> bool:
		"""Read the unit under the pointer when it is a different one from the unit read last.
		Blank space and the unit just read leave things alone. A container answer arms one more
		ask once the mouse has rested. True when something was read."""
		started = time.time()
		unit, source = self.unitAtSecondAsk(x, y, level, obj)
		lookupMs = int((time.time() - started) * 1000)
		if lookupMs > SLOW_LOOKUP_MS:
			log.info("mouseReader: finding the text under the mouse took %d ms" % lookupMs)
		if unit is None:
			if self.inDocument(obj) and _isContainer(obj):
				if self._hoverLogs < HOVER_LOG_LIMIT:
					self._hoverLogs += 1
					log.info("mouseReader: hover over %s%s; will ask again at rest" % (describe(obj), "" if source is obj else ", second ask %s" % describe(source)))
				self._armRest(x, y, level)
			return False
		if unit.key == self._lastSpoken:
			return False
		self._speak(unit)
		return True

	def _armRest(self, x, y, level):
		if self._closed:
			return
		self._restAt = (x, y, level)
		try:
			if self._restTimer is None:
				self._restTimer = wx.CallLater(REST_MS, self._onRest)
			else:
				self._restTimer.Start(REST_MS)
		except Exception:
			log.debugWarning("mouseReader: could not arm the rest timer", exc_info=True)

	def _onRest(self):
		"""The mouse has rested since the last container answer: ask about the spot again."""
		if not self.isFresh() or ocr.isReadingAll() or self._restAt is None:
			return
		x, y, level = self._restAt
		cx, cy = winUser.getCursorPos()
		if math.hypot(cx - x, cy - y) > REST_PX or not self.covers(cx, cy):
			return
		obj = objectAt(cx, cy)
		unit = self.unitAt(cx, cy, level, obj)
		if self._hoverLogs < HOVER_LOG_LIMIT:
			self._hoverLogs += 1
			log.info("mouseReader: at rest the app answered %s -> %s" % (describe(obj), ("%r" % unit.text[:60]) if unit else "no text"))
		if unit is None or unit.key == self._lastSpoken:
			return
		self._speak(unit)

	def readAllFrom(self, x, y, level, obj=None) -> bool:
		"""Start NVDA's Say All at the paragraph under the point: from the browse-mode caret when
		this document has focus, so the page scrolls along; else from the review cursor, without
		touching the caret. False if there is nothing to read from."""
		if obj is None:
			obj = objectAt(x, y)
		unit, source = self.unitAtSecondAsk(x, y, ocr.LEVEL_PARAGRAPH, obj)
		log.info("mouseReader: read on from %s -> %s" % (describe(source), ("%r" % unit.text[:60]) if unit else "no text"))
		if unit is None or unit.info is None:
			return False
		start = unit.info.copy()
		start.collapse()
		try:
			speech.cancelSpeech()
			speech.pauseSpeech(False)  # shift in NVDA+shift+click can leave the voice paused
			focused = self._isFocused()
			if focused:
				self._ti.selection = start
				sayAll.SayAllHandler.readText(sayAll.CURSOR.CARET, startedFromScript=True)
			else:
				if not api.setReviewPosition(start, clearNavigatorObject=True):
					return False
				sayAll.SayAllHandler.readText(sayAll.CURSOR.REVIEW, startedFromScript=True)
			self._lastSpoken = unit.key
			log.info("mouseReader: reading on through the document from the %s" % ("caret" if focused else "review cursor"))
			return True
		except Exception:
			log.exception("mouseReader: could not start reading on through the document")
			return False
