# QWRT → uboot-an758x（xg-040g-md）B2 固件说明

本文说明三件事：

1. 原版 QWRT 固件（`.bin`）到底是什么结构；
2. 我做出来的 **B2** 格式 `.itb` 与它的**逐项区别**（什么变了、什么**刻意没变**）；
3. 为什么必须这么变，以及刷机/回滚要注意什么。

配套的一键移植脚本见 [`qwrt2an758x.py`](qwrt2an758x.py)，环境准备见
[`requirements.txt`](requirements.txt)。

> 范围：**只针对 nokia xg-040g-md**。分区偏移、别名路径、nvmem phandle 都是这块板写死的。
> 刷入目标：**ImmortalWrt-Airoha（Loong1996）作者自研的 Airoha Web U-Boot**
> —— 标题里的 `uboot-an758x` 是仓库项目名，不是 pbs05/uboot-an758x（详见 §8）。

---

## 0. 结论先说（TL;DR）

| | 原版 QWRT | B2（本项目产物） |
|---|---|---|
| 文件名 | `QWRT-R26.09.30-airoha-an7581-nokia_xg-040g-md-squashfs-sysupgrade.bin` | `QWRT-R26.09.30-nokia_xg-040g-md-ubi-fit.itb`（+ `…-rootfs.img`） |
| 体积 | 57,908,000 B（单文件） | **6,365,468 B** 内核+DTB，**另** 53,218,304 B rootfs 卷 |
| sha256（前 16） | `9ecb2cf2212f612e` | `.itb` `82fd8087968e8d83`；`rootfs.img` `83170b7cf121c713` |
| 外层容器 | **ustar tar**（不是 FIT） | **FIT `.itb`** |
| 内核压缩 | **lzma** 4,660,123 B | **gzip** 6,333,276 B |
| 设备树 | 原版 dtb **23,605 B**（烤死 `ubiblock0_3`） | 改写后 dtb **23,995 B**（`ubiblock0_<UBID>`） |
| rootfs 在哪 | tar 里 `root` 成员（squashfs） | **独立 `rootfs` UBI 卷**（原样搬，一字节不改） |
| 能否 Web U-Boot 直接刷 | **不能**（恢复页只收 FIT） | **能** |
| 内核有没有 fitblk | 没有 | **不需要**（走 `ubiblock` 块设备） |
| 可用内存/保留内存 | 见 §4 | **与原版逐字节相同** |

一句话：**B2 = 原版内核 + 原版 rootfs + 只改了「怎么找到 rootfs」的设备树，
换成能被 Web U-Boot 接受的 FIT 容器。** 文件系统内容、驱动、内存布局一个都没换。

---

## 1. 原版 QWRT `.bin` 解剖

`…squashfs-sysupgrade.bin`（57,908,000 B）外层是 **ustar tar**（OpenWrt sysupgrade 的
传统打包），三个成员：

```
CONTROL   23 B    "BOARD=nokia_xg-040g-md"
kernel    4,685,120 B   ← 又是一个 FIT（内层）
root      53,218,304 B  ← squashfs（magic "hsqs"）
```

内层 `kernel` 这个 FIT 里：

```
Image 0 (kernel-1)  Kernel Image,  lzma compressed, 4,660,123 B,  Load 0x80200000
                    Description: ARM64 QWRT Linux-6.18.18    crc32 babfd35c
Image 1 (fdt-1)     Flat Device Tree, uncompressed,  23,605 B   crc32 cb9de6f6
Default Configuration: config-1
（没有 Loadables —— 这点很关键，见 §3）
```

解出来的 `Image` 是 14,311,432 B 的 ARM64 Image，`Linux version 6.18.18`。

