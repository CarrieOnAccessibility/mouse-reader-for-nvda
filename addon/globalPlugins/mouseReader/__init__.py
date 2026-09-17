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
import inputCore
from gui import guiHelper
from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
from logHandler import log
from scriptHandler import script
import speech
import ui
import wx

from . import hook, ocr, reader

try:
	addonHandler.initTranslation()
except Exception:  # not running from an installed add-on (e.g. scratchpad)
	pass

CONF_SECTION = "mouseReader"

LEVEL_CHOICES = (
	# Translators: a hover level: every recognised line is read on its own.
	(ocr.LEVEL_LINE, _("Line")),
	# Translators: a hover level: lines grouped into paragraphs (sentence ends and list items start new ones).
	(ocr.LEVEL_PARAGRAPH, _("Paragraph")),
	# Translators: a hover level: a whole message or section (lines grouped by spacing only).
	(ocr.LEVEL_BLOCK, _("Block (a whole message or section)")),
)

config.conf.spec[CONF_SECTION] = {
	"enabled": "boolean(default=True)",
	"hoverLevel": "option(%s, default='%s')" % (", ".join("'%s'" % key for key, _label in LEVEL_CHOICES), ocr.LEVEL_PARAGRAPH),
}


class Settings:
	"""Live view of the add-on's configuration (follows NVDA's configuration profiles)."""

	def enabled(self) -> bool:
		return bool(config.conf[CONF_SECTION]["enabled"])

	def hoverLevel(self) -> str:
		level = config.conf[CONF_SECTION]["hoverLevel"]
		return level if level in ocr.LEVELS else ocr.LEVEL_PARAGRAPH


# Translators: the text of the "How to use" window.
HOW_TO_USE = _(
	"Mouse Reader uses OCR to read text that NVDA's mouse tracking, also known as mouse echo, "
	"cannot read.\n"
	"\n"
	"Hold Control+NVDA and click where you want NVDA to read. Mouse Reader takes a screenshot of "
	"the window and reads the paragraph you clicked. If mouse tracking is on, you can then move "
	"the mouse over other text to hear it. You can also place the mouse where you want reading to "
	"start and press Control+NVDA+Enter.\n"
	"\n"
	"In a window that scrolls, roll the mouse wheel and Mouse Reader recognizes the screen again. "
	"Clicking or pressing a key ends the recognition.\n"
	"\n"
	"Hold Shift+NVDA and click, or press Shift+NVDA+Enter, to start continuous reading from that "
	"point. Continuous reading ignores mouse movement, even with mouse tracking on, and stops when "
	"you click or press a key.\n"
	"\n"
	"Please note that OCR reads a picture of the screen, so it can misread words, miss small or "
	"faint text, and does not know about links, headings or other structure."
)


# Translators: reported when a command is used while the add-on is turned off in its settings.
OFF_MESSAGE = _("Mouse Reader is turned off in its settings")


def levelLabel(level) -> str:
	for key, label in LEVEL_CHOICES:
		if key == level:
			return label
	return level


class HowToUseDialog(wx.Dialog):
	"""A small window with the shortcuts in a read-only text box, which NVDA reads on focus and
	the arrow keys can move through, and a Close button."""

	def __init__(self, parent):
		# Translators: title of the "How to use" window.
		super().__init__(parent, title=_("How to use Mouse Reader"))
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)
		# Translators: label of the read-only text box in the "How to use" window.
		label = wx.StaticText(self, label=_("How to use:"))
		sHelper.addItem(label)
		self.text = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(1100, 440))
		self.text.SetValue(HOW_TO_USE)
		sHelper.addItem(self.text, flag=wx.EXPAND, proportion=1)
		# Tie the label to the box for screen readers, since they are on separate lines.
		try:
			self.text.SetName(label.GetLabel().rstrip(":"))
		except Exception:
			pass
		sHelper.addDialogDismissButtons(self.CreateButtonSizer(wx.CLOSE), separated=True)
		self.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
		self.SetEscapeId(wx.ID_CLOSE)
		mainSizer.Add(sHelper.sizer, border=guiHelper.BORDER_FOR_DIALOGS, flag=wx.ALL | wx.EXPAND, proportion=1)
		self.SetSizer(mainSizer)
		mainSizer.Fit(self)
		self.CentreOnScreen()
		self.text.SetFocus()
		self.text.SetInsertionPoint(0)


