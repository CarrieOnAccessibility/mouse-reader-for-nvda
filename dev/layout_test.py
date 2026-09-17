# Mouse Reader: an NVDA add-on. Copyright (C) 2026 Carrie on Accessibility.
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, version 2.
# See the LICENSE file for details.
"""Offline check of Mouse Reader's OCR paragraph grouping, outside NVDA:  python dev/layout_test.py [git ref]

Compares the rules in a git ref (main by default) with the working copy.

Loads groupUnits and friends from an ocr.py source text (the committed one or the working
copy) with the NVDA imports stubbed, and runs two synthetic layouts:
- a Slack-like chat: one-line messages a message-gap apart, one wrapped two-line message;
- a justified book page in two columns with generous leading, sentence boundaries landing on
  line starts, a heading, and a bulleted item.
Prints the paragraphs each rule set produces.
"""
import io
import os
import re
import subprocess
import sys
import types

REPO = r"D:\00 Carrie on Accessibility\Coding\mouse-reader-for-nvda"
OCR = REPO + r"\addon\globalPlugins\mouseReader\ocr.py"


def loadEngine(source):
    """The grouping functions from an ocr.py source text."""
    start = source.index("class Paragraph:")
    end = source.index("# Speech calls slower than this are logged")
    head_start = source.index("LEVEL_LINE =")
    head_end = source.index("_PrintWindow =")
    import tempfile
    ns = {"re": re, "os": os, "tempfile": tempfile}
    exec(source[head_start:head_end], ns)
    exec(source[start:end], ns)
    return ns


def word(text, x, y, w=None, h=20):
    return {"text": text, "x": x, "y": y, "width": w if w is not None else 11 * len(text), "height": h}


def line(text, x, y, h=20, charWidth=11):
    """One OCR line as a list of word dicts laid out left to right."""
    words = []
    cx = x
    for t in text.split():
        words.append(word(t, cx, y, charWidth * len(t), h))
        cx += charWidth * (len(t) + 1)
    return words


def chat():
    """Slack-like: glyph 20 px, line pitch 28 px within a message, 56 px between messages.
    Column width ~ 900 px (long lines reach it)."""
    data = []
    y = 100
    data.append(line("Morning everyone, quick reminder that the meeting is at ten today.", 100, y)); y += 56
    data.append(line("Thanks Carrie. I will bring the slides and the updated budget numbers so we", 100, y)); y += 28
    data.append(line("can go through them together.", 100, y)); y += 56
    data.append(line("Sounds good.", 100, y)); y += 56
    data.append(line("Can someone share the link again?", 100, y)); y += 56
    data.append(line("Here it is: https://example.com/meeting", 100, y)); y += 56
    return data


def slackBullets():
    """Lynette's message: a header line, "What's new:", five bullets 40 px apart (glyph 25),
    then two near-equal-length one-line messages ending in full stops. Windows OCR returns
    the bullet dots as ONE line of five stacked dot words (a tall thin box), and the dots'
    boxes are small (10 px)."""
    data = []
    y = 100
    data.append(line("@channel Aira Explorer Version 4.14.13 for iOS and Android.", 360, y, 25, 12)); y += 55
    data.append(line("What's new:", 360, y, 25, 12)); y += 40
    texts = [
        "Restore one-flick five-star rating",
        "Offer details no longer need a location to load",
        "(android) Fix crash when Meta glasses are folded during a call",
        "(iOS) Recover audio when Meta glasses video stream starts",
        "(iOS) Fix switch speaker not working when glasses are being used",
    ]
    dots = []
    for t in texts:
        dots.append(word("•", 372, y + 8, 10, 10))
        data.append(line(t, 408, y, 25, 12))
        y += 40
    data.append(dots)  # the stacked dot column as one OCR line
    y += 40
    # two separate one-line messages: Slack pads between messages (48 px here against a 40 px line pitch)
    data.append(line("Thanks for the update on the new build and the release notes today.", 360, y, 25, 12)); y += 48
    data.append(line("Sounds good to me, see you all at the meeting later this afternoon.", 360, y, 25, 12))
    return data


def book():
    """Two justified columns, glyph 22 px, pitch 40 px (generous leading), column width 660.
    Sentence boundaries land on line starts. A heading, a bullet, and a paragraph gap."""
    data = []
    cw = 10
    def col(x, y0, texts, gapAfter=()):
        y = y0
        for i, t in enumerate(texts):
            data.append(line(t, x, y, 22, cw))
            y += 80 if i in gapAfter else 40
        return y
    left = [
        "You are about to start an exciting series of",     # 44 chars * 10 = 440 .. make lines ~66 chars
    ]
    # The real page: justified rows of ~64 characters; sentence ends land on line ends; italic
    # runs come back from OCR as separate fragments on the same row, contiguous with the rest.
    # Each row is a list of fragments laid end to end with one space between.
    rows = [
        ["You are about to start an exciting series of lessons on physical"],
        ["science.", "God's Design for the Physical World", "consists of: Machines"],
        ["and Motion,", "Heat and Energy,", "and", "Inventions and Technology."],
        ["It will give you insight into how God designed and created our"],
        ["world and the universe in which we live. No matter what grade you"],
        ["are in, third through eighth grade, you can use this book."],
    ]
    y = 100
    for i, frags in enumerate(rows):
        x = 100
        for piece in frags:
            data.append(line(piece, x, y, 22, cw))
            x += (len(piece) + 1) * cw
        y += 80 if i == len(rows) - 1 else 40
    y = col(100, y, ["3rd-5th grade"], gapAfter=(0,))
    col(100, y, ["Read the lesson.", "• Do the activity in the light blue box (worksheets will be prov", "ided by your teacher)."], gapAfter=(0,))
    right = [
        "6th-8th grade",
    ]
    y = col(900, 100, right, gapAfter=(0,))
    y = col(900, y, ["Read the lesson."], gapAfter=(0,))
    col(900, y, [
        "• Do the activity in the light blue box (worksheets will be prov",
        "ided by your teacher).",
        "• Test your knowledge by answering the What did we learn? questi",
        "ons.",
        "• Assess your understanding by answering the Taking it further q",
        "uestions.",
    ])
    return data


def show(title, ns, data):
    units = ns["groupUnits"](data, ns["LEVEL_PARAGRAPH"])
    print("%s: %d lines -> %d paragraphs" % (title, len(data), len(units)))
    for u in units:
        print("   -", u.text[:90] + ("..." if len(u.text) > 90 else ""))


def captures():
    """Real OCR results saved by the add-on (dev/captures/*.json): lines of words with boxes."""
    import glob, json
    out = []
    for path in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures", "*.json"))):
        with io.open(path, encoding="utf-8") as f:
            out.append((os.path.basename(path), json.load(f)["lines"]))
    return out


baseline = sys.argv[1] if len(sys.argv) > 1 else "main"
committed = subprocess.run(["git", "-C", REPO, "show", baseline + ":addon/globalPlugins/mouseReader/ocr.py"], capture_output=True, text=True, encoding="utf-8").stdout
working = io.open(OCR, encoding="utf-8").read()
for name, src in ((baseline.upper(), committed), ("WORKING COPY", working)):
    ns = loadEngine(src)
    print("=" * 70)
    print(name)
    show("chat", ns, chat())
    show("slack bullets", ns, slackBullets())
    show("book", ns, book())
    for name, data in captures():
        show("capture " + name, ns, data)
