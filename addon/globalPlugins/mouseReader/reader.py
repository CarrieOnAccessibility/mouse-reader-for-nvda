# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""The triggers, and the hover.

NVDA+control+click (caught by the low-level mouse hook, swallowed so the app never sees it)
or NVDA+control+enter recognises the window under the mouse (ocr.py) and reads the
recognised paragraph under the pointer. From then on, pointing at a spot in that window
reads the recognised paragraph there (NVDA's own mouse tracking stays out of that window
while the snapshot is fresh, so it is paragraphs everywhere); turning the wheel recognises
the window again once the wheel is still.
"""

import math
import time

import addonHandler
import config
import keyboardHandler
import queueHandler
import ui
import winUser
from logHandler import log

from . import hook, ocr

try:
	addonHandler.initTranslation()
except Exception:
	pass

REPEAT_CLICK_SECONDS = 1.5
REPEAT_CLICK_PX = 40

_CONTROL_KEYS = (winUser.VK_CONTROL, winUser.VK_LCONTROL, winUser.VK_RCONTROL)


def _keyDown(vk) -> bool:
	try:
		return bool(winUser.getKeyState(vk) & 0x8000)
	except Exception:
		return False


_SHIFT_KEYS = (winUser.VK_SHIFT, winUser.VK_LSHIFT, winUser.VK_RSHIFT)


def clickCombinationHeld():
	"""Which of our click combinations is held: "recognize" (NVDA+control), "readAll"
	(NVDA+shift) or None. The NVDA key never reaches Windows, so only NVDA's own record of
	held modifiers knows about it; control and shift are checked there and with Windows too."""
	mods = set(keyboardHandler.currentModifiers)
	if not any(keyboardHandler.isNVDAModifierKey(vk, ext) for vk, ext in mods):
		return None
	ctrl = any(vk in _CONTROL_KEYS for vk, ext in mods) or _keyDown(winUser.VK_CONTROL)
	shift = any(vk in _SHIFT_KEYS for vk, ext in mods) or _keyDown(winUser.VK_SHIFT)
	if ctrl and not shift:
		return "recognize"
	if shift and not ctrl:
		return "readAll"
	return None


class Reader:
	def __init__(self, settings):
		"""settings: object with clickEnabled() and hoverLevel() callables."""
		self._settings = settings
		self._ocr = ocr.OcrReader(settings.hoverLevel)
		self._lastClick = None  # (x, y, time) of the last recognising click

	def shutdown(self):
		self._ocr.shutdown()

	# ---- triggers ---------------------------------------------------------------------

	def onButton(self, msg, x, y, injected) -> bool:
		"""Low-level hook (hook thread): True to swallow the click and recognise at that point.
		Any other click is the user doing something to the window (opening another channel,
		say), after which the recognised text is stale: the snapshot is dropped."""
		if (
			msg == hook.WM_LBUTTONDOWN
			and self._settings.clickEnabled()
			and not (injected and config.conf["mouse"]["ignoreInjectedMouseInput"])
		):
			combination = clickCombinationHeld()
			if combination == "recognize":
				log.info("mouseReader: NVDA+control+click at (%d, %d)" % (x, y))
				queueHandler.queueFunction(queueHandler.eventQueue, self.recognize, x, y)
				return True
			if combination == "readAll":
				log.info("mouseReader: NVDA+shift+click at (%d, %d)" % (x, y))
				queueHandler.queueFunction(queueHandler.eventQueue, self.readAll, x, y)
				return True
		queueHandler.queueFunction(queueHandler.eventQueue, self.forget, "click")
		return False

	def forget(self, why=""):
		"""Drop the snapshot (and stop reading all): the user clicked or typed, so the window
		has probably changed."""
		if self._ocr.snapshot is not None:
			log.info("mouseReader: snapshot dropped (%s)" % why)
			self._ocr.forget()
		else:
			ocr.stopReadingAll()

	def readAllAtMouse(self):
		x, y = winUser.getCursorPos()
		self.readAll(x, y)

	def readAll(self, x: int, y: int):
		"""Main thread. Read on from the point, recognising the window first if need be."""
		self._lastClick = (x, y, time.time())
		if not self._ocr.readAll(x, y):
			ui.message(_("Nothing under the mouse to recognize"))

	def onGesture(self, gesture) -> bool:
		"""inputCore.decide_executeGesture: any key press other than a bare modifier means the
		user is typing or commanding, so the recognised text may be stale. Always allows the
		gesture."""
		try:
			if not getattr(gesture, "isModifier", False) and self._ocr.snapshot is not None:
				self.forget("key press")
		except Exception:
			log.debugWarning("mouseReader: gesture check failed", exc_info=True)
		return True

	def onWheel(self, x, y, injected):
		"""Low-level hook (hook thread): the wheel turned. A recognised window may have scrolled."""
		queueHandler.queueFunction(queueHandler.eventQueue, self._ocr.wheelScrolled, x, y)

	def recognizeAtMouse(self):
		x, y = winUser.getCursorPos()
		self.recognize(x, y)

	def recognize(self, x: int, y: int):
		"""Main thread. Recognise the window under the point and read the paragraph there."""
		last = self._lastClick
		now = time.time()
		if (
			last is not None
			and now - last[2] < REPEAT_CLICK_SECONDS
			and math.hypot(x - last[0], y - last[1]) < REPEAT_CLICK_PX
		):
			log.info("mouseReader: repeat click; ignored")
			return
		self._lastClick = (x, y, now)
		if not self._ocr.start(x, y):
			# Translators: message when there is no window under the mouse to recognise.
			ui.message(_("Nothing under the mouse to recognize"))

	# ---- hover ------------------------------------------------------------------------

	def onMouseMove(self, x: int, y: int) -> bool:
		"""Every mouse move NVDA reports (after any delay add-on has had its say). True when a
		fresh snapshot covers the point: it has read the paragraph there, or deliberately stayed
		quiet (same paragraph, blank space), and NVDA's own mouse tracking should stay out so
		the whole window behaves the same way."""
		return self._ocr.claim(x, y)
