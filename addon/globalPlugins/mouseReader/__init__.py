# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
# Mouse Reader add-on for NVDA: global plugin entry point.
#
# Two features that share NVDA's mouse plumbing:
#   - a delay before mouse tracking reads (dwell.py), with a lookup ladder for the spots
#     where NVDA finds no text (ladder.py);
#   - "Read from here": NVDA+control+click reads continuously from that spot, with
#     skip-back / skip-forward commands and an OCR fallback (readfrom.py).
# This module is the NVDA plumbing: config, the Settings category, the commands, and the
# single low-level mouse hook the features share (hook.py).

import addonHandler
import config
import globalPluginHandler
from gui import guiHelper
from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
from logHandler import log
from scriptHandler import script
import ui
import wx

from . import dwell, hook, readfrom

try:
	addonHandler.initTranslation()
except Exception:  # not running from an installed add-on (e.g. scratchpad)
	pass

CONF_SECTION = "mouseReader"
DELAY_MIN_MS = 100  # slider: 0.1 s ...
DELAY_MAX_MS = 2000  # ... to 2 s, in steps of a tenth
DELAY_STEP_MS = 100
DEFAULT_DELAY_MS = 300
CUSTOM_DELAY_MAX_MS = 10000

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
	"delayEnabled": "boolean(default=True)",
	"delayMs": "integer(min=%d, max=%d, default=%d)" % (DELAY_MIN_MS, DELAY_MAX_MS, DEFAULT_DELAY_MS),
	"useCustomDelay": "boolean(default=False)",
	"customDelayMs": "integer(min=0, max=%d, default=300)" % CUSTOM_DELAY_MAX_MS,
	"stopOnMove": "boolean(default=False)",
	"extraLookups": "boolean(default=True)",
	"readFromClick": "boolean(default=True)",
	"readFromStart": "option(%s, default='paragraph')" % ", ".join("'%s'" % key for key, _label in START_CHOICES),
	"readFromOcr": "boolean(default=True)",
}


class Settings:
	"""Live view of the add-on's configuration (follows NVDA's configuration profiles)."""

	@staticmethod
	def _section():
		return config.conf[CONF_SECTION]

	def delayMs(self) -> int:
		section = self._section()
		if not section["delayEnabled"]:
			return 0
		if section["useCustomDelay"]:
			return int(section["customDelayMs"])
		return int(section["delayMs"])

	def stopOnMove(self) -> bool:
		return bool(self._section()["stopOnMove"])

	def extraLookups(self) -> bool:
		return bool(self._section()["extraLookups"])

	def readFromClick(self) -> bool:
		return bool(self._section()["readFromClick"])

	def readFromStart(self) -> str:
		return self._section()["readFromStart"]

	def readFromOcr(self) -> bool:
		return bool(self._section()["readFromOcr"])


def _sliderClass():
	"""NVDA's slider (arrow keys and page keys behave), or wx's if it is not there."""
	try:
		from gui import nvdaControls

		return nvdaControls.EnhancedInputSlider
	except Exception:
		return wx.Slider


def _delayLabel(ms: int) -> str:
	# Translators: a delay in seconds, e.g. "0.3 seconds".
	return _("{seconds:g} seconds").format(seconds=ms / 1000.0)