**这个包刷不进网页 U-Boot 恢复页**：本项目刷入目标是 [Loong1996/ImmortalWrt-Airoha](https://github.com/Loong1996/ImmortalWrt-Airoha)
作者自研的 **Airoha Web U-Boot**（板上自报 `Airoha Web U-Boot 1.1.0 by Loong`，bootmenu「关于」也指向该项目）。
它的恢复页只接受 **FIT `.itb`**，而且刷的是**具体 UBI 卷**
（`fit` 卷 / `rootfs` 卷），不是 tar。原版 tar 只在**已启动的 OpenWrt 里**
由 `sysupgrade` 命令解开用。

---

## 2. flash 布局（两种格式都绕不开它）

板子 SPI-NAND 的 MTD 分区：

```
bl2   @ 0x00000      （bootloader，别碰）
fip   @ 0x20000 之后  实际布局：bl2@0x0 + ubi@0x20000
ubi   @ 0x20000      （UBI 池，其余全部空间）
```

UBI 池里的卷（**本项目这块板实测**，按创建顺序编号）：

```
i=0 fip        i=1 ubootenv    i=2 ubootenv2   i=3 bosa
i=4 ri         i=5 fit         i=6 rootfs      i=7 rootfs_data
 leb=126976  pebs=2047
```

- **`fit` 卷** = U-Boot 从这里读内核 + DTB（B2 的 `.itb` 就刷这个卷）。
- **`rootfs` 卷** = squashfs 根文件系统（B2 的 `rootfs.img` 刷这个卷）。
- **编号不是标准的**：`rootfs` 在别的板/重建 UBI 之后可能是别的数字，
  所以移植脚本要求你**显式核对或自动探测**，绝不拿默认值蒙。

---

## 3. 核心问题：内核没有 fitblk，怎么找到根？

这是整个移植的技术核心，也是「B2」这个名字的来由。

**另一条路（Route A）** 是把 rootfs 也塞进 FIT 里当一个子镜像，并在 `chosen` 里
写 `rootdisk` + `linux,fit-images` + `Loadables: rootfs-1`，靠内核的 **fitblk**
驱动在 FIT 里发现 rootfs 块设备。ImmortalWrt-Airoha 官方包就是这么做的。

问题：**QWRT 内核没编 fitblk。** 实测证据：

```
strings -a Image | grep -E '^(fitblk|ubiblock|blkdev)$'  →  只有 blkdev、ubiblock
```

没有 fitblk，`rootdisk` 那套契约就是死路：内核起得来但挂不上根，
或者直接 `VFS: Unable to mount root fs`。

而且把 53 MB rootfs 塞进 FIT 还有实打实的代价 —— ImmortalWrt 那个 Route A 包：

```
QWRT-…-squashfs-sysupgrade.itb   59,584,780 B   ← 内含 Image 2 (rootfs-1) 53,219,328 + Loadables
```

`Loadables` 意味着这 53 MB 会在启动时**被整块读进 RAM 常驻**（约 +50 MB），
在一个只有 512 MB 且有大块 `no-map` 保留内存的板子上纯属浪费。

**所以走 B2：独立 rootfs 卷 + `ubiblock` 块设备。**

- rootfs **不**进 FIT，保持它本来该在的位置（`rootfs` UBI 卷），原样搬，一字节不改；
- FIT 只装 **内核 + DTB**（所以 `.itb` 才 6 MB，很小）；
- DTB 的 `bootargs-append` 用内核**确实有**的驱动去找根：

```
ubi.mtd=ubi ubi.block=0,rootfs root=/dev/ubiblock0_<UBID> rootfstype=squashfs rootwait
```

这条路的产物实测起来就是「启动成功了」那次上板验证。

---

## 4. 逐项对比：改了什么 / 没改什么

### ✅ 刻意**保持不变**（这是 B2 的意义 —— 它就是原版 QWRT）

| 项 | 状态 |
|---|---|
| **rootfs 文件系统内容** | **逐字节相同**。`rootfs.img` sha256 `83170b7c…` 与 tar 里 `root` 成员一致，squashfs 没重压、没改配置 |
| **内核 Image 本体** | 同一个 6.18.18 Image，只是换了外层压缩算法（见下） |
| **驱动 / 固件包 / luci / QWRT 自带的一切** | 全在 rootfs 里，未动 |
| **内存布局** | `reserved-memory`（atf@80000000 / npu-binary@84000000 / qdma0-buf@87000000 / qdma1-buf@89000000，48 MB `no-map`）与 `linux,usable-memory-range = <0x00 0x80200000 0x00 0x7fe00000>` **与原版逐字节相同** |
| **MTD 分区表 / flash 布局** | 相同（`bl2@0x0` + `ubi@0x20000`） |
| **控制台** | `console=ttyS0,115200 earlycon`，`stdout-path = "serial0:115200n8"` |
| **DTB 其余内容** | 除 §4·改 里列的几处，`chosen` 之外几乎全等（反编译 diff 仅 **+36 / −10 行**） |

### 🔧 **改了什么**（以及为什么）

| # | 改动 | 原版 | B2 | 原因 |
|---|---|---|---|---|
| 1 | **外层容器** | ustar tar | **FIT `.itb`** | Web U-Boot 恢复页只收 FIT |
| 2 | **内核压缩** | lzma 4,660,123 B | **gzip 6,333,276 B** | 与**已知可启动**的 ImmortalWrt 官方 `ubi-squashfs-sysupgrade.itb` 一致（它的 `kernel-1` 也是 gzip）；体积大 ~1.6 MB。注意这台 U-Boot `CONFIG_LZMA=y`/`CONFIG_GZIP=y` **都支持**（recovery 镜像就是 lzma 且能起），所以这不是「lzma 不能用」，而是对齐已验证格式 + `gzip -n -9` 输出确定、可逐字节复现 |
| 3 | **rootfs 位置** | tar `root` 成员 | **独立 `rootfs.img`，刷 `rootfs` 卷** | 内核无 fitblk，不能靠 FIT 发现根（§3） |
| 4 | **FIT 子镜像** | kernel-1 + fdt-1（+ Route A 才有 rootfs-1） | **只有 kernel-1 + fdt-1，无 rootfs-1，无 Loadables** | 避免 53 MB 常驻 RAM；校验步骤会**主动拒绝**出现 `Loadables` |
| 5 | **DTB 里 `root=`** | 烤死 `root=/dev/ubiblock0_3` | `root=/dev/ubiblock0_<UBID>`（本板 6） | **原版这个 `_3` 就是第一次启动失败的唯一原因**：本板 `3` 是 `bosa` 卷，不是 squashfs |
| 6 | **DTB 分区/别名子树** | — | 重写 `partitions`/`aliases`，卷节点 `all_flash@0_all`/`bl2@0`/`ubi@20000`，`pon-calibration` 指到 `…/ubi-volume-bosa` | 让设备树与 `bl2@0x0 + ubi@0x20000` 实际布局对齐；nvmem phandle（`0x28`/`0x35`）有断言保护 |
| 7 | **`fitblk` 契约节点** | Route A 有 `rootdisk`/`linux,fit-images` | **删掉，且脚本会硬中止防泄漏** | 内核没有 fitblk，留着只会误导 |
| 8 | **DTB 体积** | 23,605 B | 23,995 B | 多了上面几处节点/属性；crc32 `b7795095` |
| 9 | **metadata trailer** | tar 的 `CONTROL` | `fwtool -I` 追加 260 B JSON | `sysupgrade` 就地升级用；**刷入阶段不校验**，所以缺 `fwtool` 也能出可刷产物（脚本自动跳过） |

B2 外层 FIT 实测：

```
Image 0 (kernel-1)  gzip,      6,333,276 B,  Load 0x80200000   crc32 919b4ef3
Image 1 (fdt-1)     uncompressed 23,995 B                      crc32 b7795095
Default Configuration: config-1   (Description: OpenWrt nokia_xg-040g-md-ubi)
无 Loadables ✓
```

`config-1` 作为 **default configuration** 存在是有意的：本机（Loong 的 Web U-Boot）环境里
`bootconf=config-1`，bootcmd 写的是 `bootm $loadaddr#$bootconf`；而另一套同家族 U-Boot
（pbs05/uboot-an758x）的 bootcmd 是裸 `bootm $loadaddr`。带上 default 之后两种写法都能自动选对配置。

---

## 5. 移植/使用方法

装好环境（见 [`requirements.txt`](requirements.txt)：`python3 dtc uboot-tools gzip`，
Arch/Debian/Ubuntu/Fedora 各一条命令），然后：

```bash
cd tools/qwrt2an758x

# 已知本板 rootfs 卷编号（本板是 6）
python3 qwrt2an758x.py <原版>.bin --ubid 6

# 或让脚本自动探测：把板子停在 Web U-Boot 恢复页，脚本会 GET /info 读编号
python3 qwrt2an758x.py <原版>.bin --boot-ip 192.168.1.1
```

产物：`out/<前缀>-ubi-fit.itb`（刷 `fit` 卷）+ `out/<前缀>-rootfs.img`（刷 `rootfs` 卷）。

**可复现性**：脚本产出的 `.itb` 与已上板验证的产物 **`cmp` 逐字节一致**
（6,365,468 B，`82fd8087…`）。

刷机（Web U-Boot 恢复页，地址按你板子实际，本项目是 `192.168.1.1`）：

1. **日常刷机**：选 `.itb` → 目标卷 `fit` → 刷入 → 启动系统。
2. **`rootfs` 卷内容不在板上时才需要**：选 `rootfs.img` → 目标卷 `rootfs` → 写入。
3. **不要勾「重建 UBI」**。重建会改变卷编号，`root=/dev/ubiblock0_<UBID>` 就失效了，
   必须重新核对编号并用新 `--ubid` 重刷。
4. 刷 `rootfs` 卷会清掉 `rootfs_data` 的影响：**首次开机是空配置属正常**（Wi-Fi/密码需重配）。

---

## 6. ⚠️ 唯一会让人「刷进去起不来」的坑：UBID

`root=/dev/ubiblock0_<UBID>` 里的编号是**写死在 DTB 里**的，而 UBI 卷编号按
**创建顺序**分配，**每块板可能不同**、**重建 UBI 后会变**。
编号错 → 根设备指向 `bosa`/`ri` 等非 squashfs 卷 → `VFS: Unable to mount root`。

本项目**第一次启动失败就是这个原因**（stock DTB 烤死了 `_3`，本板 rootfs 实际在 `6`）。

脚本的三条防护（都已实测）：

- 板子停在恢复页时自动 `urllib` GET `/info`，从 `vols[]` 里找 `n=="rootfs"` 取它的 `i`；
- 探测不到又没给 `--ubid` → **硬中止**（exit 3）并给出可操作提示，**不会用默认值蒙混**；
- 给了 `--ubid` 就直接用它改写 DTB（如 `--ubid 3`，DTB crc32 会随之变化）。

---

## 7. 回滚 / 各镜像说明（这些文件在父工作目录，不在本仓库内）

| 文件 | 体积 | 是什么 | 用途 |
|---|---|---|---|
| `QWRT-R26.09.30-…-squashfs-sysupgrade.bin` | 57,908,000 | **原版 QWRT** | 回滚源 |
| `QWRT-R26.09.30-…-ubi-fit.itb` | 6,365,468 | **B2**（上板验证过） | 当前在用 |
| `QWRT-R26.09.30-…-rootfs.img` | 53,218,304 | B2 的 rootfs 卷镜像 | 配套刷 `rootfs` 卷 |
| `QWRT-…-squashfs-sysupgrade.itb` | 59,584,780 | **Route A**（rootfs 进 FIT + Loadables） | **不能用于 QWRT**（内核无 fitblk）；仅作对比 |
| `immortalwrt-…-ubi-squashfs-sysupgrade.itb` | 19,730,714 | ImmortalWrt 官方包（**有 fitblk**） | fitblk 路线基线 / 回滚 |
| `immortalwrt-…-ubi-initramfs-recovery(1).itb` | 17,039,360 | ImmortalWrt recovery（initramfs） | 救砖（起进 RAM，不依赖 rootfs 卷） |

**回滚到原版**：Web U-Boot 恢复页没法直接吃 `.bin`（tar）。两条路：
(a) 起进 ImmortalWrt recovery（initramfs）后用 `sysupgrade` 刷原版 `.bin`；
(b) 保留一份 B2，之后只在 OpenWrt 里用 `sysupgrade` 正常升级。

---

## 8. 刷入目标是谁，以及它与 pbs05/uboot-an758x 的差别

**本项目的刷入目标是 ImmortalWrt-Airoha 作者 Loong 自研的 Airoha Web U-Boot**
（板上证据：`web_uboot_show_about` 打印 `Airoha Web U-Boot 1.1.0 by Loong`，
bootmenu「关于」= `github.com/Loong1996/ImmortalWrt-Airoha`）。
仓库名里的 `an758x` 只是项目名，**不是**指 [pbs05/uboot-an758x](https://github.com/pbs05/uboot-an758x)
（“U-boot for AN758X ONU”，同 SoC 家族的另一套 U-Boot）。

两者经常被放在一起对照是因为 ImmortalWrt-Airoha 主动对齐了 pbs05：其
`README.md:13` 与 CI 文案都写着「闪存按 ECC4 读写，**与 pbs05/uboot-an758x 相同**」。
下面这张表是实测对照（本机环境 dump `work/live_env.json` vs 抓取的 pbs05 env/dts 片段
`work/pbs/`，**这两个文件都不在本仓库内**）：

| 项 | 本机 Loong Web U-Boot（实测） | pbs05/uboot-an758x | 对 B2 的影响 |
|---|---|---|---|
| bootm 形式 | `bootm $loadaddr#$bootconf`（`bootconf=config-1`） | 裸 `bootm $loadaddr` | **B2 里 `config-1` 是 default configuration**，两种写法都能选对配置 → 双向兼容 |
| 恢复页 IP | `ipaddr=192.168.1.1` | `ipaddr/httpd_ipaddr=192.168.0.1` | 只是访问地址不同；脚本 `--boot-ip` 传你板子的实际 IP（默认先试 `.1.1` 再试 `.0.1`） |
| `loadaddr` | `0x90000000` | `0x90000000` | 相同 ✓ |
| 控制台 | `earlycon=uart8250,mmio32,0x11002000 console=ttyS0` | 同 | 相同 ✓ |
| 卷名 | `fip / ubootenv / ubootenv2 / bosa / ri / fit / rootfs / rootfs_data` | 同名（`bosa`/`ri` 声明为 0x40000 dynamic） | 相同 ✓，`fit`+`rootfs` 卷语义一致 |
| flash 布局 | `bl2@0x0` + `ubi@0x20000` | 同（分区表一致） | 分区**布局**相同 ✓ |
| BL2 写入过程 | `mw.b $loadaddr 0xff 0x800` + 从 `+0x800` 载文件，`mtd write bl2 $loadaddr`（按 `$filesize`） | `mw.b … 0xff 0x20000` + `itest.l $filesize -le 0x1f800` 守卫，`mtd write bl2 $loadaddr 0 0x20000`（整块 0x20000） | **不同**。B2 **完全不碰 bl2/fip**，此差异不影响移植产物 |
| NAND ECC | ECC4 / spare 28 | ECC4 | **相同**（不是差异；是 Loong 把 BL2/U-Boot/Linux 从 ECC8 改成 ECC4 对齐 pbs05） |

结论：**B2 的 `.itb` 在这两套 U-Boot 下应都能被 `bootm` 正确选中并启动**——
因为 B2 只依赖「FIT 里有 default configuration」+ `fit`/`rootfs` 卷语义，
这两点双方一致；B2 不写 bl2/fip，所以 BL2 写入流程的差异也无关。

> **验证边界要说清楚**：实机验证（「启动成功了」）只发生在 **Loong 的 Airoha Web U-Boot 1.1.0** 上。
> pbs05/uboot-an758x 一侧的兼容是**按 env 对照推导**的结论，本机没有那块板、未上板实测。

---

## 9. 同工作目录里的对照 / 参考文件（不在本仓库内）

移植过程中在**父工作目录**（不是本仓库）留下的取证与对照材料，列在这里便于回溯：

- 原版 `…-squashfs-sysupgrade.bin`、已验证 B2 `.itb` / `rootfs.img`、Route A `.itb`、
  ImmortalWrt 官方 `ubi-squashfs-sysupgrade.itb` 与 `ubi-initramfs-recovery.itb`（见 §7 表）。
- `work/qwrt_b2.its` / `work/qwrt_b2_meta.json` —— 手工构建金样用的 ITS 与 260 B metadata，
  脚本以它为逐字节基准。
- `work/qwrt_fdt.dts`、`work/ubi_b2.dts`、`work/make_b2_dts.py` —— 设备树改写的原始素材与
  一次性版本（[`b2_dts.py`](b2_dts.py) 是它的参数化副本）。
- `work/u-boot-2026.07/tools/{mkimage,dumpimage}`、`work/up/fwtool`、`work/up/fwtool-src/` ——
  本机在用的工具（Debian/Fedora 仓库都没有 `fwtool`，它是 OpenWrt 侧的
  <https://git.openwrt.org/project/fwtool.git>）。
- `work/pbs/*`（pbs05 env/dts 片段）、`work/live_env.json`（本机 U-Boot 环境 dump）。
- ImmortalWrt-Airoha 上游：<https://github.com/Loong1996/ImmortalWrt-Airoha>
  （`README.md`、`docs/uboot-http-recovery.md`、`docs/variants.md` —— Web U-Boot 恢复页、
  卷与格式约定、各变体差异）。

