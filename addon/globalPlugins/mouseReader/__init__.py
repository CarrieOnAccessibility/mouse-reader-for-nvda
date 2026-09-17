# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
# Mouse Reader add-on for NVDA: global plugin entry point.
#
# NVDA+control+click or NVDA+control+enter recognises the window under the mouse with Windows
# OCR and reads the paragraph under the pointer; from then on hovering reads recognised
# paragraphs and the wheel recognises again (ocr.py, reader.py). This module is the NVDA
# plumbing: config, the Settings category, the command, and the low-level mouse hook that
# catches the click and the wheel (hook.py).
#
# The delay before mouse tracking reads is a separate add-on, Mouse Echo Delay.

import addonHandler
import config
import globalPluginHandler
from gui import guiHelper
from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
from logHandler import log
from scriptHandler import script
import speech
import wx

from . import hook, reader

try:
	addonHandler.initTranslation()
except Exception:  # not running from an installed add-on (e.g. scratchpad)
	pass

CONF_SECTION = "mouseReader"

config.conf.spec[CONF_SECTION] = {
	"clickEnabled": "boolean(default=True)",
}


class Settings:
	"""Live view of the add-on's configuration (follows NVDA's configuration profiles)."""

	def clickEnabled(self) -> bool:
		return bool(config.conf[CONF_SECTION]["clickEnabled"])


class MouseReaderSettingsPanel(SettingsPanel):
	# Translators: title of the Mouse Reader category in the NVDA Settings dialog.
	title = _("Mouse Reader")
	helpId = ""

	def makeSettings(self, settingsSizer):
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		section = config.conf[CONF_SECTION]
		self.clickCheckBox = sHelper.addItem(
			# Translators: label of the check box that enables NVDA+control+click to recognise the window.
			wx.CheckBox(self, label=_("&Recognize the window under the mouse with NVDA+control+click"))
		)
		self.clickCheckBox.SetValue(bool(section["clickEnabled"]))

	def onSave(self):
		config.conf[CONF_SECTION]["clickEnabled"] = self.clickCheckBox.IsChecked()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	# Translators: category of the add-on's commands in the Input Gestures dialog.
	scriptCategory = _("Mouse Reader")

	def __init__(self):
		super().__init__()
		self.settings = Settings()
		self.reader = reader.Reader(self.settings)
		self.hook = hook.MouseHook()
		self.hook.onButton = self.reader.onButton
		self.hook.onWheel = self.reader.onWheel
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
		try:
			self.reader.shutdown()
		except Exception:
			log.exception("mouseReader: could not stop cleanly")
		super().terminate()

	def event_mouseMove(self, obj, nextHandler, x, y):
		# Inside a recognised window the snapshot answers the mouse, paragraph by paragraph, and
		# NVDA's own mouse tracking stays out of it, so hovering is consistent across the window.
		# Everywhere else the event goes straight through to NVDA.
		try:
			if self.reader.onMouseMove(x, y):
				return
		except Exception:
			log.debugWarning("mouseReader: hover failed", exc_info=True)
		nextHandler()

	@script(
		# Translators: description of the command that recognises the window under the mouse.
		description=_("Recognizes the window under the mouse and reads the paragraph under the pointer (same as NVDA+control+click)"),
		gesture="kb:NVDA+control+enter",
	)
	def script_recognize(self, gesture):
		try:
			speech.pauseSpeech(False)  # a modifier in the shortcut may have left the voice paused
		except Exception:
			pass
		self.reader.recognizeAtMouse()
