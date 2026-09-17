# Mouse Reader / Mouse Echo Delay: screenshots for the CoA Apps product pages.
#
# Builds a stand-in NVDA Settings dialog with NVDA's own bundled wxPython (no NVDA needed),
# showing the two add-ons' real settings panels (same controls, same labels) and Mouse
# Reader's "How to use" window, themed with the Dark Mode add-on's engine so the shots match
# the Dark Mode product page, and photographs them. Each window is on screen for about a
# second. Never moves the mouse.
#
#   python dev/site_shots.py
#
# Writes: CoA Apps/apps/mouse-reader/1.png, 2.png and CoA Apps/apps/mouse-echo-delay/1.png
import ctypes
import os
import sys
import threading

try:
	ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
	pass
_wd = threading.Timer(40, lambda: os._exit(3))
_wd.daemon = True
_wd.start()

NVDA_DIR = r"C:\Program Files\NVDA"
HERE = os.path.dirname(os.path.abspath(__file__))
CODING = os.path.abspath(os.path.join(HERE, "..", ".."))
DARK_MODE_PLUGIN = os.path.join(CODING, "dark-mode-for-nvda", "addon", "globalPlugins", "darkMode")
SITE = os.path.join(CODING, "CoA Apps", "apps")
os.add_dll_directory(NVDA_DIR)
sys.path.insert(0, os.path.join(NVDA_DIR, "library.zip"))
sys.path.insert(0, NVDA_DIR)
sys.path.insert(0, DARK_MODE_PLUGIN)
import wx  # noqa: E402
from wx.lib.mixins import listctrl as listmix  # noqa: E402
import theming  # noqa: E402  (Dark Mode's engine)
from PIL import ImageGrab  # noqa: E402

CATEGORIES = [
	"General", "Speech", "Braille", "Audio", "Vision", "Keyboard", "Mouse", "Touch Interaction",
	"Review Cursor", "Object Presentation", "Browse Mode", "Document Formatting", "Document Navigation",
	"Add-on Store", "Windows OCR", "Advanced", "Dark Mode", "Mouse Echo Delay", "Mouse Reader",
]

READER_HOW_TO = (
	"Mouse Reader uses OCR to read text that NVDA's mouse tracking, also known as mouse echo, cannot read.\n"
	"\n"
	"Hold Control+NVDA and click where you want NVDA to read. Mouse Reader takes a screenshot of the window "
	"and reads the paragraph you clicked. If mouse tracking is on, you can then move the mouse over other text "
	"to hear it. You can also place the mouse where you want reading to start and press Control+NVDA+Enter.\n"
	"\n"
	"In a window that scrolls, roll the mouse wheel and Mouse Reader recognizes the screen again. Clicking or "
	"pressing a key ends the recognition.\n"
	"\n"
	"Hold Shift+NVDA and click, or press Shift+NVDA+Enter, to start continuous reading from that point. "
	"Continuous reading ignores mouse movement, even with mouse tracking on, and stops when you click or press a key.\n"
	"\n"
	"Please note that OCR reads a picture of the screen, so it can misread words, miss small or faint text, and "
	"does not know about links, headings or other structure."
)


class CatList(wx.ListCtrl, listmix.ListCtrlAutoWidthMixin):
	def __init__(self, parent):
		wx.ListCtrl.__init__(self, parent, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_HRULES | wx.LC_VRULES)
		listmix.ListCtrlAutoWidthMixin.__init__(self)


