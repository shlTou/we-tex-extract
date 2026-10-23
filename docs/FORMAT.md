# 逆向记录：Wallpaper Engine 的 scene.pkg 与 .tex

自下而上按层记录，全部结论都经过「解出的数据能正好铺满文件」或「与 C++ 参考实现
逐像素对齐」验证。字节序：小端。

---

## 1. `scene.pkg` 容器

```
[uint32 magicLen][char magic[magicLen]]     # 例: 08 00 00 00 "PKGV0023"
[uint32 entryCount]
entry * entryCount:
    [uint32 pathLen][char path[pathLen]]
    [uint32 a][uint32 b]                     # (offset, size)，offset 相对数据区起点
[数据区]                                      # 按 entry 顺序紧排
```

判定 `a`/`b` 谁是谁的办法：两种解释都试一次，看哪一种能让数据区
`entryCount` 个文件**正好铺满**文件剩余部分（slack 越接近 0 越可信）。同一个包里
不同版本的写法可能有差异。

包里除了 `.tex`，还会有 `scene.json`（图层树）、`materials/*.json`（材质/着色器参数）、
`sounds/*.mp3`、`fonts/*.otf`、`shaders/*.frag|vert`，以及被引用到的其它工坊资源。

## 2. `.tex` 纹理

### 2.1 两级头

```
"TEXV0005" 0x00                      # 8 字节魔数 + 1 字节 0x00（每个魔数都是 9 字节）
"TEXI0001" 0x00
    int32 format                      # 见下表
    int32 flags
    int32 texWidth, texHeight          # 4 像素对齐后的尺寸（可能比真实尺寸大）
    int32 imgWidth, imgHeight          # 真实尺寸，最终要按这个裁剪
    uint32 hash
"TEXB0001".."TEXB0004" 0x00
```

注意：**魔数占 9 字节**（8 字符 + 1 个 `0x00`），所以后面所有 int32 都不是 4 字节对齐的。
拿固定偏移读会读出垃圾，必须按顺序读。

### 2.2 像素格式

| `format` | 含义 | 每像素 | 备注 |
| --- | --- | --- | --- |
| 0 | RGBA8888 | 4 B | 载荷**不一定**是裸像素，见 2.4 |
| 1 / 2 | DXT3 / BC2 | 1 B | 16 字节一块 |
| 4 | DXT5 / BC3 | 1 B | 16 字节一块（alpha 8 B 在前，颜色 8 B 在后） |
| 7 | DXT1 / BC1 | 0.5 B | 8 字节一块 |
| 8 | RG88 | 2 B | 蒙版，输出成灰度+alpha |
| 9 | R8 | 1 B | 蒙版，输出成灰度 |

用 `rawSize / (width * height)` 反推每像素字节数，是识别未知格式最快的办法。

### 2.3 mipmap 表

```
int32 imageCount                       # 通常 1
... 0~2 个额外字段 ...                  # 不同版本写法不同，见下
int32 mipmapCount
mipmap * mipmapCount:
    int32 w, h
    int32 compression                  # 0 = 原样存, 1 = LZ4 块压缩
    int32 rawSize                      # 解压后大小（未压缩时为 0 或等于 storedSize）
    int32 storedSize
    uint8 data[storedSize]             # 按 mipmap 顺序紧排
```

`imageCount` 与 `mipmapCount` 之间那 0~2 个字段随版本变化（见过 1 个和 2 个的），
**不要写死偏移**。可靠做法是枚举「跳过 0/1/2/3 个 int32」这几种可能，取能精确消费
到文件末尾的那一种。

### 2.4 `format 0` 的载荷可能是一张现成的图片

很多作者（和 WE 自己）会直接把一张 PNG 或 JPEG 塞进 mipmap，此时：

```
rawSize = 0
storedSize = 那张图的字节数
data[0:4] = 89 50 4e 47 (PNG) 或 ff d8 ff (JPEG)
```

**优先原样落地**：不重新编码，画质无损、速度也最快。只认 PNG 不认 JPEG 是个典型 bug，
会让 4K 内嵌 JPEG 的图报「数据尺寸不足」。

### 2.5 LZ4 块压缩

不是 LZ4 frame（没有 `0x184D2204` 魔数），就是裸的 block：token(1B) → literal 长度
（高 4 位，=15 则继续读字节累加）→ literal 字节 → offset(2B) → match 长度（低 4 位 + 4，
=15 同理累加）。解到 `rawSize` 为止即可。

## 3. DXT 解码要点

### 3.1 块布局

| 格式 | 块大小 | 布局 |
| --- | --- | --- |
| DXT1 (BC1) | 8 B | color(8) |
| DXT3 (BC2) | 16 B | alpha(8) + color(8) |
| DXT5 (BC3) | 16 B | alpha(8) + color(8) |

**BC2/BC3 是 alpha 在前、颜色在后**。读反了不会报错，只会输出错色。

### 3.2 颜色索引的位序

4×4 块内 16 个像素，每像素 2 bit，**按小端顺序**：`index = bit1 * 2 + bit0`，即
第一个像素用最低两位。把这两位顺序弄反会让索引 1 和 2 互换 —— 画面只是"发虚"、
色带略微偏移，很难一眼看出来，但确实错了。

### 3.3 3 色模式只属于 DXT1

DXT1 里 `color0 <= color1` 表示 3 色 + 透明（`index == 3` 是透明黑）。

**BC2/BC3 没有这个行为**：按 DirectX 规范它们的颜色块**恒用 4 色模式**，
`color0`/`color1` 的比较被忽略。有些第三方解码库这里不严谨（会套用 DXT1 的规则），
拿它当参考时要小心。

### 3.4 alpha 插值（BC3）

```
a0 > a1 : a[2..7] = ((8-i)*a0 + (i-1)*a1) / 7      i = 2..7
a0 <= a1: a[2..5] = ((6-i)*a0 + (i-1)*a1) / 5      i = 2..5
          a[6] = 0, a[7] = 255
```

常见错误：把 `a0>a1` 时的 `a[7]` 写成 0（应该是 `(a0+6a1)/7`），会让边缘半透明像素变全透明。

## 4. 验证方法

不要只靠肉眼看"图好像对了"。可行的手段：

1. **铺满校验** —— 解包后所有 entry 的 `offset+size` 应该正好等于文件长度
2. **字节数校验** —— `rawSize` 必须等于 `width * height * 字节/像素`
3. **交叉验证** —— 用独立实现（如 `texture2ddecoder`，基于 bcdec）跑同一批随机块，
   逐像素比对；注意它返回的是 **BGRA**，直接当 RGBA 比会得出"全错"的结论
4. **构造用例** —— 手算规范值，比如 `a0=255, a1=0` 时期望
   `[255, 0, 218, 182, 145, 109, 72, 36]`

最容易踩的坑是**「不报错」被当成「解对了」**：未知格式落进某个默认分支，尺寸刚好
对得上，于是输出一堆噪点却没人发现。