class MouseReaderSettingsPanel(SettingsPanel):
	# Translators: title of the Mouse Reader category in the NVDA Settings dialog.
	title = _("Mouse Reader")
	helpId = ""

	def makeSettings(self, settingsSizer):
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		section = config.conf[CONF_SECTION]

		# --- the delay ---
		self.delayEnabledCheckBox = sHelper.addItem(
			# Translators: label of the check box that turns the mouse tracking delay on or off.
			wx.CheckBox(self, label=_("&Wait until the mouse stops before reading under it"))
		)
		self.delayEnabledCheckBox.SetValue(bool(section["delayEnabled"]))
		self.delaySlider = sHelper.addLabeledControl(
			# Translators: label of the slider that sets how long the mouse must be still before NVDA reads.
			_("&Delay before reading, in milliseconds:"),
			_sliderClass(),
			value=int(section["delayMs"]),
			minValue=DELAY_MIN_MS,
			maxValue=DELAY_MAX_MS,
		)
		self.delaySlider.SetLineSize(DELAY_STEP_MS)  # arrow keys step a tenth of a second
		self.delaySlider.SetPageSize(DELAY_STEP_MS * 5)
		self.delayValueLabel = sHelper.addItem(wx.StaticText(self, label=_delayLabel(int(section["delayMs"]))))
		self.delaySlider.Bind(wx.EVT_SLIDER, self._onDelaySlider)
		self.useCustomDelayCheckBox = sHelper.addItem(
			# Translators: label of the check box that lets the user type an exact delay instead of using the slider.
			wx.CheckBox(self, label=_("Use a &custom delay instead"))
		)
		self.useCustomDelayCheckBox.SetValue(bool(section["useCustomDelay"]))
		self.customDelaySpin = sHelper.addLabeledControl(
			# Translators: label of the field for an exact mouse tracking delay in milliseconds.
			_("Custom delay in &milliseconds (0 reads at once):"),
			wx.SpinCtrl,
			min=0,
			max=CUSTOM_DELAY_MAX_MS,
			initial=int(section["customDelayMs"]),
		)
		self.useCustomDelayCheckBox.Bind(wx.EVT_CHECKBOX, self._onUseCustomDelay)
		self.delayEnabledCheckBox.Bind(wx.EVT_CHECKBOX, self._onDelayEnabled)
		self.stopOnMoveCheckBox = sHelper.addItem(
			# Translators: label of the check box that makes speech stop as soon as the mouse moves again.
			wx.CheckBox(self, label=_("&Stop speech when the mouse moves"))
		)
		self.stopOnMoveCheckBox.SetValue(bool(section["stopOnMove"]))
		self.extraLookupsCheckBox = sHelper.addItem(
			# Translators: label of the check box that turns the extra text lookups on or off.
			wx.CheckBox(self, label=_("Try &extra ways of finding text when NVDA finds none under the mouse"))
		)
		self.extraLookupsCheckBox.SetValue(bool(section["extraLookups"]))

		# --- read from here ---
		self.readFromClickCheckBox = sHelper.addItem(
			# Translators: label of the check box that enables NVDA+control+click to read from that spot.
			wx.CheckBox(self, label=_("&Read from here with NVDA+control+click"))
		)
		self.readFromClickCheckBox.SetValue(bool(section["readFromClick"]))
		self.readFromStartChoice = sHelper.addLabeledControl(
			# Translators: label of the dropdown that picks where Read from here starts reading.
			_("Read from here starts at:"),
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
		self._syncEnabled()

	def _onDelaySlider(self, evt):
		self.delayValueLabel.SetLabel(_delayLabel(self.delaySlider.GetValue()))
		evt.Skip()

	def _onUseCustomDelay(self, evt):
		self._syncEnabled()
		evt.Skip()

	def _onDelayEnabled(self, evt):
		self._syncEnabled()
		evt.Skip()

	def _syncEnabled(self):
		on = self.delayEnabledCheckBox.IsChecked()
		custom = self.useCustomDelayCheckBox.IsChecked()
		self.delaySlider.Enable(on and not custom)
		self.useCustomDelayCheckBox.Enable(on)
		self.customDelaySpin.Enable(on and custom)

	def onSave(self):
		section = config.conf[CONF_SECTION]
		section["delayEnabled"] = self.delayEnabledCheckBox.IsChecked()
		# A mouse drag can land between steps; keep the saved value on the tenth-of-a-second grid.
		ms = int(round(self.delaySlider.GetValue() / float(DELAY_STEP_MS))) * DELAY_STEP_MS
		section["delayMs"] = max(DELAY_MIN_MS, min(DELAY_MAX_MS, ms))
		section["useCustomDelay"] = self.useCustomDelayCheckBox.IsChecked()
		section["customDelayMs"] = int(self.customDelaySpin.GetValue())
		section["stopOnMove"] = self.stopOnMoveCheckBox.IsChecked()
		section["extraLookups"] = self.extraLookupsCheckBox.IsChecked()
		section["readFromClick"] = self.readFromClickCheckBox.IsChecked()
		section["readFromStart"] = START_CHOICES[self.readFromStartChoice.GetSelection()][0]
		section["readFromOcr"] = self.readFromOcrCheckBox.IsChecked()
		if GlobalPlugin.instance:
			GlobalPlugin.instance.onConfigChanged()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	# Translators: category of the add-on's commands in the Input Gestures dialog.
	scriptCategory = _("Mouse Reader")
	instance = None

	def __init__(self):
		super().__init__()
		GlobalPlugin.instance = self
		self.settings = Settings()
		self.readFrom = None
		self.dwell = dwell.DwellEngine(self.settings, self._isReadingFromHere)
		self.readFrom = readfrom.ReadFromHere(self.settings, self.dwell)
		self.hook = hook.MouseHook()
		self.hook.onMove = self.dwell.onRawMove
		self.hook.onButton = self.readFrom.onButton
		NVDASettingsDialog.categoryClasses.append(MouseReaderSettingsPanel)
		config.post_configProfileSwitch.register(self.onConfigChanged)
		config.post_configReset.register(self.onConfigChanged)
		try:
			self.dwell.start()
			self.hook.install()
		except Exception:
			log.exception("mouseReader: failed to start")

	def terminate(self):
		config.post_configProfileSwitch.unregister(self.onConfigChanged)
		config.post_configReset.unregister(self.onConfigChanged)
		try:
			NVDASettingsDialog.categoryClasses.remove(MouseReaderSettingsPanel)
		except ValueError:
			pass
		try:
			self.hook.uninstall()
		except Exception:
			log.exception("mouseReader: could not remove the mouse hook")
		try:
			self.dwell.stop()
		except Exception:
			log.exception("mouseReader: could not restore NVDA's mouse handler")
		GlobalPlugin.instance = None
		super().terminate()

	def _isReadingFromHere(self) -> bool:
		return bool(self.readFrom and self.readFrom.isReading())

	def onConfigChanged(self, **kwargs):
		self.dwell.forgetLadder()

	# ---- commands -----------------------------------------------------------------------

	@script(
		# Translators: description of the command that toggles the delay before mouse tracking reads.
		description=_("Toggles the delay before NVDA reads what is under the mouse"),
		gesture="kb:NVDA+control+shift+m",
	)
	def script_toggleDelay(self, gesture):
		section = config.conf[CONF_SECTION]
		section["delayEnabled"] = not section["delayEnabled"]
		self.dwell.forgetLadder()
		if section["delayEnabled"]:
			# Translators: reported when the mouse tracking delay is turned on; {delay} is e.g. "0.3 seconds".
			ui.message(_("Mouse delay on, {delay}").format(delay=_delayLabel(self.settings.delayMs())))
		else:
			# Translators: reported when the mouse tracking delay is turned off.
			ui.message(_("Mouse delay off"))

	@script(
		# Translators: description of the command that reads continuously from the mouse position.
		description=_("Reads from the text under the mouse onwards (same as NVDA+control+click)"),
		gesture="kb:NVDA+shift+r",
	)
	def script_readFromMouse(self, gesture):
		self.readFrom.readFromMouse()

	@script(
		# Translators: description of the command that skips back a paragraph while Read from here is reading.
		description=_("Skips back a paragraph and keeps reading"),
		gesture="kb:NVDA+alt+,",
	)
	def script_skipBack(self, gesture):
		self.readFrom.skip(-1)

	@script(
		# Translators: description of the command that skips forward a paragraph while Read from here is reading.
		description=_("Skips forward a paragraph and keeps reading"),
		gesture="kb:NVDA+alt+.",
	)
	def script_skipForward(self, gesture):
		self.readFrom.skip(1)
