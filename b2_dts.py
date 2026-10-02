#!/usr/bin/env python3
"""B2 device-tree transform for nokia_xg-040g-md (parameterized copy of
work/make_b2_dts.py, which was validated on real hardware; every rewrite and
every assertion is carried over unchanged, only the env-var plumbing became
function arguments).

B2 = QWRT kernel + QWRT userspace, unchanged, booted by the web U-Boot on the
UBI layout. So the ONLY things that change are the ones tied to the flash layout:

 1. partitions (stock describes the old mtd layout, which now lives inside the
    ubi volume)
      partition@0      bootloader 0x00     0x80000   DELETED - overlaps ubi@0x20000
      partition@80000  env        0x80000  0x80000   DELETED - env moved to volumes
      +              all_flash    0x00     0x10000000 read-only
      +              bl2          0x00     0x20000    read-only
      ubi            0x100000 0xff00000  ->  0x20000 0xffe0000 (renamed partition@20000)

   The stock `ubi-volume-bosa` (calibration@2) and `ubi-volume-factory`/ri
   (pon-serial@1a, macaddr@3e) subtrees are carried over BYTE FOR BYTE, phandles
   included, so all five nvmem consumers keep resolving:
     ethernet@1fb64000  pon-serial     <0x20>
     optical-front-end@50 calibration  <0x28>
     ethernet@1/@2/@4   mac            <0x35>
   fit/fip/ubootenv/ubootenv2 volume nodes are appended without phandles.

   NO `rootfs` volume node on purpose: `ubi.block=0,rootfs` resolves the volume
   BY NAME and needs no DT node, while an auto-create node without vol-size
   could grab every free PEB and leave nothing for rootfs_data. The volume is
   created by 「按卷写入」 at the exact file length.

 2. aliases pon-calibration: .../partition@100000/... -> .../partition@20000/...

 3. chosen/bootargs-append: QWRT's own name-based UBI root contract; `ubid` is
   the UBI vol_id the rootfs volume lands on on THIS board (GET /info, field i).
   `linux,usable-memory-range` is added (start 0x80200000 trims the 2 MB ATF
   region earlier than no-map does; it can only SHRINK memory, never grow it).
   `rootdisk` is deliberately NOT added - that plus `loadables` is the fitblk
   contract, and the QWRT kernel has no fitblk. That is the whole point of B2.

Everything else - model, compatible, memory, reserved-memory, NFC, PON
ethernet, LEDs, buttons - is left exactly as QWRT shipped it.
"""
import re
import sys

T = "\t"


class TransformError(RuntimeError):
    """A structural expectation about the stock QWRT tree was not met."""


def _block(text, start_pat, label):
    """Return (start, end_after, verbatim) of the brace-balanced node whose
    opening line matches start_pat."""
    m = re.search(start_pat, text, re.M)
    if not m:
        raise TransformError(f"node not found: {label}")
    i = text.index("{", m.start())
    depth = 0
    while True:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                j = text.index(";", i) + 1
                return m.start(), j, text[m.start():j]
        i += 1


def _parts(ubid):
    p, v, n = T * 5, T * 7, T * 8
    return (
        p + "partition@0_all {\n" + T * 6 + 'label = "all_flash";\n'
        + T * 6 + "reg = <0x00 0x10000000>;\n" + T * 6 + "read-only;\n" + p + "};\n\n"
        + p + "partition@0 {\n" + T * 6 + 'label = "bl2";\n'
        + T * 6 + "reg = <0x00 0x20000>;\n" + T * 6 + "read-only;\n" + p + "};\n\n"
    )


