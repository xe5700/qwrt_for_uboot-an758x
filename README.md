# qwrt2an758x — 原版 QWRT `.bin` → xg-040g-md Web-U-Boot 可刷的 B2 `.itb` 一键移植脚本

把 QWRT 官方编译出来的 `…-nokia_xg-040g-md-squashfs-sysupgrade.bin`，一条命令重新打包成
能直接从 **Airoha Web U-Boot 恢复页**（本项目 / pbs05 `uboot-an758x`）刷入的 **B2 格式** FIT `.itb`。

> 只支持 **nokia xg-040g-md**（分区表、别名路径、nvmem phandle 都是这块板写死的）。
> 详细原理与「和原版有何区别」见仓库根目录 [`QWRT-B2-说明.md`](../../QWRT-B2-说明.md)。

---

## 为什么是「B2」

QWRT 内核 **没有 `fitblk`**（只有 `ubiblock`/`blkdev`），所以不能走「rootfs 塞进 FIT 当
`loadables` + `rootdisk`」那条标准 OpenWrt FIT 路线（那是 Route A，会多占 ≈50 MB 常驻 initrd）。
B2 的做法：

- **FIT `.itb` 只装 kernel + 改好的 dtb**，刷进 UBI 的 `fit` 卷；
- **rootfs 是独立的 `rootfs` 卷**，用恢复页「按卷写入」单独刷 `…-rootfs.img`；
- dtb 里用 QWRT 自己的 `ubi.block=0,rootfs root=/dev/ubiblock0_<UBID>` **按名解析**根设备，
  **完全不碰 fitblk / rootdisk / loadables**。

`<UBID>` 是 **这块板上 rootfs 卷的 UBI 编号**，不是固定值——脚本会帮你算，见下。

---

## 依赖

| 需要 | 从哪来 | 备注 |
|------|--------|------|
| Python 3 | 系统 | 只用标准库，不需要 pip 包 |
| `dtc` | Arch: `sudo pacman -S dtc` | 反编译 / 回编设备树 |
| `mkimage` / `dumpimage` | U-Boot tools（脚本自动找 `../../work/u-boot-2026.07/tools`），或 `--tools DIR` | 解内层 FIT、打包外层 FIT |
| `fwtool` | OpenWrt（自动找 `../../work/up/fwtool`），或 `--fwtool PATH` | 追加 sysupgrade metadata |
| `gzip` | 系统 | 优先用 `gzip -n -9` 保证与已验证产物逐字节一致；没有则用 Python `gzip` 回退 |

> 脚本 **不依赖网络，也不需要 curl**：自动探测 UBID 用的是内置 `urllib`。

---

## 用法

```bash
cd tools/qwrt2an758x

# 最常用：已知本板 rootfs 卷编号（本项目当前板是 6）
python3 qwrt2an758x.py ../../QWRT-R26.09.30-airoha-an7581-nokia_xg-040g-md-squashfs-sysupgrade.bin --ubid 6

# 让板子停在 Web U-Boot 恢复页，脚本自动 GET /info 读 rootfs 卷编号
python3 qwrt2an758x.py <原版.bin>

# pbs05 uboot-an758x 的恢复页 IP 是 192.168.0.1，指定探测地址
python3 qwrt2an758x.py <原版.bin> --boot-ip 192.168.0.1

# 指定输出目录 / 文件前缀 / 保留中间产物排查
python3 qwrt2an758x.py <原版.bin> --ubid 6 -o ../../out --keep-build
```

产物默认落在 `tools/qwrt2an758x/out/`：

```
<前缀>-ubi-fit.itb        # → 恢复页「日常刷机」刷进 UBI 卷 fit
<前缀>-rootfs.img         # → 恢复页「按卷写入」刷进 UBI 卷 rootfs（仅在缺该卷/要更新根时才需要）
<前缀>-ubi-fit.itb.sha256sum
<前缀>-rootfs.img.sha256sum
```

### 全部选项

