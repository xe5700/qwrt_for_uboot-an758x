#!/usr/bin/env python3
"""qwrt2an758x.py — 一键把「原版 QWRT」sysupgrade .bin 移植成 uboot-an758x /
本项目 ImmortalWrt-Airoha Web U-Boot 能直接从网页刷入的 **B2 格式** FIT .itb。

目标机型固定：**nokia xg-040g-md**（分区/别名/nvmem phandle 都是这块板写死的）。

B2 的做法：内核 + 改好的 dtb 打包成 .itb 刷进 UBI 的 `fit` 卷；rootfs 作为
**独立的 `rootfs` 卷**单独「按卷写入」；**不依赖 fitblk**（QWRT 内核没有 fitblk），
所以 dtb 里用 `ubi.block=0,rootfs root=/dev/ubiblock0_<UBID>` 按名解析根设备。
详见仓库根目录 QWRT-B2-说明.md。

流程（全自动，只从 .bin 出发，不依赖任何预先存在的中间产物）：
  1. 解 sysupgrade .bin（ustar tar）→ CONTROL / kernel / root
  2. 内层 kernel FIT（dumpimage，与 bootm 同一套 lib）→ 压缩内核 + stock dtb
  3. 解出原始 ARM64 Image（gzip/lzma-alone/xz/zlib-raw/lzma-raw/none 逐个试）
  4. stock dtb 反编译成 dts
  5. 决定 UBID（rootfs 卷的 UBI 编号）：--ubid 指定，或自动 GET /info 探测
  6. b2_dts.transform() 生成 B2 dtb（全套断言，无 fitblk / 无 rootdisk）
  7. mkimage -E -B 0x1000 外部打包（非 static，无 -p），gzip@0x80200000
  8. fwtool -I 追加 OpenWrt metadata
  9. 全量校验：dumpimage 逐载荷回比 + 无 Loadables 检查
 10. 落地 .itb / rootfs.img / .sha256sum，打印刷入步骤

用法:
    python3 qwrt2an758x.py <原版QWRT...-squashfs-sysupgrade.bin> [选项]
    python3 qwrt2an758x.py <bin> --ubid 6 -o out

依赖：Python 3（仅标准库）+ 外部命令 dtc（Arch: pacman -S dtc），U-Boot 的
mkimage/dumpimage（--tools DIR 指定），OpenWrt 的 fwtool（--fwtool 指定）。
curl 可选：自动探测 UBID 用的是内置 urllib，不需要 curl。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARM64_MAGIC = b"ARMd\x00\x00\x00\x00"              # arm64 Image magic 在偏移 0x38
SQUASHFS_MAGIC = b"hsqs"
DTB_MAGIC = b"\xd0\x0d\xfe\xed"
DEFAULT_BOOT_IPS = ["192.168.1.1", "192.168.0.1"]   # ImmortalWrt-Airoha 网页 U-Boot / pbs05 uboot-an758x
LEB = 126976                                         # 本板 live GET /info -> ubi.leb
TARGET_BOARD = "nokia_xg-040g-md"


class Fatal(Exception):
    pass


def die(msg, code=1):
    print(f"ABORT: {msg}", file=sys.stderr)
    raise SystemExit(code)


def run(cmd, cwd=None, check=True, capture=False):
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None, check=False,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None, text=capture)
    if check and r.returncode != 0:
        raise Fatal(f"命令失败 (exit {r.returncode}): {' '.join(map(str, cmd))}\n{r.stdout or ''}")
    return r


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- 工具发现 ----------

def find_tools(cli_tools, cli_fwtool):
    def which(name):
        return shutil.which(name)

    dirs = []
    if cli_tools:
        dirs.append(Path(cli_tools))
    dirs += [
        HERE / "u-boot-tools", HERE / "tools",
        HERE.parent.parent / "work" / "u-boot-2026.07" / "tools",
        HERE.parent.parent / "work",
    ]

    def find_bin(name, extra=()):
        w = which(name)
        if w:
            return Path(w)
        for d in list(extra) + dirs:
            cand = Path(d) / name
            if cand.is_file() and os.access(cand, os.X_OK):
                return cand
        return None

    mk = find_bin("mkimage")
    di = find_bin("dumpimage")
    dtc = which("dtc")
    # fwtool 是可选的：Web U-Boot 刷 fit/rootfs 卷并不校验 sysupgrade metadata，
    # 只有系统启动后跑 `sysupgrade` 就地升级才会读它。没有 fwtool 也能出可刷产物。
    if cli_fwtool:
        fw = Path(cli_fwtool).expanduser()
        if not (fw.is_file() and os.access(fw, os.X_OK)):
            raise Fatal(f"--fwtool 指定的文件不可执行: {fw}")
    else:
        fw = find_bin("fwtool", extra=[HERE / "up", HERE.parent.parent / "work" / "up"])
    missing = [n for n, v in (("mkimage", mk), ("dumpimage", di), ("dtc", dtc))
               if not v]
    if missing:
        raise Fatal(
            "找不到可执行: " + ", ".join(missing) + "\n"
            "  mkimage/dumpimage 来自 U-Boot tools（--tools DIR 或系统包 uboot-tools/u-boot-tools），\n"
            "  dtc 用系统包（Arch: sudo pacman -S dtc；Debian: apt install device-tree-compiler）。\n"
            "  详见同目录 requirements.txt。")
    return mk, di, fw, Path(dtc)


# ---------- 1. 解 sysupgrade .bin（ustar） ----------

def unpack_sysupgrade(bin_path, build):
    try:
        tf = tarfile.open(bin_path)
    except tarfile.TarError as e:
        raise Fatal(f"输入不是 sysupgrade ustar tar: {e}")
    want = {"CONTROL": "CONTROL", "kernel": "kernel.fit", "root": "root.sqshfs"}
    got = {}
    for m in tf.getmembers():
        b = os.path.basename(m.name)
        if b in want and not m.isdir():
            (build / want[b]).write_bytes(tf.extractfile(m).read())
            got[b] = want[b]
    miss = [k for k in want if k not in got]
    if miss:
        raise Fatal(f"tar 里缺成员: {miss}")
    control = (build / "CONTROL").read_text(errors="replace")
    board = ""
    for line in control.splitlines():
        if line.startswith("BOARD="):
            board = line.split("=", 1)[1].strip()
    if (build / "root.sqshfs").read_bytes()[:4] != SQUASHFS_MAGIC:
        raise Fatal("root 成员不是 squashfs (magic != hsqs)")
    return board, {k: build / v for k, v in got.items()}


# ---------- 2/3. 内层 FIT -> 压缩内核 + stock dtb -> 原始 Image ----------

def split_inner_fit(dumpimage, kernel_fit, build):
    run([dumpimage, "-l", kernel_fit])                 # 打印清单，验证是 flat_dt
    kern_comp = build / "kern_comp"
    stock_dtb = build / "stock.dtb"
    run([dumpimage, "-T", "flat_dt", "-p", 0, "-o", kern_comp, kernel_fit])
    run([dumpimage, "-T", "flat_dt", "-p", 1, "-o", stock_dtb, kernel_fit])
    if stock_dtb.read_bytes()[:4] != DTB_MAGIC:
        raise Fatal("p1 不是 dtb (magic != d00dfeed)——内层 FIT 结构与预期不符")
    return kern_comp, stock_dtb


def decompress_kernel(kern_comp):
    """逐个尝试解压，返回第一个偏移 0x38 带合法 arm64 magic 的结果（不靠猜压缩头单字节）。"""
    b = Path(kern_comp).read_bytes()

    def ok(x):
        return len(x) > 0x40 and x[0x38:0x40] == ARM64_MAGIC

    def lzma_raw(lc, lp, pb):
        return lzma.decompress(b, format=lzma.FORMAT_RAW,
                               filters=[{"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb}])

    attempts = [
        ("gzip", lambda: gzip.decompress(b)),
        ("lzma-alone", lambda: lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(b)),
        ("xz", lambda: lzma.LZMADecompressor(format=lzma.FORMAT_XZ).decompress(b)),
        ("zlib-raw", lambda: zlib.decompress(b, -15)),
        ("lzma-raw(lc2)", lambda: lzma_raw(2, 0, 2)),
        ("lzma-raw(lc3)", lambda: lzma_raw(3, 0, 2)),
        ("none", lambda: b),
    ]
    tried = []
    for name, fn in attempts:
        try:
            raw = fn()
        except Exception as e:
            tried.append(f"{name}(fail:{type(e).__name__})")
            continue
        if ok(raw):
            return raw, name
        tried.append(f"{name}(magic错)")
    raise Fatal("无法从压缩内核解出合法 arm64 Image（试过: " + ", ".join(tried) + "）")


# ---------- 4/6. stock dtb -> dts -> B2 dtb ----------

def decompile_dtb(dtc, stock_dtb, build):
    out = build / "stock.dts"
    run([dtc, "-I", "dtb", "-O", "dts", "-o", out, stock_dtb], capture=True)
    txt = out.read_text(errors="replace")
    if "bootargs-append" not in txt:
        raise Fatal("stock.dts 没有 bootargs-append——输入可能不是 xg-040g-md 原版固件")
    m = re.search(r'model = "([^"]*)"', txt)
    return out, (m.group(1) if m else "<none>")


def build_b2_dts(stock_dts, ubid, build):
    sys.path.insert(0, str(HERE))
    import b2_dts
    src = Path(stock_dts).read_text(encoding="utf-8")
    try:
        bosa, fact = b2_dts.extract_stock_blocks(src)
        out = b2_dts.transform(src, ubid, bosa, fact)
    except AssertionError as e:
        raise Fatal(f"B2 结构断言失败（输入可能不是预期 stock 树）: {e}")
    dst = build / "b2.dts"
    dst.write_text(out, encoding="utf-8")
    return dst


def detect_ubid(boot_ips, timeout=4):
    """从 Web U-Boot GET /info 的 .ubi.vols[] 里找 n=='rootfs' 的 i。"""
    last = ""
    for ip in boot_ips:
        url = f"http://{ip}/info"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as e:
            last = f"{url} -> {type(e).__name__}: {e}"
            continue
        try:
            obj = json.loads(body)
        except Exception:
            last = f"{url} 返回的不是 JSON（板子可能不在 Web U-Boot 恢复页，而是已启动的 LuCI）"
            continue
        vols = (obj.get("ubi") or {}).get("vols") or []
        hit = next((v for v in vols if v.get("n") == "rootfs"), None)
        if hit is not None:
            try:
                return int(hit["i"]), f"{url}: rootfs vol_id={hit['i']}"
            except Exception:
                last = f"{url}: rootfs 卷 i 无法解析为整数: {hit!r}"
        else:
            names = ",".join(str(v.get("n")) for v in vols) or "<无 vols>"
            last = f"{url}: 没有名为 rootfs 的卷（现有: {names}）"
    return None, last


# ---------- 7/8. 组 FIT 打包 + metadata ----------

ITS_TEMPLATE = """/dts-v1/;
/ {
\tdescription = "ARM64 OpenWrt QWRT FIT (Flattened Image Tree)";
\t#address-cells = <1>;

\timages {
\t\tkernel-1 {
\t\t\tdescription = "ARM64 OpenWrt QWRT __KVER__";
\t\t\ttype = "kernel";
\t\t\tarch = "arm64";
\t\t\tos = "linux";
\t\t\tcompression = "gzip";
\t\t\tload = <0x80200000>;
\t\t\tentry = <0x80200000>;
\t\t\tdata = /incbin/("Image.gz");
\t\t\thash-1 {
\t\t\t\talgo = "crc32";
\t\t\t};
\t\t\thash-2 {
\t\t\t\talgo = "sha1";
\t\t\t};
\t\t};

\t\tfdt-1 {
\t\t\tdescription = "ARM64 OpenWrt nokia_xg-040g-md-ubi device tree blob";
\t\t\ttype = "flat_dt";
\t\t\tarch = "arm64";
\t\t\tcompression = "none";
\t\t\tdata = /incbin/("b2.dtb");
\t\t\thash-1 {
\t\t\t\talgo = "crc32";
\t\t\t};
\t\t\thash-2 {
\t\t\t\talgo = "sha1";
\t\t\t};
\t\t};
\t};

\tconfigurations {
\t\tdefault = "config-1";

\t\tconfig-1 {
\t\t\tdescription = "OpenWrt nokia_xg-040g-md-ubi";
\t\t\tkernel = "kernel-1";
\t\t\tfdt = "fdt-1";
\t\t};
\t};
};
"""


def make_itb(mkimage, image_gz, b2_dtb, out_itb, build, kver="Linux"):
    # .its 用相对名引用载荷；载荷通常已经在 build/ 里就叫这名字，此时不重复拷贝
    for src, name in ((image_gz, "Image.gz"), (b2_dtb, "b2.dtb")):
        dst = build / name
        if Path(src).resolve() != dst.resolve():
            shutil.copy(src, dst)
    (build / "b2.its").write_text(ITS_TEMPLATE.replace("__KVER__", kver), encoding="utf-8")
    tmp = build / "b2.itb.new"
    # 注意：mkimage 的 -B 按 16 进制解析，必须传 "0x1000" 字面量，不能传 4096
    run([mkimage, "-E", "-B", "0x1000", "-f", "b2.its", "b2.itb.new"], cwd=build)
    shutil.move(build / "b2.itb.new", out_itb)


def default_metadata(board, dist, ver):
    return {
        "metadata_version": "1.1",
        "compat_version": "1.0",
        "supported_devices": ["nokia,xg-040g-md", "nokia,xg-040g-md-ubi"],
        "version": {"dist": dist, "version": ver, "revision": ver,
                    "target": "airoha/an7581", "board": f"{board}-ubi"},
    }


def format_metadata(meta):
    """渲染成与已上板验证产物逐字节一致的 trailer：键值用 ': '、项间 ', '，
    但数组是紧凑的 [\"a\",\"b\"]（没有空格）。fwtool 会原样追加这个文件。"""
    sd = ",".join('"%s"' % x for x in meta["supported_devices"])
    v = meta["version"]
    return (
        '{ "metadata_version": "%s", "compat_version": "%s", "supported_devices":[%s], '
        '"version": { "dist": "%s", "version": "%s", "revision": "%s", "target": "%s", '
        '"board": "%s" } }\n' % (
            meta["metadata_version"], meta["compat_version"], sd,
            v["dist"], v["version"], v["revision"], v["target"], v["board"]))


def append_metadata(fwtool, itb, meta):
    meta_json = Path(itb).parent / "meta.json"
    meta_json.write_text(format_metadata(meta), encoding="utf-8")
    run([fwtool, "-I", meta_json, itb])
    readback = Path(itb).parent / "meta_read.json"
    run([fwtool, "-i", readback, itb], check=False)
    if readback.is_file():
        if json.loads(meta_json.read_text()) != json.loads(readback.read_text()):
            raise Fatal("metadata trailer 读回不一致")


# ---------- 9. 校验 ----------

def verify(dumpimage, itb, b2_dtb, image_gz, build):
    listing = run([dumpimage, "-l", itb], capture=True).stdout
    if "Loadables" in listing:
        raise Fatal("FIT 里出现了 Loadables（B2 应无 rootfs 节点/无 loadables）")
    if "Default Configuration" not in listing:
        raise Fatal("FIT 没有 default configuration（pbs05 裸 bootm 需要它）")
    ck, cf = build / "chk_kern.gz", build / "chk_fdt.dtb"
    run([dumpimage, "-T", "flat_dt", "-p", 0, "-o", ck, itb])
    run([dumpimage, "-T", "flat_dt", "-p", 1, "-o", cf, itb])
    if ck.read_bytes() != Path(image_gz).read_bytes():
        raise Fatal("kernel-1 载荷回比不一致")
    if cf.read_bytes() != Path(b2_dtb).read_bytes():
        raise Fatal("fdt-1 载荷回比不一致")
    return listing


def derive_prefix(bin_name):
    base = re.sub(r"\.bin$", "", bin_name)
    base = re.sub(r"-squashfs-sysupgrade$", "", base)
    base = re.sub(r"-sysupgrade$", "", base)
    return base or "firmware"


def derive_dist_ver(bin_name):
    m = re.match(r"^([A-Za-z][\w.]*)-([^-]+)-", bin_name)   # QWRT-R26.09.30-...
    if m:
        return m.group(1), m.group(2)
    m2 = re.match(r"^([^-]+)-", bin_name)
    if m2:
        return m2.group(1), "unknown"
    return "QWRT", "unknown"


# ---------- 主流程 ----------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="qwrt2an758x.py",
        description="把原版 QWRT sysupgrade .bin 移植成 xg-040g-md 的 uboot-an758x B2 FIT .itb",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例: python3 qwrt2an758x.py ../../QWRT-R26.09.30-...-squashfs-sysupgrade.bin --ubid 6")
    ap.add_argument("bin", help="原版 QWRT ...-squashfs-sysupgrade.bin")
    ap.add_argument("--ubid", type=int, help="rootfs 卷的 UBI 编号（不给则自动 GET /info 探测）")
    ap.add_argument("--boot-ip", action="append", default=[],
                    help="探测用 U-Boot 地址，可重复；默认依次试 192.168.1.1 192.168.0.1")
    ap.add_argument("-o", "--outdir", default=str(HERE / "out"), help="产物目录（默认 脚本目录/out）")
    ap.add_argument("--prefix", help="输出文件名前缀（默认取输入文件名去掉 squashfs-sysupgrade.bin）")
    ap.add_argument("--tools", help="U-Boot tools 目录（含 mkimage/dumpimage）")
    ap.add_argument("--fwtool", help="fwtool 路径")
    ap.add_argument("--dist", help="写进 metadata 的发行版名")
    ap.add_argument("--ver", help="写进 metadata 的版本")
    ap.add_argument("--keep-build", action="store_true", help="保留中间产物目录")
    args = ap.parse_args(argv)

    src_bin = Path(args.bin).expanduser().resolve()
    if not src_bin.is_file():
        die(f"找不到输入文件: {src_bin}", 2)

    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix="qwrt2itb.", dir=outdir))
    prefix = args.prefix or derive_prefix(src_bin.name)

    os.environ.setdefault("SOURCE_DATE_EPOCH", "1760000000")   # 可复现：mkimage 会读它

    print(f"输入 : {src_bin} ({src_bin.stat().st_size} bytes)")
    print(f"前缀 : {prefix}")
    print(f"临时 : {build}")

    try:
        mk, di, fw, dtc = find_tools(args.tools, args.fwtool)
        print(f"工具 : {mk} | {di} | {fw if fw else 'fwtool(缺失→跳过 metadata)'} | {dtc}")

        print("### 1. 解包 sysupgrade .bin (ustar tar)")
        board, parts = unpack_sysupgrade(src_bin, build)
        print(f"    BOARD={board!r}  kernel={parts['kernel'].stat().st_size}  "
              f"root={parts['root'].stat().st_size} (squashfs ✓)")
        if board and board != TARGET_BOARD:
            print(f"    警告: CONTROL BOARD={board}，本工具只保证 {TARGET_BOARD}，请自行核对。")

        print("### 2. 解析内层 kernel FIT (dumpimage)")
        kern_comp, stock_dtb = split_inner_fit(di, parts["kernel"], build)
        print(f"    kern_comp={kern_comp.stat().st_size}  stock.dtb={stock_dtb.stat().st_size}")

        print("### 3. 解出原始 ARM64 Image")
        image_raw, how = decompress_kernel(kern_comp)
        text_off = struct.unpack("<Q", image_raw[8:16])[0]
        (build / "Image").write_bytes(image_raw)
        print(f"    解压[{how}] -> Image {len(image_raw)} bytes  text_offset={text_off:#x}")
        kver = re.search(rb"Linux version (\d+\.\d+)", image_raw)
        kver = ("Linux-" + kver.group(1).decode()) if kver else "Linux"
        gz = build / "Image.gz"
        cli_gzip = shutil.which("gzip")
        if cli_gzip:
            # 首选系统 gzip -n -9：与已上板验证过的产物逐字节一致
            with open(build / "Image", "rb") as i, open(gz, "wb") as o:
                r = subprocess.run([cli_gzip, "-n", "-9", "-c"], stdin=i, stdout=o)
            if r.returncode != 0:
                raise Fatal("gzip -n -9 失败")
        else:
            with open(gz, "wb") as rawf, gzip.GzipFile(
                    filename="", mode="wb", fileobj=rawf, compresslevel=9, mtime=0) as f:
                f.write(image_raw)
        with gzip.open(gz, "rb") as f:
            assert f.read() == image_raw, "gzip 往返不一致"
        print(f"    Image.gz={gz.stat().st_size}")

        print("### 4. 反编译 stock dtb -> dts")
        stock_dts, model = decompile_dtb(dtc, stock_dtb, build)
        print(f"    stock.dts model={model!r}  ({stock_dts.stat().st_size} bytes)")

        print("### 5. 确定 rootfs 卷 UBID")
        if args.ubid is not None:
            ubid = args.ubid
            print(f"    命令行指定 UBID={ubid}")
        elif os.environ.get("UBID"):
            ubid = int(os.environ["UBID"])
            print(f"    环境变量 UBID={ubid}")
        else:
            ips = args.boot_ip or DEFAULT_BOOT_IPS
            ubid, why = detect_ubid(ips)
            if ubid is None:
                die("读不到 `rootfs` 卷的 UBI 编号。\n"
                    f"  原因: {why}\n"
                    "  这是第一次『启动失败』的唯一根因：bootargs 写死 root=/dev/ubiblock0_<UBID>，\n"
                    "  编号错就指向 bosa/ri 等非 squashfs 卷 -> VFS: Unable to mount root。\n"
                    "  解法：(a) 让板子停在 Web U-Boot 恢复页再跑（会自动 GET /info）；\n"
                    "        (b) 手动 --ubid <n>（恢复页『设备详情』里 rootfs 行最前面的数字）。", 3)
            print(f"    自动探测 UBID={ubid}  ({why})")
        if ubid < 0:
            die(f"--ubid 必须是非负整数，收到 {ubid}", 2)

        print(f"### 6. 生成 B2 dtb (UBID={ubid}, 无 fitblk/rootdisk/loadables)")
        b2_dts = build_b2_dts(stock_dts, ubid, build)
        b2_dtb = build / "b2.dtb"
        # -q：反编译再回编必然冒一堆「phandle 非引用」之类的良性告警，对机器产物毫无意义
        run([dtc, "-q", "-I", "dts", "-O", "dtb", "-o", b2_dtb, b2_dts])
        bootargs = None
        fdtget = shutil.which("fdtget")
        if fdtget:
            r = run([fdtget, b2_dtb, "/chosen", "bootargs-append"], capture=True, check=False)
            if r.returncode == 0:
                bootargs = r.stdout.strip()
        if bootargs is None:
            m = re.search(r'bootargs-append = "([^"]*)"', b2_dts.read_text())
            bootargs = m.group(1) if m else ""
        if "rootdisk" in bootargs:
            raise Fatal("rootdisk 泄漏进 chosen（fitblk 契约，QWRT 内核没有 fitblk）")
        if f"/dev/ubiblock0_{ubid} " not in bootargs + " ":
            raise Fatal(f"bootargs 未含 /dev/ubiblock0_{ubid}: {bootargs!r}")
        print(f'    bootargs-append = "{bootargs}"')

        print("### 7. mkimage 生成 FIT .itb (-E -B 0x1000, gzip@0x80200000)")
        itb = build / f"{prefix}-ubi-fit.itb"
        make_itb(mk, gz, b2_dtb, itb, build, kver)
        print(f"    {itb.name}={itb.stat().st_size}")

        print("### 8. fwtool 追加 OpenWrt metadata")
        dist, ver = derive_dist_ver(src_bin.name)
        dist = args.dist or dist
        ver = args.ver or ver
        meta = default_metadata(board or TARGET_BOARD, dist, ver)
        if fw:
            append_metadata(fw, itb, meta)
            print(f"    metadata: dist={dist} version={ver} board={meta['version']['board']} (读回 ✓)")
        else:
            print("    跳过（未找到 fwtool）。产物照常可刷；仅启动后跑 `sysupgrade` 就地升级才需要这段 metadata。")

        print("### 9. 校验")
        verify(di, itb, b2_dtb, gz, build)
        print("    无 Loadables ✓  载荷逐字节回比 ✓  default configuration ✓")

        print("### 10. 落地产物 + sha256")
        final_itb = outdir / f"{prefix}-ubi-fit.itb"
        final_root = outdir / f"{prefix}-rootfs.img"
        shutil.copy(itb, final_itb)
        shutil.copy(parts["root"], final_root)
        itb_hash = sha256_file(final_itb)
        (outdir / f"{prefix}-ubi-fit.itb.sha256sum").write_text(
            f"{itb_hash}  {prefix}-ubi-fit.itb\n")
        (outdir / f"{prefix}-rootfs.img.sha256sum").write_text(
            f"{sha256_file(final_root)}  {prefix}-rootfs.img\n")
        root_lebs = (final_root.stat().st_size + LEB - 1) // LEB
        probe_ip = (args.boot_ip or DEFAULT_BOOT_IPS)[0]
        print(f"""
================  完成  ================
产物目录: {outdir}
  {final_itb.name}  ({final_itb.stat().st_size} bytes)  -> 刷进 UBI 卷 fit
  {final_root.name}  ({final_root.stat().st_size} bytes, ~{root_lebs} LEB) -> 刷进 UBI 卷 rootfs
  {prefix}-ubi-fit.itb.sha256sum / {prefix}-rootfs.img.sha256sum
  itb sha256 = {itb_hash}

本次写入 dtb: root=/dev/ubiblock0_{ubid}  (UBID={ubid})

================  刷入步骤（Web U-Boot）  ================
0) 上电按住 Reset ~1s 进恢复页 http://{probe_ip}/ （或 192.168.0.1）
1) 「设备详情」核对 rootfs 卷编号是不是 {ubid}；不同就重跑 --ubid <那个数>。
2) 「日常刷机」上传 {final_itb.name}（只刷 fit 卷，保留 rootfs 卷）。
3) 「按卷写入」：仅当还没有 rootfs 卷或要更新根时，写 {final_root.name} 到卷 rootfs。
4) 千万别勾「重建 UBI」——会按新顺序重建卷、改 UBID、并清掉 rootfs 数据。
5) 「启动系统」。首次因 rootfs_data 被删而配置为空属正常（tmpfs overlay）。
""")
    finally:
        if args.keep_build:
            print(f"(保留中间产物: {build})")
        else:
            shutil.rmtree(build, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fatal as e:
        die(str(e), 1)
