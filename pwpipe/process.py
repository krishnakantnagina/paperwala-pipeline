"""Run one paper end to end: analyze -> OCR -> plan -> clean -> verify -> crops.

Returns a JSON-able report. That report is the whole record of the paper's
run: the stage jobs store it, discovery reads its lines, the review page shows
its crops, and the details extractor reuses its page-1 OCR.
"""

from __future__ import annotations

from pathlib import Path

from . import __version__
from .blocklist import Blocklist
from .clean import clean_pdf
from .crops import page_thumb, region_crop
from .detect import AD, LOW_TEXT_WORDS, classify_lines, merge_found_ads, plan_page
from .ocr import DEFAULT_LANGS, OTSU, SAUVOLA, group_lines, ocr_image
from .pdf import analyze, file_sha256, open_pdf, raster_page
from .ruled import struck_through_ads
from .stamp import find_stamp_boxes
from .verify import verify


MAX_SECOND_LOOKS = 2


def _real_words(lines) -> int:
    return sum(len(l.real_words) for l in lines)


def _read(img, langs: str, bl: Blocklist):
    """OCR a page; if that finds almost nothing, read it again with Sauvola
    thresholding and keep whichever reading found more real words."""
    lines = classify_lines(group_lines(ocr_image(img, langs)), bl)
    if _real_words(lines) >= LOW_TEXT_WORDS:
        return OTSU, lines
    again = classify_lines(group_lines(ocr_image(img, langs, threshold=SAUVOLA)), bl)
    if _real_words(again) <= _real_words(lines):
        return OTSU, lines
    # Each reading sees what the other misses: Sauvola read the BU paper's
    # questions but not the URL printed on white between them.
    merge_found_ads(again, [l for l in lines if l.kind == AD], bl)
    return SAUVOLA, again


def _plans(pages, bl: Blocklist) -> list:
    plans = []
    for page, (size, origin, scale, threshold), lines, stamps in pages:
        for l in lines:
            l.region = None
        plan = plan_page(page, size, lines, bl)
        plan.origin, plan.scale, plan.ocr_threshold = origin, scale, threshold
        if not plan.drop:
            plan.stamp_boxes = stamps
        plans.append(plan)
    return plans


def process_paper(src: str | Path, dst: str | Path, bl: Blocklist, paper_id: str = "",
                  crops_dir: str | Path | None = None, langs: str = DEFAULT_LANGS,
                  stamp: bool = True) -> dict:
    src, dst = Path(src), Path(dst)
    info = analyze(src)
    doc = open_pdf(src)
    pages, before = [], {}
    second_looks = []
    try:
        for page in info.pages:
            raster = raster_page(doc, page)
            threshold, lines = _read(raster.image, langs, bl)
            # URLs with a printed rule through them read as nothing above.
            if merge_found_ads(lines, struck_through_ads(raster.image, bl, langs), bl):
                second_looks.append(f"page {page.number}: URL under a printed rule")
            plan = plan_page(page, raster.image.size, lines, bl)
            stamps = []
            if stamp and not plan.drop and "rotated_page" not in plan.review:
                stamps = find_stamp_boxes(raster.image, [b for l in lines for _, _, b in l.words])
            pages.append((page, (raster.image.size, raster.origin, raster.scale, threshold),
                          lines, stamps))
            # Keep pixels only where a crop will be drawn; a 30-page scan held
            # whole would cost hundreds of MB.
            if plan.regions or plan.suspects:
                before[page.number] = raster.image

        dst.parent.mkdir(parents=True, exist_ok=True)
        # Erasing changes how Tesseract lays a page out, so the verifier's
        # reading of the cleaned page sometimes finds a copy of the site's URL
        # that the first reading ran into a neighbouring word (2 papers the
        # owner sent back, 2026-10-28). Such a copy is added and the paper
        # cleaned again; every round is verified from scratch.
        for round_ in range(1 + MAX_SECOND_LOOKS):
            plans = _plans(pages, bl)
            review = sorted({reason for p in plans for reason in p.review})
            if plans and all(p.drop for p in plans):
                # A file that is nothing but ads is not a paper; never publish empty.
                for p in plans:
                    p.drop = False
                review.append("all_pages_ad")
            for p in plans:
                if (p.regions or p.suspects) and p.number not in before:
                    before[p.number] = raster_page(doc, info.pages[p.number]).image
            clean_pdf(src, info, plans, bl, dst)
            result = verify(src, info, plans, bl, dst, langs, keep_rasters_for=set(before))
            if result.ok or round_ == MAX_SECOND_LOOKS:
                break
            added = [n for n, ads in result.leftover_ads.items()
                     if merge_found_ads(pages[n][2], ads, bl)]
            if not added:
                break
            second_looks += [f"page {n}: ad found after cleaning (round {round_ + 2})"
                             for n in added]
    finally:
        doc.close()
    if not result.ok:
        review.append("verify_failed")

    crops = []
    if crops_dir is not None:
        cdir = Path(crops_dir)
        for plan in plans:
            img = before.get(plan.number)
            if img is None:
                continue
            after = result.rasters.get(plan.number)
            for i, region in enumerate(plan.regions):
                path = region_crop(img, after, region.bbox,
                                   cdir / f"p{plan.number + 1}_r{i + 1}.png", region.safe,
                                   region.boxes)
                crops.append({"page": plan.number, "region": i, "path": path.name,
                              "applied": region.safe and not plan.drop})
            if plan.drop:
                path = page_thumb(img, cdir / f"p{plan.number + 1}_dropped.png")
                crops.append({"page": plan.number, "region": None, "path": path.name,
                              "applied": True})

    return {
        "paper": paper_id or src.stem,
        "pipeline_version": __version__,
        "blocklist_version": bl.version,
        "source_sha256": info.sha256,
        "output_sha256": file_sha256(dst),
        "source_metadata": info.metadata,
        "page_count": info.page_count,
        "pages_dropped": sum(p.drop for p in plans),
        "scan_pages": sum(p.is_scan for p in info.pages),
        "stamped_pages": sum(1 for p in plans if p.stamp_boxes and not p.drop),
        "stamps": sum(len(p.stamp_boxes) for p in plans if not p.drop),
        "image_sha1": sorted({i.sha1 for p in info.pages for i in p.images}),
        "changed": any(p.applied_regions or p.drop for p in plans),
        "review": review,
        "second_looks": second_looks,
        "auto_ok": not review and result.ok,
        "verify": {"ok": result.ok, "failures": result.failures, "notes": result.notes,
                   "pages": result.pages},
        "crops": crops,
        "pages": [p.to_dict() for p in plans],
    }
