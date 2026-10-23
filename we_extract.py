# -*- coding: utf-8 -*-
"""we-tex-extract —— 从 Wallpaper Engine 壁纸（scene 类型）里提取图片

用法:
    python we_extract.py <壁纸目录> [-o 输出目录] [--keep]
    python we_extract.py <scene.pkg 路径> [-o 输出目录]

特性:
    * 零第三方依赖，只用标准库（zlib/struct），任何 Python 3.8+ 都能直接跑
    * 自动解 scene.pkg（PKGV0001~PKGV00xx），自动识别 .tex 的格式与压缩方式
    * 支持 RGBA8888 / DXT1 / DXT5 / DXT3 / R8 / RG88，以及内嵌 PNG/JPEG 直出
    * 只读源文件，输出到壁纸目录下的 images\\，绝不修改原包

格式细节见 docs/FORMAT.md。以 MIT 许可发布。
"""
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import zlib

BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PNG_SIG = b"\x89PNG\r\n\x1a\n"


# ----------------------------------------------------------------- 输出
def save_png(path, width, height, data, channels):
    """data: 行优先像素字节；channels: 3=RGB, 4=RGBA"""
    stride = width * channels
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                  # filter: none
        raw += data[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 6)

    def chunk(typ, payload):
        return (struct.pack(">I", len(payload)) + typ + payload
                + struct.pack(">I", zlib.crc32(typ + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8,
                       {1: 0, 2: 4, 3: 2, 4: 6}[channels],   # 灰度 / 灰度+alpha / RGB / RGBA
                       0, 0, 0)
    with open(path, "wb") as f:
        f.write(PNG_SIG)
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", comp))
        f.write(chunk(b"IEND", b""))


# ----------------------------------------------------------------- LZ4
def lz4_block(src, out_size=None):
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if lit:
            out += src[i:i + lit]
            i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        if off <= 0:
            raise ValueError("lz4 offset")
        mlen = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        start = len(out) - off
        if mlen <= off:
            out += out[start:start + mlen]
        else:
            for k in range(mlen):
                out.append(out[start + k])
        if out_size is not None and len(out) >= out_size:
            break
    if out_size is not None and len(out) != out_size:
        raise ValueError("lz4 size %d != %d" % (len(out), out_size))
    return bytes(out)


# ----------------------------------------------------------------- DXT
_LUT = None


def _lut():
    global _LUT
    if _LUT is None:
        t = []
        for c in range(65536):
            r = (c >> 11) & 0x1F
            g = (c >> 5) & 0x3F
            b = c & 0x1F
            t.append(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
        _LUT = t
    return _LUT


def _decode_blocks(raw, mw, mh, fmt, channels, four_color):
    """DXT1 / BC3 / BC2 -> bytearray，行优先，跨距 = mw*channels

    块布局: DXT1 = 颜色 8 字节
            BC3  = alpha 8 字节 + 颜色 8 字节
            BC2  = alpha 8 字节 + 颜色 8 字节
    """
    lut = _lut()
    bw, bh = (mw + 3) // 4, (mh + 3) // 4
    stride = mw * channels
    out = bytearray(stride * mh)
    pos = 0
    three_color = False
    for by in range(bh):
        row0 = (by * 4) * stride
        for bx in range(bw):
            alphas = None
            alpha_pal = None
            if fmt != 7:                          # 先读 8 字节 alpha
                if fmt == 4:                      # BC3: 2 端点 + 6 字节 3bit 索引
                    a0 = raw[pos]
                    a1 = raw[pos + 1]
                    abits = 0
                    for k in range(6):
                        abits |= raw[pos + 2 + k] << (8 * k)
                    if a0 > a1:
                        av = ([a0, a1] + [((8 - i) * a0 + (i - 1) * a1) // 7
                                          for i in range(2, 8)])
                    else:
                        av = ([a0, a1] + [((6 - i) * a0 + (i - 1) * a1) // 5
                                          for i in range(2, 6)] + [0, 255])
                    alphas = [av[(abits >> (3 * k)) & 7] for k in range(16)]
                else:                             # BC2: 4bit 显式 alpha
                    alphas = []
                    for k in range(8):
                        byte = raw[pos + k]
                        alphas.append((byte & 0x0F) * 17)
                        alphas.append((byte >> 4) * 17)
                pos += 8

            c0 = raw[pos] | (raw[pos + 1] << 8)
            c1 = raw[pos + 2] | (raw[pos + 3] << 8)
            bits = (raw[pos + 4] | (raw[pos + 5] << 8)
                    | (raw[pos + 6] << 16) | (raw[pos + 7] << 24))
            pos += 8

            r0, g0, b0 = lut[c0]
            r1, g1, b1 = lut[c1]
            if fmt == 7 and not four_color and c0 <= c1:
                three_color = True                # DXT1 3 色模式，index3 全透明
                r2 = (r0 + r1) // 2
                g2 = (g0 + g1) // 2
                b2 = (b0 + b1) // 2
                r3 = g3 = b3 = 0
                a3 = 0
            else:
                r2 = (2 * r0 + r1) // 3
                g2 = (2 * g0 + g1) // 3
                b2 = (2 * b0 + b1) // 3
                r3 = (r0 + 2 * r1) // 3
                g3 = (g0 + 2 * g1) // 3
                b3 = (b0 + 2 * b1) // 3
                a3 = 255
            if alphas is None:
                alpha_pal = (255, 255, 255, a3)

            # 每行 4 个像素正好占 8 bit，逐个 byte 取索引，避免 16 次临时 bytes
            if channels == 3:
                pal = (bytes((r0, g0, b0)), bytes((r1, g1, b1)),
                       bytes((r2, g2, b2)), bytes((r3, g3, b3)))
                for py in range(4):
                    y = by * 4 + py
                    if y >= mh:
                        break
                    t = (bits >> (8 * py)) & 0xFF
                    seg = (pal[t & 3] + pal[(t >> 2) & 3]
                           + pal[(t >> 4) & 3] + pal[t >> 6])
                    off = row0 + py * stride + bx * 4 * 3
                    n = (mw - bx * 4)
                    if n >= 4:
                        out[off:off + 12] = seg
                    elif n > 0:
                        out[off:off + 3 * n] = seg[:3 * n]
            else:
                rgb = (bytes((r0, g0, b0)), bytes((r1, g1, b1)),
                       bytes((r2, g2, b2)), bytes((r3, g3, b3)))
                apal = alpha_pal if alphas is None else None
                for py in range(4):
                    y = by * 4 + py
                    if y >= mh:
                        break
                    t = (bits >> (8 * py)) & 0xFF
                    seg = bytearray()
                    base = 4 * py
                    for j in range(4):
                        v = (t >> (2 * j)) & 3
                        seg += rgb[v]
                        seg.append(apal[v] if apal is not None else alphas[base + j])
                    off = row0 + py * stride + bx * 16
                    n = (mw - bx * 4)
                    if n >= 4:
                        out[off:off + 16] = seg
                    elif n > 0:
                        out[off:off + 4 * n] = seg[:4 * n]

    return out, three_color


# ----------------------------------------------------------------- tex
def solve_layout(b):
    fmt, flags, tw, th, iw, ih = struct.unpack_from("<6i", b, 0x12)
    base = 0x37
    exact = best = None
    for skipn in range(0, 4):
        pos = base + 4 + 4 * skipn
        if pos + 4 > len(b):
            continue
        mc = struct.unpack_from("<i", b, pos)[0]
        pos += 4
        if not (1 <= mc <= 24):
            continue
        mips, p, ok = [], pos, True
        try:
            for _ in range(mc):
                w, h, comp, raw_size, st = struct.unpack_from("<5i", b, p)
                p += 20
                if not (1 <= w <= 16384 and 1 <= h <= 16384 and comp in (0, 1)
                        and st > 0 and p + st <= len(b)):
                    ok = False
                    break
                mips.append((w, h, comp, raw_size, b[p:p + st]))
                p += st
        except Exception:  # noqa: BLE001
            ok = False
        if not ok or not mips:
            continue
        lay = (fmt, iw, ih, mips)
        if p == len(b):
            exact = exact or lay
        elif best is None or p > best[0]:
            best = (p, lay)
    return exact or (best[1] if best else None)


_IMG_SIGS = ((b"\x89PNG\r\n\x1a\n", "png"),
             (b"\xff\xd8\xff", "jpg"),
             (b"GIF87a", "gif"),
             (b"GIF89a", "gif"))


def sniff_image(d):
    """返回 'png'/'jpg'/'gif'/'webp'，不是自描述图片则 None"""
    if not d:
        return None
    for sig, ext in _IMG_SIGS:
        if d.startswith(sig):
            return ext
    if d[:4] == b"RIFF" and d[8:12] == b"WEBP":
        return "webp"
    return None


def image_size(d, ext):
    """读出真实像素尺寸，失败返回 None"""
    try:
        if ext == "png":
            return struct.unpack_from(">II", d, 16)
        if ext == "gif":
            return struct.unpack_from("<HH", d, 6)
        if ext == "jpg":
            i, n = 2, len(d)
            while i + 9 < n:
                if d[i] != 0xFF:
                    i += 1
                    continue
                m = d[i + 1]
                if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7 or m == 0x01:
                    i += 2
                    continue
                ln = struct.unpack_from(">H", d, i + 2)[0]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                         0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack_from(">HH", d, i + 5)
                    return w, h
                i += 2 + ln
    except Exception:  # noqa: BLE001
        pass
    return None


def _dump(outbase, payload, ext, mw, mh):
    outpath = outbase + "." + ext
    with open(outpath, "wb") as f:
        f.write(payload)
    got = image_size(payload, ext) or (mw, mh)
    return outpath, got[0], got[1]


def _scan_images(b):
    """在任意字节流里找最大的内嵌 PNG / JPEG，返回 (payload, ext, w, h)"""
    best = None
    i = 0
    while True:                                   # PNG
        i = b.find(PNG_SIG, i)
        if i < 0:
            break
        w, h = struct.unpack_from(">II", b, i + 16)
        p, end = i + 8, None
        while p + 8 <= len(b):
            ln = struct.unpack_from(">I", b, p)[0]
            if b[p + 4:p + 8] == b"IEND":
                end = p + 12 + ln
                break
            p += 12 + ln
        if not end:
            i += 8
            continue
        if best is None or w * h > best[2] * best[3]:
            best = (b[i:end], "png", w, h)
        i = end
    i = 0
    while True:                                   # JPEG
        i = b.find(b"\xff\xd8\xff", i)
        if i < 0:
            break
        j, end = i + 2, None
        while j + 4 <= len(b):
            if b[j] != 0xFF:
                j += 1
                continue
            m = b[j + 1]
            if m == 0xD9:
                end = j + 2
                break
            if m == 0x01 or 0xD0 <= m <= 0xD7:
                j += 2
                continue
            ln = struct.unpack_from(">H", b, j + 2)[0]
            j += 2 + ln
        if end:
            sz = image_size(b[i:end], "jpg")
            if sz and (best is None or sz[0] * sz[1] > best[2] * best[3]):
                best = (b[i:end], "jpg", sz[0], sz[1])
            i = end
        else:
            i += 3
    return best


def convert_tex(path, outbase):
    """解码一个 .tex。返回 (输出路径, 宽, 高)；无法处理返回 None

    载荷优先级:
      ① 本身就是 PNG/JPEG/GIF/WebP -> 原样落盘（最保真，不重编码）
      ② LZ4 解开之后才是图片       -> 同上
      ③ RGBA8888 裸像素
      ④ DXT1 / DXT5 / BC2 块解码
    """
    b = open(path, "rb").read()
    if b[:8] != b"TEXV0005":
        return None
    lay = solve_layout(b)
    if lay is None:
        got = _scan_images(b)
        return _dump(outbase, got[0], got[1], got[2], got[3]) if got else None

    fmt, iw, ih, mips = lay
    mw, mh, comp, raw_size, data = mips[0]

    ext = sniff_image(data) if comp == 0 else None
    if ext:
        return _dump(outbase, data, ext, mw, mh)

    raw = data if comp == 0 else lz4_block(data, raw_size)

    if fmt == 0:
        ext = sniff_image(raw)
        if ext:
            return _dump(outbase, raw, ext, mw, mh)
        need = mw * mh * 4
        if len(raw) < need:
            got = _scan_images(b)                 # 最后再捞一遍
            if got:
                return _dump(outbase, got[0], got[1], got[2], got[3])
            raise ValueError(
                "未压缩纹理数据不足: 需要 %d 字节(= %dx%dx4)，实际只有 %d 字节，"
                "载荷开头 %s" % (need, mw, mh, len(raw), raw[:8].hex(" ")))
        px = raw[:need]
        channels = 4
        cw, ch = iw, ih
    elif fmt == 9:                                 # R8: 单通道灰度蒙版
        need = mw * mh
        if len(raw) < need:
            raise ValueError("R8 数据不足: 需要 %d, 实际 %d" % (need, len(raw)))
        px = raw[:need]
        channels = 1
        cw, ch = min(iw, mw), min(ih, mh)
    elif fmt == 8:                                 # RG88: 双通道蒙版
        need = mw * mh * 2
        if len(raw) < need:
            raise ValueError("RG88 数据不足: 需要 %d, 实际 %d" % (need, len(raw)))
        px = raw[:need]
        channels = 2
        cw, ch = min(iw, mw), min(ih, mh)
    elif fmt in (7, 4, 1, 2):                      # DXT1 / DXT5 / DXT3
        channels = 3
        if fmt == 7:
            # DXT1 若有 3 色块就存在透明像素，需要 alpha
            bw, bh = (mw + 3) // 4, (mh + 3) // 4
            has3 = False
            for k in range(bw * bh):
                p = k * 8
                if (raw[p] | (raw[p + 1] << 8)) <= (raw[p + 2] | (raw[p + 3] << 8)):
                    has3 = True
                    break
            if has3:
                channels = 4
        else:
            channels = 4
        px, _ = _decode_blocks(raw, mw, mh, fmt, channels, False)
        cw, ch = mw, mh
    else:
        raise ValueError(
            "未知纹理格式 fmt=%d (%dx%d, 每像素 %.2f 字节)，"
            "载荷开头 %s" % (fmt, mw, mh, len(raw) / float(mw * mh), raw[:8].hex(" ")))

    # 裁掉 4 像素对齐的 padding，按真实尺寸写 PNG
    cw, ch = min(cw, iw), min(ch, ih)
    stride = mw * channels
    outpath = outbase + ".png"
    if (cw, ch) == (mw, mh) and stride * mh == len(px):
        save_png(outpath, cw, ch, px, channels)
    else:
        buf = bytearray()
        for y in range(ch):
            buf += px[y * stride:y * stride + cw * channels]
        save_png(outpath, cw, ch, buf, channels)
    return outpath, cw, ch


# ----------------------------------------------------------------- pkg
def parse_pkg(path):
    blob = open(path, "rb").read()
    pos = 0
    mlen = struct.unpack_from("<I", blob, pos)[0]
    pos += 4
    magic = blob[pos:pos + mlen].decode("ascii", "replace")
    if not magic.startswith("PKGV"):
        raise ValueError("不是 Wallpaper Engine pkg")
    pos += mlen
    count = struct.unpack_from("<I", blob, pos)[0]
    pos += 4
    ents = []
    for _ in range(count):
        plen = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        name = blob[pos:pos + plen].decode("utf-8", "replace")
        pos += plen
        a, c = struct.unpack_from("<II", blob, pos)
        pos += 8
        ents.append([name, a, c])
    data_start = pos
    best = None
    for swapped in (False, True):
        pairs = [((e[2], e[1]) if swapped else (e[1], e[2])) for e in ents]
        if any(o + s > len(blob) - data_start or s == 0 for o, s in pairs):
            continue
        slack = (len(blob) - data_start) - sum(s for _, s in pairs)
        if best is None or (0 <= slack < best[0]):
            best = (slack, swapped)
    if best is None:
        raise ValueError("条目表解析失败")
    swapped = best[1]
    out = []
    for name, a, c in ents:
        off, size = (c, a) if swapped else (a, c)
        out.append((name, off, size))
    return magic, out, blob, data_start


# ----------------------------------------------------------------- main
def safe_name(s, limit=60):
    s = BAD.sub("_", s or "").strip().strip(".")
    return (s or "wallpaper")[:limit]


def unique(d, name, hint=""):
    stem, ext = os.path.splitext(name)
    cand = name
    if os.path.exists(os.path.join(d, cand)):
        cand = "%s_%s%s" % (stem, safe_name(os.path.basename(hint)) or "x", ext)
    i = 2
    while os.path.exists(os.path.join(d, cand)):
        cand = "%s_%d%s" % (stem, i, ext)
        i += 1
    return cand


def place(src, outdir, name):
    """把 src 落到 outdir/name。目标已存在且内容相同 -> 直接跳过（重跑不产生副本）"""
    dst = os.path.join(outdir, name)
    if os.path.exists(dst):
        try:
            same = (os.path.getsize(dst) == os.path.getsize(src)
                    and open(src, "rb").read() == open(dst, "rb").read())
        except Exception:  # noqa: BLE001
            same = False
        if same:
            os.remove(src)
            return dst, True
        stem, ext = os.path.splitext(name)
        i = 2
        while True:
            cand = os.path.join(outdir, "%s_%d%s" % (stem, i, ext))
            if not os.path.exists(cand):
                dst = cand
                break
            i += 1
    shutil.move(src, dst)
    return dst, False


def main():
    args = [a for a in sys.argv[1:]]
    keep = "--keep" in args
    if keep:
        args.remove("--keep")
    out = None
    if "-o" in args:
        i = args.index("-o")
        out = args[i + 1]
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return 1
    src = os.path.abspath(args[0])
    if os.path.isfile(src) and src.lower().endswith(".pkg"):
        root, pkg = os.path.dirname(src), src
    else:
        root, pkg = src, None
    if not os.path.isdir(root):
        print("目录不存在: %s" % root)
        return 1
    out = os.path.abspath(out) if out else os.path.join(root, "images")
    os.makedirs(out, exist_ok=True)

    title, wtype = None, None
    pj = os.path.join(root, "project.json")
    if os.path.isfile(pj):
        try:
            import json
            proj = json.load(open(pj, encoding="utf-8"))
            title, wtype = proj.get("title"), proj.get("type")
        except Exception:  # noqa: BLE001
            pass
    print("壁纸: %s   类型: %s" % (title or os.path.basename(root), wtype or "?"))
    print("输出: %s" % out)
    print()

    if not pkg:
        cands = [f for f in os.listdir(root) if f.lower().endswith(".pkg")]
        pkg = os.path.join(root, cands[0]) if cands else None
    if not pkg:
        media = [f for f in os.listdir(root)
                 if f.lower().endswith((".mp4", ".webm", ".webp", ".gif", ".jpg", ".png"))]
        for f in media:
            shutil.copy2(os.path.join(root, f), os.path.join(out, unique(out, f)))
            print("  [拷贝] %s" % f)
        if not media:
            print("这里既没有 .pkg 也没有能直接用的视频/图片。")
            return 1
        return 0

    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="we_")
    try:
        magic, ents, blob, ds = parse_pkg(pkg)
        ext = os.path.join(tmp, "extracted")
        texs = []
        for name, off, size in ents:
            safe = os.path.normpath(name).replace("..", "_")
            dst = os.path.join(ext, safe)
            os.makedirs(os.path.dirname(dst) or ext, exist_ok=True)
            with open(dst, "wb") as f:
                f.write(blob[ds + off:ds + off + size])
            if safe.lower().endswith(".tex"):
                texs.append((dst, name))
        print("%s -> %d 个文件, 其中 %d 个纹理" % (magic, len(ents), len(texs)))

        n = 0
        top = None
        for dst, name in sorted(texs):
            stem = os.path.splitext(os.path.basename(name))[0]
            target = os.path.join(tmp, "out")
            os.makedirs(target, exist_ok=True)
            tp = os.path.join(target, stem)
            try:
                got = convert_tex(dst, tp)
            except Exception as e:  # noqa: BLE001
                print("  [失败] %s: %s" % (stem, e))
                continue
            if not got:
                print("  [跳过] %s" % stem)
                continue
            src_path, w, h = got
            final, same = place(src_path, out, stem + os.path.splitext(src_path)[1])
            n += 1
            if top is None or w * h > top[0]:
                top = (w * h, final, w, h)
            print("  [OK] %-46s %dx%d  %.2fs%s"
                  % (stem[:46], w, h, time.time() - t0,
                     "  (已存在, 跳过)" if same else ""))

        # 最大的那张按标题命名，方便一眼认出主图
        if top and title:
            src = top[1]
            newname = "%s_%dx%d%s" % (safe_name(title), top[2], top[3],
                                      os.path.splitext(src)[1])
            if os.path.basename(src) != newname:
                moveto, _ = place(src, out, newname)
                print("\n主图 -> %s" % os.path.basename(moveto))
        print("\n共 %d 张图 -> %s  (耗时 %.1fs)" % (n, out, time.time() - t0))
        return 0
    finally:
        if keep:
            print("中间文件保留在: %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:                       # 输出被重定向到奇怪编码时不至于崩
        sys.stdout.reconfigure(errors="replace")
    except Exception:          # noqa: BLE001
        pass
    rc = main()
    try:                       # 双击/拖拽时窗口别一闪而过
        if sys.stdin is not None and sys.stdin.isatty():
            input("\n按回车键退出...")
    except Exception:          # noqa: BLE001
        pass
    sys.exit(rc)
