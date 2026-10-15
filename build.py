#!/usr/bin/env python3
"""
Build the ARC dashboard page: embeds arc_data.json into template.html.

Outputs:
  arc-dashboard.html  page fragment (no <html>/<head>) used for the hosted artifact
  index.html          the same page wrapped as a full HTML document: open it directly in a browser

Usage: python export_data.py && python build.py
"""
import os

here = os.path.dirname(os.path.abspath(__file__))
tpl = open(os.path.join(here, "template.html"), encoding="utf-8").read()
data = open(os.path.join(here, "arc_data.json"), encoding="utf-8").read()
page = tpl.replace("/*__DATA__*/null", data.replace("</", "<\\/"))
open(os.path.join(here, "arc-dashboard.html"), "w", encoding="utf-8").write(page)
full = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
        "</head>\n<body>\n" + page + "\n</body>\n</html>\n")
open(os.path.join(here, "index.html"), "w", encoding="utf-8").write(full)
print(f"built arc-dashboard.html and index.html ({len(page) / 1e6:.2f} MB)")
