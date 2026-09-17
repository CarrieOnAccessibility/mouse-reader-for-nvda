# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""One wrapper around NVDA's low-level mouse hook, shared by the two features.

NVDA registers a single callback with winInputHook for every mouse message. We slip in
front of it: a click with the right modifiers held becomes a Mouse Reader action and is
swallowed so the app never sees it, other clicks and the wheel are reported, and everything
goes straight through to NVDA's own callback.

Runs on NVDA's input-hook thread, never the main thread: handlers must be quick and must
queue any real work with queueHandler.
"""

import winInputHook
from logHandler import log

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_XBUTTONDOWN = 0x020B
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E


class MouseHook:
	def __init__(self):
		self._forward = None  # NVDA's callback (or whoever was there before us)
		self._installed = False
		self.onMove = None  # (x, y, injected) -> None
		self.onButton = None  # (msg, x, y, injected) -> True to swallow
		self.onWheel = None  # (x, y, injected) -> None
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
		self.onWheel = None
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
			elif msg in (WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN):
				if self.onButton:
					self.onButton(msg, x, y, injected)  # never swallowed; reported so a snapshot can be dropped
			elif msg == WM_LBUTTONUP and self._swallowNextUp:
				self._swallowNextUp = False
				return False
			elif msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
				if self.onWheel:
					self.onWheel(x, y, injected)
		except Exception:
			log.exception("mouseReader: error in mouse hook handler")
		if forward:
			return forward(msg, x, y, injected)
		return True
