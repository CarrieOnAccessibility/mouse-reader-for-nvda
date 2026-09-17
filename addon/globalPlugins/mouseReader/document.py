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
import os
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
# Roles that are a paragraph in themselves: the whole element is the reading unit. A PDF
# paragraph is one text element whose text holds line breaks between the visual lines, and the
# buffer's own paragraph unit would stop at the first of them.
_PARAGRAPH_ROLES = frozenset(
	role
	for role in (
		getattr(controlTypes.Role, name, None)
		for name in ("PARAGRAPH", "HEADING", "LISTITEM", "BLOCKQUOTE", "CAPTION", "LABEL", "TEXTFRAME")
	)
	if role is not None
)
# How many resolved boxes (element rectangle -> unit) a snapshot remembers for cheap hovering.
RECENT_BOXES = 16
# A buffer "paragraph" this long, and this many times longer than the text leaf under the
# pointer, is not a paragraph: the page's text has no structure there (a text layer of placed
# snippets), and OCR is the better source.
UNSTRUCTURED_MIN_CHARS = 400
UNSTRUCTURED_RATIO = 5
# A container with more children than this is not walked (a pdf.js text layer has hundreds).
MAX_WALK_CHILDREN = 250


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


_explained = {}  # pid -> time the UIA decision was last explained in the log


def explainUIA(hwnd):
	"""Log the inputs of NVDA's rule for reading a Chromium window through UIA (once a minute
	per process): NVDA chooses UIA when the window offers a UIA provider and its in-process
	helper has not registered from inside that process (or the process runs under another logon
	session). Diagnostic only."""
	import winUser

	try:
		pid, _tid = winUser.getWindowThreadProcessID(hwnd)
	except Exception:
		return
	now = time.time()
	if now - _explained.get(pid, 0) < 60:
		return
	_explained[pid] = now
	parts = ["pid %d" % pid, "class %s" % winUser.getClassName(hwnd)]
	try:
		import appModuleHandler

		mod = appModuleHandler.getAppModuleFromProcessID(pid)
		parts.append("app %r" % getattr(mod, "appName", "?"))
		parts.append("helper registered %s" % bool(getattr(mod, "helperLocalBindingHandle", None)))
		try:
			parts.append("different logon session %s" % bool(mod.isRunningUnderDifferentLogonSession))
		except Exception:
			parts.append("different logon session ?")
	except Exception:
		parts.append("no app module")
	try:
		from winBindings import uiAutomationCore

		parts.append("UIA provider %s" % bool(uiAutomationCore.UiaHasServerSideProvider(hwnd)))
	except Exception:
		parts.append("UIA provider ?")
	try:
		import UIAHandler

		parts.append("setting %s" % UIAHandler.AllowUiaInChromium.getConfig().name)
	except Exception:
		parts.append("setting ?")
	try:
		import psutil

		names = {os.path.basename(m.path).lower() for m in psutil.Process(pid).memory_maps()}
		parts.append("helper DLL in process %s" % ("nvdahelperremote.dll" in names))
		parts.append("UIAutomationCore in process %s" % ("uiautomationcore.dll" in names))
	except Exception:
		parts.append("modules ?")
	log.info("mouseReader: why UIA: " + ", ".join(parts))


def isUIAWindowAt(x: int, y: int, topHwnd) -> bool:
	"""Does NVDA read the window under the point through UI Automation? Only the window right
	under the pointer counts: a browser's outer frame (Chrome_WidgetWin_1) is always UIA to NVDA
	while the web content in its child window is on the normal in-process route."""
	try:
		import UIAHandler
		from ctypes.wintypes import POINT

		from winBindings import user32

		handler = UIAHandler.handler
		if handler is None:
			return False
		hwnd = user32.WindowFromPoint(POINT(x, y)) or topHwnd
		if hwnd and handler.isUIAWindow(hwnd):
			explainUIA(hwnd)
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
	"""One line of speakable text: embedded-object marks and private-use glyphs (bullet symbols
	from symbol fonts, which the voice cannot say) become spaces; whitespace collapses."""
	text = (text or "").replace("\ufffc", " ")
	if any("\ue000" <= ch <= "\uf8ff" for ch in text):
		text = "".join(" " if "\ue000" <= ch <= "\uf8ff" else ch for ch in text)
	return " ".join(text.split())


def _normalBullet(text: str) -> str:
	"""An item's odd bullet (a symbol-font glyph, an "o") becomes an ordinary one, so every item
	sounds the same and NVDA's own symbol setting decides whether "bullet" is said."""
	words = text.split(None, 1)
	if not words:
		return text
	first = words[0]
	rest = words[1] if len(words) > 1 else ""
	if "" <= first[0] <= "":
		return ("• " + (first[1:] + " " if first[1:] else "") + rest).strip()
	if first in ("o", "O") and rest:
		return "• " + rest
	return text


