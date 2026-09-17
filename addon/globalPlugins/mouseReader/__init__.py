# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
# Mouse Reader add-on for NVDA: global plugin entry point.
#
# "Read from here": NVDA+control+click (or NVDA+shift+R) reads continuously from the text
# under the mouse, with skip-back / skip-forward commands (readfrom.py) and an OCR fallback
# whose snapshot also reads paragraphs on hover (ocr.py).
# This module is the NVDA plumbing: config, the Settings category, the commands, and the
# low-level mouse hook that catches the click (hook.py).
#
# The delay before mouse tracking reads is a separate add-on, Mouse Echo Delay.

import addonHandler
import config
import globalPluginHandler
from gui import guiHelper
from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
from logHandler import log
from scriptHandler import script
import wx

from . import hook, readfrom
import speech

try:
	addonHandler.initTranslation()
except Exception:  # not running from an installed add-on (e.g. scratchpad)
	pass

CONF_SECTION = "mouseReader"

START_CHOICES = (
	# Translators: a choice for where Read from here starts reading.
	("paragraph", _("Beginning of the paragraph")),
	# Translators: a choice for where Read from here starts reading.
	("line", _("Beginning of the line")),
	# Translators: a choice for where Read from here starts reading.
	("word", _("Beginning of the word")),
	# Translators: a choice for where Read from here starts reading.
	("point", _("The exact spot")),
)

config.conf.spec[CONF_SECTION] = {
	"readFromClick": "boolean(default=True)",
	"readFromStart": "option(%s, default='paragraph')" % ", ".join("'%s'" % key for key, _label in START_CHOICES),
	"readFromOcr": "boolean(default=True)",
}


class Settings:
	"""Live view of the add-on's configuration (follows NVDA's configuration profiles)."""

	@staticmethod
	def _section():
		return config.conf[CONF_SECTION]

	def readFromClick(self) -> bool:
		return bool(self._section()["readFromClick"])

	def readFromStart(self) -> str:
		return self._section()["readFromStart"]

	def readFromOcr(self) -> bool:
		return bool(self._section()["readFromOcr"])


class MouseReaderSettingsPanel(SettingsPanel):
	# Translators: title of the Mouse Reader category in the NVDA Settings dialog.
	title = _("Mouse Reader")
	helpId = ""

	def makeSettings(self, settingsSizer):
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		section = config.conf[CONF_SECTION]
		self.readFromClickCheckBox = sHelper.addItem(
			# Translators: label of the check box that enables NVDA+control+click to read from that spot.
			wx.CheckBox(self, label=_("&Read from here with NVDA+control+click, recognize with NVDA+control+shift+click"))
		)
		self.readFromClickCheckBox.SetValue(bool(section["readFromClick"]))
		self.readFromStartChoice = sHelper.addLabeledControl(
			# Translators: label of the dropdown that picks where Read from here starts reading.
			_("Read from here &starts at:"),
			wx.Choice,
			choices=[label for _key, label in START_CHOICES],
		)
		keys = [key for key, _label in START_CHOICES]
		current = section["readFromStart"]
		self.readFromStartChoice.SetSelection(keys.index(current) if current in keys else 0)
		self.readFromOcrCheckBox = sHelper.addItem(
			# Translators: label of the check box that lets Read from here OCR the window when no text is found.
			wx.CheckBox(self, label=_("Use &OCR when Read from here finds no text"))
		)
		self.readFromOcrCheckBox.SetValue(bool(section["readFromOcr"]))

	def onSave(self):
		section = config.conf[CONF_SECTION]
		section["readFromClick"] = self.readFromClickCheckBox.IsChecked()
		section["readFromStart"] = START_CHOICES[self.readFromStartChoice.GetSelection()][0]
		section["readFromOcr"] = self.readFromOcrCheckBox.IsChecked()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	# Translators: category of the add-on's commands in the Input Gestures dialog.
	scriptCategory = _("Mouse Reader")

	def __init__(self):
		super().__init__()
		self.settings = Settings()
		self.readFrom = readfrom.ReadFromHere(self.settings)
		self.hook = hook.MouseHook()
		self.hook.onButton = self.readFrom.onButton
		NVDASettingsDialog.categoryClasses.append(MouseReaderSettingsPanel)
		try:
			self.hook.install()
		except Exception:
			log.exception("mouseReader: could not install the mouse hook")

	def terminate(self):
		try:
			NVDASettingsDialog.categoryClasses.remove(MouseReaderSettingsPanel)
		except ValueError:
			pass
		try:
			self.hook.uninstall()
		except Exception:
			log.exception("mouseReader: could not remove the mouse hook")
		super().terminate()

	def event_mouseMove(self, obj, nextHandler, x, y):
		try:
			self.readFrom.onMouseMove(obj, x, y)
		except Exception:
			log.debugWarning("mouseReader: hover failed", exc_info=True)
		nextHandler()

	# ---- commands -----------------------------------------------------------------------

	@staticmethod
	def _unpause():
		"""Shift on its own pauses NVDA's speech, and Shift is part of these shortcuts: NVDA's
		own state is reset by the cancel that follows, but the voice driver can stay paused
		(seen with the 32-bit voice bridge). Tell it to resume before reading starts."""
		try:
			speech.pauseSpeech(False)
		except Exception:
			log.debugWarning("mouseReader: could not resume speech", exc_info=True)

	@script(
		# Translators: description of the command that reads continuously from the mouse position.
		description=_("Reads from the text under the mouse onwards (same as NVDA+control+click)"),
		gesture="kb:NVDA+shift+r",
	)
	def script_readFromMouse(self, gesture):
		self._unpause()
		self.readFrom.readFromMouse()

	@script(
		# Translators: description of the command that OCRs the window under the mouse for hovering.
		description=_("Recognizes the text in the window under the mouse, for reading paragraphs by hovering (same as NVDA+control+shift+click)"),
		gesture="kb:NVDA+control+shift+r",
	)
	def script_recognizeWindow(self, gesture):
		self._unpause()
		self.readFrom.recognizeAtMouse()

	@script(
		# Translators: description of the command that moves the review cursor to the mouse position.
		description=_("Moves the review cursor to the text under the mouse without reading"),
	)
	def script_moveReviewToMouse(self, gesture):
		self._unpause()
		self.readFrom.moveReviewToMouse()

	@script(
		# Translators: description of the command that skips back a paragraph while Read from here is reading.
		description=_("Skips back a paragraph and keeps reading"),
		gesture="kb:NVDA+alt+,",
	)
	def script_skipBack(self, gesture):
		self._unpause()
		self.readFrom.skip(-1)

	@script(
		# Translators: description of the command that skips forward a paragraph while Read from here is reading.
		description=_("Skips forward a paragraph and keeps reading"),
		gesture="kb:NVDA+alt+.",
	)
	def script_skipForward(self, gesture):
		self._unpause()
		self.readFrom.skip(1)