class MouseReaderSettingsPanel(SettingsPanel):
	# Translators: title of the Mouse Reader category in the NVDA Settings dialog.
	title = _("Mouse Reader")
	helpId = ""

	def makeSettings(self, settingsSizer):
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		section = config.conf[CONF_SECTION]
		self.enabledCheckBox = sHelper.addItem(
			# Translators: label of the check box that turns the add-on on or off.
			wx.CheckBox(self, label=_("&Enable Mouse Reader"))
		)
		self.enabledCheckBox.SetValue(bool(section["enabled"]))
		self.levelChoice = sHelper.addLabeledControl(
			# Translators: label of the dropdown that picks how much text a hover reads.
			_("&Hover reads:"),
			wx.Choice,
			choices=[label for _key, label in LEVEL_CHOICES],
		)
		keys = [key for key, _label in LEVEL_CHOICES]
		current = section["hoverLevel"]
		self.levelChoice.SetSelection(keys.index(current) if current in keys else 1)
		# Translators: label of the button that opens a short explanation of the add-on.
		howToButton = sHelper.addItem(wx.Button(self, label=_("&How to use...")))
		howToButton.Bind(wx.EVT_BUTTON, self._onHowTo)
		note = wx.StaticText(
			self,
			# Translators: explanatory text shown at the end of the Mouse Reader settings category.
			label=_(
				"Hovering to hear text needs NVDA's mouse tracking to be on (NVDA+M). "
				"The click and Enter commands work either way."
			),
		)
		note.Wrap(self.scaleSize(500))
		sHelper.addItem(note)

	def _onHowTo(self, evt):
		dialog = HowToUseDialog(self)
		try:
			dialog.ShowModal()
		finally:
			dialog.Destroy()

	def onSave(self):
		config.conf[CONF_SECTION]["enabled"] = self.enabledCheckBox.IsChecked()
		config.conf[CONF_SECTION]["hoverLevel"] = LEVEL_CHOICES[self.levelChoice.GetSelection()][0]


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	# Translators: category of the add-on's commands in the Input Gestures dialog.
	scriptCategory = _("Mouse Reader")

	def __init__(self):
		super().__init__()
		try:
			log.info("mouseReader %s loaded" % addonHandler.getCodeAddon().version)
		except Exception:
			pass
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
		try:
			inputCore.decide_executeGesture.register(self._onGesture)
		except Exception:
			log.exception("mouseReader: could not watch key presses")

	def terminate(self):
		try:
			inputCore.decide_executeGesture.unregister(self._onGesture)
		except Exception:
			pass
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

	def _onGesture(self, gesture):
		"""Every gesture NVDA is about to execute. Our own commands keep the snapshot; any other
		key press drops it (the user is typing or commanding, so the window may have changed)."""
		try:
			script = getattr(gesture, "script", None)
			if script is not None and getattr(script, "__self__", None) is self:
				return True
		except Exception:
			pass
		return self.reader.onGesture(gesture)

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
		# Translators: description of the command that switches how much text a hover reads (no key by default).
		description=_("Cycles what a hover reads in a recognized window: line, paragraph or block"),
	)
	def script_cycleLevel(self, gesture):
		keys = [key for key, _label in LEVEL_CHOICES]
		current = self.settings.hoverLevel()
		nextLevel = keys[(keys.index(current) + 1) % len(keys)]
		config.conf[CONF_SECTION]["hoverLevel"] = nextLevel
		# Translators: reported when the hover level changes; {level} is Line, Paragraph or Block.
		ui.message(_("Hover reads: {level}").format(level=levelLabel(nextLevel)))

	@script(
		# Translators: description of the command that reads on from the mouse position.
		description=_("Reads on from the text under the mouse to the end of the window (same as NVDA+shift+click)"),
		gesture="kb:NVDA+shift+enter",
	)
	def script_readAll(self, gesture):
		if not self.settings.enabled():
			ui.message(OFF_MESSAGE)
			return
		try:
			speech.pauseSpeech(False)  # shift in the shortcut may have left the voice paused
		except Exception:
			pass
		self.reader.readAllAtMouse()

	@script(
		# Translators: description of the command that recognises the window under the mouse.
		description=_("Recognizes the window under the mouse and reads the paragraph under the pointer (same as NVDA+control+click)"),
		gesture="kb:NVDA+control+enter",
	)
	def script_recognize(self, gesture):
		if not self.settings.enabled():
			ui.message(OFF_MESSAGE)
			return
		try:
			speech.pauseSpeech(False)  # a modifier in the shortcut may have left the voice paused
		except Exception:
			pass
		self.reader.recognizeAtMouse()