def _startsItem(line: str) -> bool:
	"""Does this line of a paragraph begin a list item: a bullet (including a symbol-font glyph
	or an "o"), or a numbering like "1." "2)" "a."? Same rules as the OCR side."""
	words = line.split()
	if not words:
		return False
	first = words[0]
	if "\ue000" <= first[0] <= "\uf8ff":
		return True
	if first in ocr._BULLETS or ocr._NUMBERING.match(first):
		return True
	if len(first) > 1 and first[0] in ocr._BULLETS and first[1:2].isalnum():
		return True  # the bullet glued onto the first word
	return first in ("o", "O") and len(words) > 1


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


def _isParagraphRole(obj) -> bool:
	try:
		return obj.role in _PARAGRAPH_ROLES
	except Exception:
		return False


def _location(obj):
	try:
		return obj.location
	except Exception:
		return None


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
	"""One reading unit of the document: what to say, where it is in the buffer (info; None for
	a line, which is measured in the element rather than the buffer), and its box on screen
	when known (rect)."""

	__slots__ = ("key", "text", "info", "rect")

	def __init__(self, key, text, info=None, rect=None):
		self.key = key
		self.text = text
		self.info = info
		self.rect = rect


class Unstructured:
	"""The page's text has no paragraph structure where the pointer is."""


UNSTRUCTURED = Unstructured()


