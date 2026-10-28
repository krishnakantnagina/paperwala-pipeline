import numpy as np
from PIL import Image, ImageDraw

from pwpipe.ruled import rule_mask
from pwpipe.verify import _identical_outside


def test_rule_mask_finds_long_rules_not_letters():
    img = Image.new("L", (1000, 400), 255)
    d = ImageDraw.Draw(img)
    d.line((50, 300, 950, 300), fill=0, width=3)     # a rule
    d.rectangle((100, 100, 160, 130), fill=0)        # a short dash or letter
    mask = rule_mask(np.asarray(img))
    assert mask[300, 500] and mask[300, 60] and mask[300, 940]
    assert not mask[115, 130] and not mask[200].any()


def test_identical_outside_ignores_only_the_erased_boxes():
    a = Image.new("L", (100, 100), 255)
    b = a.copy()
    ImageDraw.Draw(b).rectangle((10, 10, 20, 20), fill=0)
    assert _identical_outside(a, b, [(5, 5, 30, 30)])
    assert not _identical_outside(a, b, [(50, 50, 60, 60)])
