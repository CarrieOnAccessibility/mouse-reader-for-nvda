# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""One wrapper around NVDA's low-level mouse hook, shared by the two features.

NVDA registers a single callback with winInputHook for every mouse message. We slip in
front of it: raw moves feed "stop speech when the mouse moves", and a click with the right
modifiers held becomes "Read from here" and is swallowed so the app never sees it. Everything
else goes straight through to NVDA's own callback.

Runs on NVDA's input-hook thread, never the main thread: handlers must be quick and must
queue any real work with queueHandler.
"""

import winInputHook
from logHandler import log

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202


class MouseHook:
	def __init__(self):
		self._forward = None  # NVDA's callback (or whoever was there before us)
		self._installed = False
		self.onMove = None  # (x, y, injected) -> None
		self.onButton = None  # (msg, x, y, injected) -> True to swallow
		self._swallowNextUp = False

	def install(self):
		if self._installed:
			return
		previous = winInputHook.mouseCallback
		if not previous:
			log.warning("mouseReader: NVDA's mouse callback is not set; hook not installed")
			return
		self._forward = previous
		self._installed = True
		winInputHook.setCallbacks(mouse=self._callback)

	def uninstall(self):
		if not self._installed:
			return
		self._installed = False
		self.onMove = None
		self.onButton = None
		if winInputHook.mouseCallback == self._callback:
			winInputHook.setCallbacks(mouse=self._forward)
		else:
			# Another add-on wrapped us after we wrapped NVDA. Restoring would drop it, so leave
			# the chain alone; our callback keeps forwarding and simply has no handlers.
			log.warning("mouseReader: mouse hook was wrapped by someone else; leaving the chain in place")

	def _callback(self, msg, x, y, injected):
		forward = self._forward
		try:
			if msg == WM_MOUSEMOVE:
				if self.onMove:
					self.onMove(x, y, injected)
			elif msg == WM_LBUTTONDOWN:
				if self.onButton and self.onButton(msg, x, y, injected):
					self._swallowNextUp = True
					return False
			elif msg == WM_LBUTTONUP and self._swallowNextUp:
				self._swallowNextUp = False
				return False
		except Exception:
			log.exception("mouseReader: error in mouse hook handler")
		if forward:
			return forward(msg, x, y, injected)
		return True