class ItemSet:
	"""A paragraph element that is really a list: its items, each with the lines it spans, so
	the item under the pointer can be picked by the pointer's height within the element's box
	(the lines of a PDF paragraph are evenly spaced)."""

	__slots__ = ("items", "spans", "lineCount")

	def __init__(self, items, spans, lineCount):
		self.items = items  # [Unit]
		self.spans = spans  # [(firstLine, lastLine)] per item
		self.lineCount = lineCount

	def pick(self, y, rect):
		"""The item at the pointer's height, with its own slice of the box as its rect."""
		if rect is None or not getattr(rect, "height", 0) or self.lineCount <= 0:
			return self.items[0]
		lineHeight = rect.height / float(self.lineCount)
		lineIndex = int((y - rect.top) / lineHeight)
		lineIndex = max(0, min(self.lineCount - 1, lineIndex))
		for unit, (first, last) in zip(self.items, self.spans):
			if first <= lineIndex <= last:
				try:
					unit.rect = type(rect)(rect.left, int(rect.top + first * lineHeight), rect.width, int((last - first + 1) * lineHeight))
				except Exception:
					unit.rect = None
				return unit
		return self.items[-1]


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
		self._recent = []  # [(rectangle, level, unit)] resolved lately, until the document scrolls
		self._via = ""  # how the last lookup found its answer, for the log
		self.unstructured = False  # set when the text under the pointer proved to have no paragraphs
		self._paragraphLogs = 0
		self._walkDepth = 0  # how many levels the last walk down got before it stopped

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
		del self._recent[:]

	def _remember(self, rect, level, unit):
		"""Keep the unit's box (its own slice of a list, else the element's box) with the unit,
		so hovering inside it costs nothing."""
		if unit is None or level == ocr.LEVEL_LINE:
			return  # a line is smaller than its element's box
		if unit.rect is not None:
			rect = unit.rect
		if rect is None:
			return
		self._recent.append((rect, level, unit))
		if len(self._recent) > RECENT_BOXES:
			del self._recent[0]

	def _recalled(self, x, y, level):
		for rect, lvl, unit in reversed(self._recent):
			if lvl == level and _contains(rect, x, y):
				return unit
		return None

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

	def _positionOfLeaf(self, info, leaf):
		"""Where the text leaf sits inside the node's buffer range, found by its opening words;
		None when it cannot be told."""
		try:
			raw = leaf.name or ""
			if not raw:
				raw = leaf.makeTextInfo(textInfos.POSITION_ALL).text or ""
		except Exception:
			return None
		needle = raw.strip()[:30]
		if len(needle) < 3:
			return None
		try:
			hay = info.text or ""
		except Exception:
			return None
		index = hay.find(needle)
		if index < 0:
			words = needle.split()
			if len(words) >= 2:
				index = hay.find(" ".join(words[:2]))
		if index < 0:
			return None
		pos = info.copy()
		pos.collapse()
		try:
			pos.move(textInfos.UNIT_CHARACTER, index)
		except Exception:
			return None
		return pos

	def _splitItems(self, info):
		"""A paragraph element as list items, when at least one of its lines after the first
		begins with a bullet or a number (or the first does and another follows): [(item text,
		first line, last line, character offset of the item's start)]. None otherwise."""
		try:
			raw = (info.text or "").replace("\r\n", "\n").replace("\r", "\n")
		except Exception:
			return None
		lines = raw.split("\n")
		while lines and not lines[-1].strip():
			lines.pop()
		if len(lines) < 2:
			return None
		items = []  # [lines list, first line, last line, offset]
		offset = 0
		for index, line in enumerate(lines):
			if items and not _startsItem(line):
				items[-1][0].append(line)
				items[-1][2] = index
			else:
				items.append([[line], index, index, offset])
			offset += len(line) + 1
		if len(items) < 2:
			return None
		return [(_normalBullet(" ".join(part)), first, last, start) for part, first, last, start in items], len(lines)

	def _paragraph(self, info, node=None, leaf=None):
		"""The paragraph for a text leaf. The whole node when the node is a paragraph in itself
		(a <p>, a list item, a heading, a PDF paragraph): its text may hold line breaks between
		visual lines, and the buffer's paragraph unit would stop at the first; when its lines
		are bulleted or numbered it is a list, and an ItemSet of its items is returned instead.
		Otherwise the buffer paragraph around where the leaf sits in the node, stretched to the
		end of a multi-line text run. None when empty."""
		if node is not None and _isParagraphRole(node):
			whole = info.copy()
			bookmark = whole.bookmark
			split = self._splitItems(whole)
			if self._paragraphLogs < 6:
				self._paragraphLogs += 1
				try:
					raw = whole.text or ""
				except Exception:
					raw = ""
				log.info(
					"mouseReader: paragraph element %s: %d characters, %d line breaks, %s; text starts %r"
					% (describe(node), len(raw), raw.count("\n") + raw.count("\r"), ("split into %d items" % len(split[0])) if split else "one unit", raw[:160])
				)
			if split is not None:
				parts, lineCount = split
				units = []
				spans = []
				for index, (text, first, last, offset) in enumerate(parts):
					text = _clean(text)
					if not text:
						continue
					start = whole.copy()
					start.collapse()
					try:
						start.move(textInfos.UNIT_CHARACTER, offset)
					except Exception:
						pass
					units.append(Unit((ocr.LEVEL_PARAGRAPH, bookmark.startOffset, bookmark.endOffset, index), text, start))
					spans.append((first, last))
				if len(units) > 1:
					return ItemSet(units, spans, lineCount)
			text = _clean(whole.text)
			if not text:
				return None
			return Unit((ocr.LEVEL_PARAGRAPH, bookmark.startOffset, bookmark.endOffset), text, whole)
		start = info.copy()
		start.collapse()
		if node is not None and leaf is not None and node is not leaf:
			pos = self._positionOfLeaf(info, leaf)
			if pos is not None:
				start = pos
		para = start.copy()
		para.expand(textInfos.UNIT_PARAGRAPH)
		if node is None or node is leaf:
			try:
				if "\n" in (info.text or "").strip() and para.compareEndPoints(info, "endToEnd") < 0:
					para.setEndPoint(info, "endToEnd")  # a text run of several lines: all of it
			except Exception:
				pass
		text = _clean(para.text)
		if not text:
			return None
		if leaf is not None and len(text) >= UNSTRUCTURED_MIN_CHARS:
			try:
				leafText = _clean(leaf.name or leaf.makeTextInfo(textInfos.POSITION_ALL).text)
			except Exception:
				leafText = ""
			if len(text) > UNSTRUCTURED_RATIO * max(len(leafText), 1):
				log.info("mouseReader: the paragraph round %r would be %d characters: no paragraph structure here" % (leafText[:40], len(text)))
				return UNSTRUCTURED
		bookmark = para.bookmark
		return Unit((ocr.LEVEL_PARAGRAPH, bookmark.startOffset, bookmark.endOffset), text, para)

	def _block(self, info, node, leaf=None, x=0, y=0, rect=None):
		"""The node one up from the paragraph's: a list, a section, a table cell, a PDF page
		region. The paragraph itself when the next node up is the whole document."""
		paragraph = self._paragraph(info, node, leaf)
		if paragraph is None or paragraph is UNSTRUCTURED:
			return paragraph
		if isinstance(paragraph, ItemSet):
			inner = info
			paragraph = paragraph.pick(y, rect)
		else:
			inner = paragraph.info
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

	def unitAt(self, x, y, level, obj, trusted=False, rect=None):
		"""The unit under the pointer at the level, or None over blank space, a container or a
		control. trusted: obj was reached by walking down inside this document, so it need not
		be checked for belonging to it. rect: obj's box on screen, if already known (a list
		paragraph picks its item by the pointer's height in it). A container is never read from
		its first paragraph: in Chrome's PDF viewer the whole PDF comes back as the answer while
		Chromium is still working out the real one, and its first paragraph is Chrome's own
		"this PDF is inaccessible" status line."""
		if not _isText(obj) or (not trusted and not self.inDocument(obj)):
			return None
		if level == ocr.LEVEL_LINE:
			return self._line(x, y, obj)
		ident = self._identity(obj)
		cacheKey = (level, ident) if ident is not None else None
		unit = self._cache.get(cacheKey) if cacheKey is not None else None
		if unit is None:
			found = self._nodeInfo(obj)
			if found is None:
				return None
			info, node = found
			if self._isRoot(node):
				unit = self._elementText(obj, level)
			elif level == ocr.LEVEL_BLOCK:
				unit = self._block(info, node, obj, x, y, rect if rect is not None else _location(obj))
			else:
				unit = self._paragraph(info, node, obj)
			if cacheKey is not None and unit is not None:
				self._cache[cacheKey] = unit
		if unit is UNSTRUCTURED:
			self.unstructured = True
			return None
		if isinstance(unit, ItemSet):
			return unit.pick(y, rect if rect is not None else _location(obj))
		return unit

	def _childRects(self, obj, refresh=False):
		"""[(screen rectangle, child)] for the children of obj, remembered until a scroll."""
		ident = self._identity(obj)
		if not refresh and ident is not None and ident in self._rects:
			return self._rects[ident]
		rects = []
		try:
			count = obj.childCount
		except Exception:
			count = 0
		if count > MAX_WALK_CHILDREN:
			log.info("mouseReader: not walking a container of %d children" % count)
			children = []
		else:
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
		document moved without the wheel. Returns (deepest element reached, its rectangle);
		obj itself, with no rectangle, if nothing under the point."""
		current = obj
		currentLoc = None
		self._walkDepth = 0
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
			current, currentLoc = hit, hitLoc
			self._walkDepth += 1
		return current, currentLoc

	def _walkFrom(self, top, x, y, level):
		"""Walk down from a container by rectangles; (unit, element) when text is reached there."""
		trail = [] if self._descentLogs < DESCENT_LOG_LIMIT else None
		deep, rect = self._descend(top, x, y, trail)
		if trail is not None:
			self._descentLogs += 1
			log.info("mouseReader: walked down from %s: %s" % (describe(top), " > ".join(trail) if trail else "nowhere"))
		if deep is None or deep is top or not _isText(deep):
			return None, deep
		if rect is None:
			rect = _location(deep)
		unit = self.unitAt(x, y, level, deep, trusted=True, rect=rect)
		if unit is not None:
			self._remember(rect, level, unit)
		return unit, deep

	def unitAtSecondAsk(self, x, y, level, obj):
		"""The unit under the pointer, cheapest way first: a box resolved lately that holds the
		point; the element NVDA found, if it is text; a walk down by rectangles from it, if it
		is a container (Chrome's PDF viewer answers with the box round the PDF); and only then
		the app asked about the point again, with a walk down from that answer too. Returns
		(unit, element the unit came from)."""
		unit = self._recalled(x, y, level)
		if unit is not None:
			self._via = "remembered box"
			return unit, obj
		if not self.inDocument(obj):
			self._via = "outside the document"
			return None, obj
		if _isText(obj):
			self._via = "NVDA's element"
			rect = _location(obj)
			unit = self.unitAt(x, y, level, obj, trusted=True, rect=rect)
			if unit is not None:
				self._remember(rect, level, unit)
			return unit, obj
		if not _isContainer(obj):
			self._via = "a control"
			return None, obj
		self._via = "walk down"
		unit, deep = self._walkFrom(obj, x, y, level)
		if unit is not None:
			return unit, deep
		if self._walkDepth >= 2:
			self._via = "blank inside the page"
			return None, deep if deep is not None else obj  # the walk was inside the page: nothing there
		self._via = "second ask"
		again = objectAt(x, y)
		if again is None or again is obj:
			return None, deep if deep is not None else obj
		if _isText(again):
			rect = _location(again)
			unit = self.unitAt(x, y, level, again, rect=rect)
			if unit is not None:
				self._remember(rect, level, unit)
			return unit, again
		if _isContainer(again):
			self._via = "second ask, walk down"
			unit, deep2 = self._walkFrom(again, x, y, level)
			if unit is not None:
				return unit, deep2
			return None, deep2 if deep2 is not None else again
		return None, again

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
		if self.unstructured:
			return False
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
			log.info("mouseReader: finding the text under the mouse took %d ms (%s)" % (lookupMs, self._via))
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
		unit, source = self.unitAtSecondAsk(cx, cy, level, obj)
		if self._hoverLogs < HOVER_LOG_LIMIT:
			self._hoverLogs += 1
			log.info("mouseReader: at rest the app answered %s -> %s (%s)" % (describe(source), ("%r" % unit.text[:60]) if unit else "no text", self._via))
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
