# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""Package the add-on: python build.py  ->  dist/mouseReader-<version>.nvda-addon

Also: python build.py --install   copies the add-on into the running user's NVDA
add-ons folder for development testing (restart NVDA afterwards).
"""

import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "addon")
DIST = os.path.join(HERE, "dist")


def version() -> str:
	with open(os.path.join(ADDON, "manifest.ini"), encoding="utf-8") as f:
		for line in f:
			if line.startswith("version"):
				return line.split("=", 1)[1].strip()
	raise SystemExit("version not found in manifest.ini")


def build() -> str:
	os.makedirs(DIST, exist_ok=True)
	out = os.path.join(DIST, f"mouseReader-{version()}.nvda-addon")
	with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
		for root, dirs, files in os.walk(ADDON):
			dirs[:] = [d for d in dirs if d != "__pycache__"]
			for name in files:
				if name.endswith(".pyc"):
					continue
				full = os.path.join(root, name)
				z.write(full, os.path.relpath(full, ADDON))
	print("built", out)
	return out


def install():
	target = os.path.join(os.environ["APPDATA"], "nvda", "addons", "mouseReader")
	if os.path.isdir(target):
		shutil.rmtree(target)
	shutil.copytree(ADDON, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
	print("installed to", target, "- restart NVDA")


if __name__ == "__main__":
	build()
	if "--install" in sys.argv:
		install()