class FakeSettings(wx.Dialog):
	"""NVDA's Settings dialog shape: categories list on the left, the panel on the right."""

	def __init__(self, category, buildPanel):
		super().__init__(None, title="NVDA Settings: %s (normal configuration)" % category)
		self.SetSize(self.FromDIP(wx.Size(1000, 640)))
		outer = wx.BoxSizer(wx.VERTICAL)
		grid = wx.BoxSizer(wx.HORIZONTAL)
		left = wx.BoxSizer(wx.VERTICAL)
		left.Add(wx.StaticText(self, label="&Categories:"), 0, wx.ALL, 5)
		self.catList = CatList(self)
		self.catList.InsertColumn(0, "Name")
		self.catList.InsertColumn(1, "Status", width=self.FromDIP(90))
		for name in CATEGORIES:
			self.catList.Append((name, "Enabled"))
		self.catList.Select(CATEGORIES.index(category))
		self.catList.EnsureVisible(CATEGORIES.index(category))
		left.Add(self.catList, 1, wx.EXPAND | wx.ALL, 5)
		grid.Add(left, 0, wx.EXPAND)
		grid.SetItemMinSize(left, self.FromDIP(wx.Size(300, -1)))
		self.container = wx.Panel(self)
		panel = wx.Panel(self.container)
		ps = wx.BoxSizer(wx.VERTICAL)
		buildPanel(panel, ps)
		panel.SetSizer(ps)
		cs = wx.BoxSizer(wx.VERTICAL)
		cs.Add(panel, 1, wx.EXPAND | wx.ALL, 8)
		self.container.SetSizer(cs)
		grid.Add(self.container, 1, wx.EXPAND | wx.ALL, 5)
		outer.Add(grid, 1, wx.EXPAND)
		btns = wx.StdDialogButtonSizer()
		ok = wx.Button(self, wx.ID_OK, "OK")
		ok.SetDefault()
		btns.AddButton(ok)
		btns.AddButton(wx.Button(self, wx.ID_CANCEL, "Cancel"))
		btns.AddButton(wx.Button(self, wx.ID_APPLY, "Apply"))
		btns.Realize()
		outer.Add(btns, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
		self.SetSizer(outer)
		self.CentreOnScreen()


def gap(panel):
	return panel.FromDIP(10)


def readerPanel(panel, ps):
	cb = wx.CheckBox(panel, label="&Enable Mouse Reader")
	cb.SetValue(True)
	ps.Add(cb, 0, wx.BOTTOM, gap(panel))
	row = wx.BoxSizer(wx.HORIZONTAL)
	row.Add(wx.StaticText(panel, label="&Hover reads:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, gap(panel))
	ch = wx.Choice(panel, choices=["Line", "Paragraph", "Block (a whole message or section)"])
	ch.SetSelection(1)
	row.Add(ch, 0)
	ps.Add(row, 0, wx.BOTTOM, gap(panel))
	ps.Add(wx.Button(panel, label="&How to use..."), 0, wx.BOTTOM, gap(panel))
	note = wx.StaticText(panel, label="Hovering to hear text needs NVDA's mouse tracking to be on (NVDA+M). The click and Enter commands work either way.")
	note.Wrap(panel.FromDIP(500))
	ps.Add(note, 0)


def delayPanel(panel, ps):
	cb = wx.CheckBox(panel, label="&Add delay before mouse echo")
	cb.SetValue(True)
	ps.Add(cb, 0, wx.BOTTOM, gap(panel))
	row = wx.BoxSizer(wx.HORIZONTAL)
	row.Add(wx.StaticText(panel, label="&Delay before mouse echo, in milliseconds:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, gap(panel))
	sp = wx.SpinCtrl(panel, min=0, max=10000, initial=300, style=wx.SP_ARROW_KEYS | wx.ALIGN_RIGHT)
	row.Add(sp, 0)
	ps.Add(row, 0, wx.BOTTOM, gap(panel))
	note = wx.StaticText(panel, label="Mouse Echo Delay needs NVDA's mouse tracking to be on (NVDA+M). You can add a keyboard shortcut for turning the delay on and off in Input Gestures.")
	note.Wrap(panel.FromDIP(500))
	ps.Add(note, 0)


class HowTo(wx.Dialog):
	def __init__(self):
		super().__init__(None, title="How to use Mouse Reader")
		main = wx.BoxSizer(wx.VERTICAL)
		inner = wx.BoxSizer(wx.VERTICAL)
		inner.Add(wx.StaticText(self, label="How to use:"), 0, wx.BOTTOM, self.FromDIP(6))
		text = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=self.FromDIP(wx.Size(1100, 440)))
		text.SetValue(READER_HOW_TO)
		inner.Add(text, 1, wx.EXPAND)
		main.Add(inner, 1, wx.ALL | wx.EXPAND, 10)
		main.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
		btn = wx.Button(self, wx.ID_CLOSE, "Close")
		main.Add(btn, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
		self.SetSizer(main)
		main.Fit(self)
		self.CentreOnScreen()
		text.SetFocus()
		text.SetInsertionPoint(0)


class _RECT(ctypes.Structure):
	_fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]


def screenshot(hwnd, path, pad=8):
	r = _RECT()
	ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
	img = ImageGrab.grab(bbox=(r.l - pad, r.t - pad, r.r + pad, r.b + pad), all_screens=True)
	img.save(path)
	print("saved", path, img.size)


app = wx.App(False)
engine = theming.DarkModeEngine(lambda: True)
engine.start()

SHOTS = [
	("Mouse Reader", readerPanel, os.path.join(SITE, "mouse-reader", "1.png")),
	("Mouse Echo Delay", delayPanel, os.path.join(SITE, "mouse-echo-delay", "1.png")),
	("howto", None, os.path.join(SITE, "mouse-reader", "2.png")),
]
state = {"i": 0, "win": None}


def step():
	if state["win"] is not None:
		state["win"].Destroy()
		state["win"] = None
	if state["i"] >= len(SHOTS):
		engine.stop()
		app.ExitMainLoop()
		return
	name, build, path = SHOTS[state["i"]]
	state["i"] += 1
	win = HowTo() if name == "howto" else FakeSettings(name, build)
	state["win"] = win
	win.Show()
	wx.CallLater(900, lambda: (screenshot(win.GetHandle(), path), wx.CallLater(200, step)))


wx.CallLater(300, step)
app.MainLoop()
print("done")