| 选项 | 作用 |
|------|------|
| `bin`（位置参数） | 原版 QWRT `…-squashfs-sysupgrade.bin` |
| `--ubid N` | **rootfs 卷的 UBI 编号**。不给则自动从 `GET /info` 探测；探测不到就**硬中止**（退出码 3），绝不瞎猜 |
| `--boot-ip IP` | 探测用地址，可重复；默认依次试 `192.168.1.1`（本项目）`192.168.0.1`（pbs05） |
| `-o, --outdir DIR` | 产物目录，默认 `脚本目录/out` |
| `--prefix NAME` | 输出文件名前缀，默认取输入名去掉 `squashfs-sysupgrade.bin` |
| `--tools DIR` | U-Boot tools 目录（含 `mkimage`/`dumpimage`） |
| `--fwtool PATH` | `fwtool` 路径 |
| `--dist / --ver` | 写进 metadata 的发行版名/版本（默认从文件名正则取，如 `QWRT` / `R26.09.30`） |
| `--keep-build` | 保留中间产物目录（默认成功即删；失败也会自动删，用这个开关来查）|

---

## 脚本内部流程（10 步，全自动，只从 `.bin` 出发）

1. `tarfile` 解 sysupgrade `.bin`（ustar）→ `CONTROL` / `kernel` / `root`；校验 `root` 是 squashfs
2. `dumpimage`（与 `bootm` 同一套 lib）拆内层 `kernel` FIT → 压缩内核 + stock dtb
3. 解出原始 ARM64 Image：**逐个试** gzip / lzma-alone / xz / zlib-raw / lzma-raw / none，
   以「偏移 0x38 == `ARMd\0\0\0\0`」为准（不靠猜压缩头单字节）；再 `gzip -n -9` 回去并往返校验
4. `dtc -I dtb -O dts` 反编译 stock dtb
5. 决定 `<UBID>`：`--ubid` > 环境变量 `UBID` > 自动 `GET /info`（取 `ubi.vols[]` 里 `n=="rootfs"` 的 `i`）
6. `b2_dts.transform()` 改写设备树（**全套断言**：无 rootdisk、无 `volname="rootfs"`、phandle 唯一、nvmem 引用可解析），`dtc` 回编
7. 生成 `.its`（gzip 内核 @`0x80200000`、`configurations.default="config-1"`）→ `mkimage -E -B 0x1000` **外部**打包
8. `fwtool -I` 追加 OpenWrt sysupgrade metadata，并 `-i` 读回比对
9. **全量校验**：`dumpimage -l` 断言**无 Loadables、有 default**；逐载荷回比 kernel/fdt 字节一致
10. 落地 `.itb` / `rootfs.img` / 两个 `.sha256sum`，并打印下一步的刷入指引

### 已验证：可复现

对本仓库那份原版 `.bin` 跑 `--ubid 6`，生成的 `.itb` 与**已上板启动成功**的那份
（sha256 `82fd8087…9ed25e`，6,365,468 B）**逐字节完全一致**（`cmp` 干净）。
即脚本 = 手工 `work/build_qwrt_itb_b2.sh` 产物的等价、可复现版本。

---

## ⚠️ UBID 是唯一会让人「刷进去起不来」的坑

`bootargs` 里的 `root=/dev/ubiblock0_<UBID>` 是**写死的编号**，而 UBI 卷编号按**创建顺序**排，
每块板可能不同。编号错 → 根设备指向 `bosa`/`ri` 等**非 squashfs 卷** → 内核 `VFS: Unable to mount root`。
（本项目第一次启动失败就是这个：stock dtb 里烤死的是 `root=/dev/ubiblock0_3`，而本板 rootfs 其实在 6。）

所以：

- 优先让板子停在恢复页，让脚本自动 `GET /info` 读编号；
- 或先在恢复页「设备详情」里看到 rootfs 行最前面的数字，再 `--ubid <那个数>`；
- 脚本探测不到时**一定硬中止**，不会拿默认值蒙混。

**勾了恢复页「重建 UBI」后，卷编号会变**，务必重新 `GET /info` 核对并用新的 `--ubid` 重刷，
或者直接**不要勾**「重建 UBI」（推荐）。

---

## 文件

- `qwrt2an758x.py` — 主脚本（CLI）
- `b2_dts.py`   — 设备树改写逻辑（`work/make_b2_dts.py` 的参数化副本，逐断言保留；也可 `python3 b2_dts.py` 单独跑，接口同旧版：`SRC_DTS`/`DST_DTS`/`UBID`）
