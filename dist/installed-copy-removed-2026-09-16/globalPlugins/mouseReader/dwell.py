# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""The delay: NVDA's mouse tracking, postponed until the mouse stops.

NVDA processes mouse movement in mouseHandler.pumpAll, which calls executeMouseMoveEvent with
the latest position once per core cycle. We replace that function with one that only notes
the position and (re)starts a timer; when the mouse has been still for the delay, the timer
calls NVDA's original function with the final position. Nothing about *what* NVDA reads
changes: the same-chunk rule, the "keep reading over a blank spot" behaviour, the text unit
setting, "report object when mouse enters it" and the audio coordinates all come along,
merely later. Delay 0 is stock NVDA.

After NVDA's routine has run, the lookup ladder (ladder.py) gets its turn if NVDA had nothing
to read. Reading is paused altogether while "Read from here" is speaking.
"""

import math

import mouseHandler
import queueHandler
import speech
import wx
from logHandler import log

from . import ladder

# "Stop speech when the mouse moves": a resting hand jitters by a pixel or two, so movement
# must be at least this far from where the last reading was triggered.
MOVE_THRESHOLD_PX = 12


class DwellEngine:
	def __init__(self, settings, isReadingFromHere):
		"""settings: object with delayMs(), stopOnMove(), extraLookups() callables.
		isReadingFromHere: callable, True while the Read from here feature is speaking."""
		self._settings = settings
		self._isReadingFromHere = isReadingFromHere
		self._original = None
		self._timer = None
		self._pending = None
		self._anchor = None  # where the last reading was triggered (stop-on-move)
		self._lastLadderKey = None

	# ---- install / remove -------------------------------------------------------------

	def start(self):
		if self._original is not None:
			return
		self._original = mouseHandler.executeMouseMoveEvent
		mouseHandler.executeMouseMoveEvent = self._onMouseMove

	def stop(self):
		self._cancelTimer()
		if self._original is None:
			return
		if mouseHandler.executeMouseMoveEvent == self._onMouseMove:
			mouseHandler.executeMouseMoveEvent = self._original
		else:
			log.warning("mouseReader: executeMouseMoveEvent was re-wrapped by another add-on; not restoring")
		self._original = None

	def _cancelTimer(self):
		if self._timer is not None:
			try:
				self._timer.Stop()
			except Exception:
				pass
			self._timer = None
		self._pending = None

	# ---- the replacement for mouseHandler.executeMouseMoveEvent (main thread) ----------

	def _onMouseMove(self, x, y):
		if self._isReadingFromHere():
			return
		delay = self._settings.delayMs()
		if delay <= 0:
			self._cancelTimer()
			self._read(x, y)
			return
		self._pending = (x, y)
		if self._timer is None:
			self._timer = wx.CallLater(delay, self._fire)
		else:
			self._timer.Start(delay)

	def _fire(self):
		pending = self._pending
		self._pending = None
		if not pending:
			return
		self._read(*pending)

	def _read(self, x, y):
		self._anchor = (x, y)
		try:
			self._original(x, y)
		except Exception:
			log.exception("mouseReader: NVDA's mouse move handler failed")
		if not self._settings.extraLookups():
			return
		try:
			handled, found = ladder.climb(x, y)
		except Exception:
			log.debugWarning("mouseReader: ladder failed", exc_info=True)
			return
		if handled or found is None:
			# NVDA read it, or there is nothing here. Either way the ladder's memory resets, so
			# coming back to the same text reads it again, as NVDA itself does after a blank spot.
			self._lastLadderKey = None
			return
		if found.key == self._lastLadderKey:
			return  # still hovering in the same chunk: NVDA's own no-repeat rule, applied to the ladder
		self._lastLadderKey = found.key
		found.speak()

	# ---- raw movement from the low-level hook (hook thread) ------------------------------

	def onRawMove(self, x, y, injected):
		if not self._settings.stopOnMove():
			return
		anchor = self._anchor
		if anchor is None:
			return
		if math.hypot(x - anchor[0], y - anchor[1]) < MOVE_THRESHOLD_PX:
			return
		self._anchor = None
		if self._isReadingFromHere():
			return
		queueHandler.queueFunction(queueHandler.eventQueue, speech.cancelSpeech)

	# ---- housekeeping -----------------------------------------------------------------

	def forgetLadder(self):
		"""Let the ladder speak the same thing again (after Read from here, or a toggle)."""
		self._lastLadderKey = None