def transform(src, ubid, bosa, fact):
    """Rewrite the decompiled stock .dts text into the B2 .dts text.

    src   : stock QWRT dtb decompiled by `dtc -I dtb -O dts` (tab-indented)
    ubid  : UBI vol_id of this board's `rootfs` volume (decimal int)
    bosa  : verbatim `ubi-volume-bosa {...};` block taken from src
    fact  : verbatim `ubi-volume-factory {...};` block taken from src
    """
    if not isinstance(ubid, int) or ubid < 0:
        raise TransformError(f"ubid must be a non-negative int, got {ubid!r}")

    # ---- the two stock volume nodes must survive verbatim ---------------------
    assert "phandle = <0x28>" in bosa, "bosa lost its calibration phandle"
    assert "phandle = <0x20>" in fact and "phandle = <0x35>" in fact, "ri lost a phandle"
    # re-indent is a no-op: both stay at 7 tabs inside the new volumes{} node
    assert bosa.startswith(T * 7) and fact.startswith(T * 7), "unexpected volume indent"

    p, v, n = T * 5, T * 7, T * 8
    parts = (
        _parts(ubid)
        + p + "partition@20000 {\n" + T * 6 + 'label = "ubi";\n'
        + T * 6 + "reg = <0x20000 0xffe0000>;\n" + T * 6
        + 'compatible = "linux,ubi";\n\n'
        + T * 6 + "volumes {\n\n"
        + bosa.rstrip() + "\n\n"
        + fact.rstrip() + "\n\n"
        + v + "ubi-volume-fit {\n" + n + 'volname = "fit";\n' + v + "};\n\n"
        + v + "ubi-volume-fip {\n" + n + 'volname = "fip";\n' + v + "};\n\n"
        + v + "ubi-volume-ubootenv {\n" + n + 'volname = "ubootenv";\n\n'
        + n + "nvmem-layout {\n" + T * 9
        + 'compatible = "u-boot,env-redundant-bool-layout";\n'
        + n + "};\n" + v + "};\n\n"
        + v + "ubi-volume-ubootenv2 {\n" + n + 'volname = "ubootenv2";\n\n'
        + n + "nvmem-layout {\n" + T * 9
        + 'compatible = "u-boot,env-redundant-bool-layout";\n'
        + n + "};\n" + v + "};\n"
        + T * 6 + "};\n"
        + p + "};\n"
    )

    # ---- replace the whole stock partitions child block -----------------------
    s0, e0, old = _block(src, r"^\t+partitions \{", "partitions")
    assert 'label = "bootloader"' in old and 'label = "ubi"' in old, "unexpected partitions"
    body = (
        T * 4 + "partitions {\n" + T * 5 + 'compatible = "fixed-partitions";\n'
        + T * 5 + "#address-cells = <0x01>;\n" + T * 5 + "#size-cells = <0x01>;\n\n"
        + parts + T * 4 + "};\n"
    )
    src = src[:s0] + body + src[e0:]

    # ---- alias path ----------------------------------------------------------
    src, k = re.subn(
        r'pon-calibration = "[^"]*"',
        "pon-calibration = "
        '"/soc/spi@1fa10000/nand@0/partitions/partition@20000/volumes/ubi-volume-bosa"',
        src, count=1)
    if k != 1:
        raise TransformError("pon-calibration alias not found")

    # ---- bootargs-append -----------------------------------------------------
    src, k = re.subn(
        r'bootargs-append = "[^"]*"',
        'bootargs-append = " ubi.mtd=ubi ubi.block=0,rootfs '
        f'root=/dev/ubiblock0_{ubid} rootfstype=squashfs rootwait"',
        src, count=1)
    if k != 1:
        raise TransformError("chosen/bootargs-append not found")

    # ---- usable-memory-range (no rootdisk) -----------------------------------
    src, k = re.subn(
        r'(\n' + T * 2 + 'stdout-path = "serial0:115200n8";\n)',
        r"\1" + T * 2 + "linux,usable-memory-range = <0x00 0x80200000 0x00 0x7fe00000>;\n",
        src, count=1)
    if k != 1:
        raise TransformError("chosen/stdout-path anchor not found")

    # ---- assertions on the result --------------------------------------------
    assert "rootdisk" not in src, "fitblk contract leaked into B2"
    assert 'label = "bootloader"' not in src, "stale bootloader partition survived"
    assert 'label = "env";' not in src, "stale env partition survived"
    assert "0x100000 0xff00000" not in src, "stale ubi geometry survived"
    assert "partition@100000" not in src, "stale partition name survived"
    assert 'volname = "rootfs"' not in src, "rootfs volume node must not be described"
    for ph in ("0x28", "0x20", "0x35"):
        assert src.count(f"phandle = <{ph}>") == 1, f"phandle {ph} not unique"
    # nvmem-cells is <phandle [index]...>; #nvmem-cell-cells = <0x01> on macaddr@3e, so
    # a trailing 0x00/0x01 token is the MAC index argument, not a dangling reference.
    defined = set(re.findall(r"phandle = <(0x[0-9a-f]+)>", src))
    for c in re.finditer(r"nvmem-cells = <([^>]*)>", src):
        toks = c.group(1).split()
        assert toks[0] in defined, f"dangling nvmem phandle {toks[0]}"
        for t in toks[1:]:
            assert t in ("0x00", "0x01"), f"unexpected nvmem cell arg {t}"

    return src


def extract_stock_blocks(src):
    """Pull the verbatim ubi-volume-bosa / ubi-volume-factory nodes out of the
    stock tree (they are carried over untouched, phandles included)."""
    bosa = _block(src, r"^\t+ubi-volume-bosa \{", "ubi-volume-bosa")[2]
    fact = _block(src, r"^\t+ubi-volume-factory \{", "ubi-volume-factory")[2]
    return bosa, fact


def main(argv):
    # Standalone use, kept compatible with the original env-var interface.
    import os
    src = open(os.environ.get("SRC_DTS", "qwrt_fdt.dts"), encoding="utf-8").read()
    bosa, fact = extract_stock_blocks(src)
    out = transform(src, int(os.environ.get("UBID", "1")), bosa, fact)
    dst = os.environ.get("DST_DTS", "ubi_b2.dts")
    open(dst, "w", encoding="utf-8").write(out)
    print(f"wrote {dst}  UBID={os.environ.get('UBID','1')}  nvmem-cells consumers={out.count('nvmem-cells =')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
