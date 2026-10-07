#!/usr/bin/env python3
"""Rebuild the text layer of a PDF with OCR (macOS Vision).

Usage:
    python fix_pdf.py SRC.pdf DST.pdf [--pages auto|all|2-15,17] [--check-dir DIR]
"""
import argparse
import os
import re

import fitz
import Vision
from Foundation import NSData

DPI = 300
FONTS = [
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]
BAD = re.compile(
    r"[\u0180-\u024F]"
    r"|[A-Za-zА-Яа-яЁё][<>@;%{}\[\]][A-Za-zА-Яа-яЁё0-9]"
    r"|[0-9][А-Яа-яЁё]|[А-Яа-яЁё][0-9]"
)
JUNK_LINES = {"", "Х", "X", "•"}


def broken_ratio(page):
    words = page.get_text().split()
    if not words:
        return 1.0, 0
    return sum(1 for w in words if BAD.search(w)) / len(words), len(words)


def page_is_broken(page):
    ratio, n = broken_ratio(page)
    return n < 20 or ratio > 0.15


def parse_pages(spec, doc):
    if spec == "auto":
        return {i for i, p in enumerate(doc) if page_is_broken(p)}
    if spec == "all":
        return set(range(doc.page_count))
    result = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        result.update(range(int(a) - 1, int(b or a)))
    return result


def pick_font():
    for path in FONTS:
        if not os.path.exists(path):
            continue
        d = fitz.open()
        p = d.new_page()
        p.insert_font(fontname="t", fontfile=path)
        p.insert_text((50, 50), "а - б", fontname="t", render_mode=3)
        if fitz.open("pdf", d.tobytes())[0].get_text().strip() == "а - б":
            return path
    raise SystemExit("No font with a correct ToUnicode map found")


def ocr(png_bytes):
    data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["ru-RU", "en-US"])
    req.setUsesLanguageCorrection_(True)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(err)
    for obs in req.results():
        bb = obs.boundingBox()
        yield (obs.topCandidates_(1)[0].string(),
               bb.origin.x, bb.origin.y, bb.size.width, bb.size.height)


def clean(text):
    text = text.replace(" \u00ad ", " – ").replace("\u00ad", "-").replace("\u00a0", " ")
    if text.startswith(("Х ", "X ")):
        text = text[2:]
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--pages", default="auto")
    ap.add_argument("--check-dir")
    args = ap.parse_args()

    src = fitz.open(args.src)
    if src.needs_pass:
        raise SystemExit("PDF is password-protected")
    targets = parse_pages(args.pages, src)
    print("pages to OCR:", sorted(i + 1 for i in targets) or "none")

    font_path = pick_font()
    font = fitz.Font(fontfile=font_path)
    dst = fitz.open()

    for i, page in enumerate(src):
        if i not in targets:
            dst.insert_pdf(src, from_page=i, to_page=i)
            print(f"page {i + 1}: kept")
            continue
        png = page.get_pixmap(dpi=DPI).tobytes("png")
        W, H = page.rect.width, page.rect.height
        new = dst.new_page(width=W, height=H)
        new.insert_image(new.rect, stream=png)
        new.insert_font(fontname="ocr", fontfile=font_path)
        lines = 0
        for text, x, y, w, h in ocr(png):
            text = clean(text)
            if text.strip() in JUNK_LINES:
                continue
            x0, x1 = x * W, (x + w) * W
            top, bottom = (1 - y - h) * H, (1 - y) * H
            size = (bottom - top) * 0.85
            length = font.text_length(text, fontsize=size)
            if length <= 0:
                continue
            base = fitz.Point(x0, bottom - (bottom - top) * 0.2)
            new.insert_text(base, text, fontsize=size, fontname="ocr", render_mode=3,
                            morph=(base, fitz.Matrix((x1 - x0) / length, 1)))
            lines += 1
        print(f"page {i + 1}: OCR, {lines} lines")

    dst.set_metadata({**src.metadata, "producer": "fix_pdf.py"})
    dst.save(args.dst, garbage=4, deflate=True)

    out = fitz.open(args.dst)
    if args.check_dir:
        os.makedirs(args.check_dir, exist_ok=True)
    print("\n=== verification ===")
    for i, page in enumerate(out):
        ratio, n = broken_ratio(page)
        print(f"page {i + 1}: words={n} broken={ratio:.0%}")
        if args.check_dir:
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    for s in line["spans"]:
                        page.draw_rect(s["bbox"], color=(1, 0, 0), width=0.5)
            page.get_pixmap(dpi=100).save(f"{args.check_dir}/page_{i + 1:02d}.png")
            with open(f"{args.check_dir}/page_{i + 1:02d}.txt", "w") as f:
                f.write(out[i].get_text())
    print("saved:", args.dst)


if __name__ == "__main__":
    main()
