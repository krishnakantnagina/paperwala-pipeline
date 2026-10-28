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
from .detect import classify_lines, plan_page
from .ocr import DEFAULT_LANGS, group_lines, ocr_image
from .pdf import analyze, file_sha256, open_pdf, raster_page
from .verify import verify


def process_paper(src: str | Path, dst: str | Path, bl: Blocklist, paper_id: str = "",
                  crops_dir: str | Path | None = None, langs: str = DEFAULT_LANGS) -> dict:
    src, dst = Path(src), Path(dst)
    info = analyze(src)
    doc = open_pdf(src)
    plans, before = [], {}
    try:
        for page in info.pages:
            raster = raster_page(doc, page)
            lines = classify_lines(group_lines(ocr_image(raster.image, langs)), bl)
            plan = plan_page(page, raster.image.size, lines, bl)
            plan.origin, plan.scale = raster.origin, raster.scale
            plans.append(plan)
            # Keep pixels only where a crop will be drawn; a 30-page scan held
            # whole would cost hundreds of MB.
            if plan.regions or plan.suspects:
                before[page.number] = raster.image
    finally:
        doc.close()

    review = sorted({reason for p in plans for reason in p.review})
    if plans and all(p.drop for p in plans):
        # A file that is nothing but ads is not a paper; never publish empty.
        for p in plans:
            p.drop = False
        review.append("all_pages_ad")

    dst.parent.mkdir(parents=True, exist_ok=True)
    clean_pdf(src, info, plans, bl, dst)
    result = verify(src, info, plans, bl, dst, langs, keep_rasters_for=set(before))
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
        "image_sha1": sorted({i.sha1 for p in info.pages for i in p.images}),
        "changed": any(p.applied_regions or p.drop for p in plans),
        "review": review,
        "auto_ok": not review and result.ok,
        "verify": {"ok": result.ok, "failures": result.failures, "notes": result.notes,
                   "pages": result.pages},
        "crops": crops,
        "pages": [p.to_dict() for p in plans],
    }
