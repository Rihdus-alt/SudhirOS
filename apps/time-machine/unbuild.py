#!/usr/bin/env python3
"""Recover the desk page as it was before build.py's patches, from a page that has them.

    python3 apps/time-machine/unbuild.py --built <the published desk page> --out <where to write the base>

The base page (the Trading Desk as it lives in Claude, with whatever edits you have made to it since) is not in this repository.
build.py patches the Time tab into it with anchored text replacements; this runs the same list of replacements backwards, so
the base can be recovered from the published page at any time. Every replacement must be found exactly once, or it stops and says
which: a published page that was edited inside a patched region cannot be unpicked blindly.

Checks its own work: building the recovered base again must give back the published page byte for byte."""
import argparse
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_build():
    spec = importlib.util.spec_from_file_location("tm_build", os.path.join(HERE, "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Recorder:
    """Stands in for build.Patcher: records every (old, new, label) instead of applying it."""
    pairs = []

    def __init__(self, text):
        self.s = text
        Recorder.pairs = []

    def once(self, old, new, label):
        Recorder.pairs.append((old, new, label))

    def sub_once(self, pattern, repl, label, flags=0):
        sys.exit("unbuild.py: build.py uses a regex patch (%r), which cannot be reversed" % label)


def unbuild(built_text, build_mod):
    m = re.match(r"^(<!doctype html>.*?<body>\n?)(.*?)(</body></html>\s*)$", built_text, re.S | re.I)
    if not m:
        sys.exit("unbuild.py: not a complete page in the Artifact skeleton")
    head, body, tail = m.group(1), m.group(2), m.group(3)
    real = build_mod.Patcher
    build_mod.Patcher = Recorder
    try:
        build_mod.build("<!doctype html><html><head></head><body>\n<title>Trading Desk</title>\n</body></html>")
    finally:
        build_mod.Patcher = real
    for old, new, label in reversed(Recorder.pairs):
        n = body.count(new)
        if n != 1:
            sys.exit("unbuild.py: the text build.py inserts for %r is in the page %d times (wanted 1): the page was edited inside that region" % (label, n))
        body = body.replace(new, old)
    return head + body + tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--built", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    b = load_build()
    with open(a.built, encoding="utf-8") as fh:
        built = fh.read()
    base = unbuild(built, b)
    head, body, tail = b.build(base)
    again = head + body + tail
    if again.strip() != built.strip():
        i = next((k for k, (x, y) in enumerate(zip(again, built)) if x != y), min(len(again), len(built)))
        sys.exit("unbuild.py: building the recovered base does not give the published page back (first difference at %d: %r vs %r)" % (i, again[i - 40:i + 40], built[i - 40:i + 40]))
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(base)
    print("recovered the base (%d bytes) and checked that build.py rebuilds the published page from it exactly" % len(base.encode()))


if __name__ == "__main__":
    main()
