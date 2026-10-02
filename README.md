# qwrt2an758x — 原版 QWRT `.bin` → xg-040g-md Web-U-Boot 可刷的 B2 `.itb` 一键移植脚本

把 QWRT 官方编译出来的 `…-nokia_xg-040g-md-squashfs-sysupgrade.bin`，一条命令重新打包成
能直接从 **ImmortalWrt-Airoha 网页 U-Boot 恢复页**刷入的 **B2 格式** FIT `.itb`。
这套 Web U-Boot 是 [Loong1996/ImmortalWrt-Airoha](https://github.com/Loong1996/ImmortalWrt-Airoha)
作者自研的（板上自报 `Airoha Web U-Boot 1.1.0 by Loong`，恢复页「关于」也指向该项目）。

> 只支持 **nokia xg-040g-md**（分区表、别名路径、nvmem phandle 都是这块板写死的）。
> 仓库名里的 `an758x` 只是项目名。**刷入目标不是 pbs05/uboot-an758x** —— 那是同 SoC
> 家族的另一套 U-Boot，本项目只与它在 ECC4 布局、卷名、bootcmd 写法上互相参考
> （对比见 [`QWRT-B2-说明.md`](QWRT-B2-说明.md) §8）。
> 详细原理与「和原版有何区别」见同目录 [`QWRT-B2-说明.md`](QWRT-B2-说明.md)；
> 环境准备见同目录 [`requirements.txt`](requirements.txt)。

---

## 刷入目标（别搞混）

产物是刷 **ImmortalWrt-Airoha 作者 Loong 自研的 Airoha Web U-Boot**（[Loong1996/ImmortalWrt-Airoha](https://github.com/Loong1996/ImmortalWrt-Airoha)）。
板子上能直接验证这一点：bootmenu 第 9 项「关于」= `github.com/Loong1996/ImmortalWrt-Airoha`，
`web_uboot_show_about` 打印 `Airoha Web U-Boot 1.1.0 by Loong`；恢复页 IP 默认 `192.168.1.1`。

仓库名里的 `an758x` **不是** [pbs05/uboot-an758x](https://github.com/pbs05/uboot-an758x)（同 SoC 家族的另一套
U-Boot，恢复页 IP 是 `192.168.0.1`）。本项目只与它在 ECC4 布局、卷名、bootcmd 写法上互相参考：
ImmortalWrt-Airoha 主动把 BL2/U-Boot/Linux 从 ECC8 改成了与 pbs05 一致的 ECC4（见其 `README.md:13`）。
逐条对照见 [`QWRT-B2-说明.md`](QWRT-B2-说明.md) §8。因为 B2 的 FIT 里 `config-1` 是 default configuration，两套 U-Boot 的
`bootm $loadaddr#$bootconf` 与裸 `bootm $loadaddr` 在**结构上**都能选中同一份配置。
（注意：**实机验证只做了 Loong 这一套** —— 「启动成功了」那台跑的是 Airoha Web U-Boot 1.1.0；
pbs05 那一侧是按 env 对照推导，没有上板试过。）

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

## 警告
如果你刷入了本修改版固件，请不要在QWRT固件里的升级界面里执行任何升级，因为本脚本并没有对它进行更改，输入了之后会无法启动，
你如果真要刷入请手动使用dd刷入，或者重启到U-Boot界面进行刷入。

---

## 准备环境

脚本自身 **只用 Python 3 标准库，不需要 `pip install` 任何东西**；真正的前置条件是
4 个宿主命令行工具。完整清单与各发行版安装命令见
[`requirements.txt`](requirements.txt)（该文件全是注释，`pip install -r` 是空操作，
当说明书看即可）。

### 需要的工具

| 需要 | 作用 | 说明 |
|------|------|------|
| `python3` | 运行脚本 | ≥ 3.8 |
| `dtc` | 反编译 / 回编设备树 | 改写 `root=/dev/ubiblock0_<UBID>` 那一步 |
| `mkimage` | 打包外层 FIT `.itb` | U-Boot tools |
| `dumpimage` | 拆内层 kernel FIT + 校验 | U-Boot tools，与 `bootm` 同一套 lib |
| `fwtool` | **可选**：追加 sysupgrade metadata | Web U-Boot 刷 `fit`/`rootfs` 卷**不校验**它；缺失时脚本自动跳过该步，产物照常可刷。只有系统启动后用 `sysupgrade` 就地升级才需要 |
| `gzip` | 重压缩内核 | 优先用系统 `gzip -n -9`（与已上板验证产物逐字节一致）；没有则回退 Python 内置 gzip |

> 脚本**不依赖网络，也不需要 curl**：自动探测 UBID 用的是内置 `urllib`。

### 按发行版一条命令装好

**Arch Linux**（本项目的实际验证环境）

```bash
sudo pacman -S --needed python dtc uboot-tools gzip
```

**Debian 12/13（bookworm/trixie）**

```bash
sudo apt update && sudo apt install -y python3 device-tree-compiler u-boot-tools gzip
```

**Ubuntu 22.04 / 24.04（jammy/noble）** — 与 Debian 同名同命令

```bash
sudo apt update && sudo apt install -y python3 device-tree-compiler u-boot-tools gzip
```

**Fedora 39+ / RHEL 系**

```bash
sudo dnf install -y python3 dtc uboot-tools gzip
```

注意两点：

- 包名不同：Debian/Ubuntu 叫 `u-boot-tools` + `device-tree-compiler`，
  Arch/Fedora 叫 `uboot-tools` + `dtc`。
- **`fwtool` 四个发行版的仓库里都没有**（它只存在于 OpenWrt 侧）。需要 metadata 时源码构建：

```bash
git clone https://git.openwrt.org/project/fwtool.git
cd fwtool && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build
# 用 --fwtool ./build/fwtool 指向它，或放进 PATH
```

### 装完自检

```bash
python3 --version && dtc --version && mkimage -V && dumpimage -V
command -v fwtool || echo "无 fwtool：将自动跳过 metadata（不影响刷入）"
```

### 不想装系统包？用现成的 U-Boot tools 目录

`mkimage`/`dumpimage` 也可以是你在别的 U-Boot 源码树里编出来的
`tools/mkimage`、`tools/dumpimage`，直接告诉脚本那个目录：

```bash
python3 qwrt2an758x.py <原版.bin> --ubid 6 --tools /path/to/u-boot/tools
```

脚本还会自动搜 `./u-boot-tools/`、`./tools/`、`../../work/u-boot-2026.07/tools/`、
`../../work/`（后两个是本仓库工作目录的便捷路径，独立 clone 时不存在，属正常）。
真找不到会明确报错列出缺哪个，不会静默失败。

---

## 用法

```bash
cd tools/qwrt2an758x

# 最常用：已知本板 rootfs 卷编号（本项目当前板是 6）
python3 qwrt2an758x.py ../../QWRT-R26.09.30-airoha-an7581-nokia_xg-040g-md-squashfs-sysupgrade.bin --ubid 6

# 让板子停在 Web U-Boot 恢复页，脚本自动 GET /info 读 rootfs 卷编号
python3 qwrt2an758x.py <原版.bin>

# 若板子刷的是 pbs05/uboot-an758x，它的恢复页 IP 是 192.168.0.1，指定探测地址
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
| `--boot-ip IP` | 探测用地址，可重复；默认依次试 `192.168.1.1`（ImmortalWrt-Airoha 网页 U-Boot）`192.168.0.1`（pbs05/uboot-an758x） |
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
- `requirements.txt` — 环境要求：无 pip 依赖，4 个系统工具 + Arch/Debian/Ubuntu/Fedora 各自安装命令 + `fwtool` 源码构建方法
- `QWRT-B2-说明.md` — 详细原理，以及 B2 与原版 QWRT 的逐项区别（什么变了、什么刻意没变、为何这么变）
