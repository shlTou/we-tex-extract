# we-tex-extract

把 Wallpaper Engine 壁纸（`scene` 类型）里的图片提取出来，变成一个普通的 PNG/JPG 文件。

> **English** — A zero-dependency Python tool that extracts the images out of Wallpaper Engine
> `scene` wallpapers. It unpacks `scene.pkg` (`PKGV*`), then decodes the `TEXV/TEXI/TEXB`
> textures: DXT1 / DXT5 / DXT3 / RGBA8888 / R8 / RG88, LZ4-compressed payloads, and textures
> that simply embed a PNG or JPEG. No third-party packages, no Wallpaper Engine code — the
> formats were reverse-engineered (see [`docs/FORMAT.md`](docs/FORMAT.md)). Windows, Python 3.8+.

![icon](assets/icon.png)

## 为什么需要它

Wallpaper Engine 的 scene 壁纸把图片全部封在 `scene.pkg` 里，而包里是 `.tex` 纹理，
所以改扩展名打不开，用普通解压软件也解不出来 —— 它既不是 zip 也不是 tar。

这个工具做了三件事：解包 → 解码纹理 → 输出图片。全程**只读**源文件。

## 快速开始

### 方式一：下载 exe（免安装）

到 [Releases](../../releases) 下载 `we-extract.exe`，把壁纸文件夹拖到它上面即可。
不需要装 Python、不需要装任何库。

### 方式二：跑源码

```bash
python we_extract.py "E:\SteamLibrary\steamapps\workshop\content\431960\1234567890"
```

只需要 Python 3.8+，不用 `pip install` 任何东西。

### 输出

图片写到壁纸目录下的 `images\`：

```
壁纸: Ayanami Rei - 凌波丽『Starry』   类型: scene
输出: ...\431960\1234567890\images

PKGV0023 -> 34 个文件, 其中 5 个纹理
  [OK] 109电脑                                          3840x2160  0.19s
  [OK] iris_movement___mask_774b6459                   1920x1080  0.64s
  [OK] shake_mask_81bfb131                             1920x1080  1.50s
  [OK] Flare_01                                        1000x800   1.52s
  [OK] Star_04                                         1600x1600  1.53s

主图 -> Ayanami Rei-凌波丽『Starry』_3840x2160.jpg
共 5 张图 -> ...\images  (耗时 1.5s)
```

- 像素面积最大的那张会按 `<标题>_<宽>x<高>` 命名，方便一眼认出主图
- 其余保留材质原名；重名才加序号
- **重复跑没有副作用**：目标文件已存在且内容相同就跳过，不会堆出一堆副本

### 参数

| 参数 | 说明 |
| --- | --- |
| `-o <目录>` | 指定输出目录（默认写到壁纸目录下的 `images\`） |
| `--keep` | 保留解包出来的原始 `scene.pkg` 内容，便于分析 |

## 支持的格式

| 层 | 支持情况 |
| --- | --- |
| 容器 | `PKGV0001` ~ `PKGV00xx`（entry offset/size 两种字节序自动判定） |
| 纹理头 | `TEXV0005` / `TEXI0001` / `TEXB0001`~`TEXB0004` |
| 像素格式 | `0` RGBA8888、`4` DXT5、`7` DXT1、`1`/`2` DXT3、`8` RG88、`9` R8 |
| 压缩 | 无压缩、LZ4 块压缩（注意：不是 LZ4 frame，没有魔数） |
| 内嵌文件 | 纹理里直接塞 PNG / JPEG / GIF / WebP 时原样落地，**不重新编码** |
| 壁纸类型 | `scene` 完整支持；`video` 直接拷贝 mp4/webm；`web` 不支持 |

遇到没见过的格式会**显式报错**并打印 `fmt`、每像素字节数、载荷开头若干字节，
而不是猜一个格式硬解出垃圾图。

## 已知限制

- 只导出每张纹理的**最大 mipmap**，不导出多级 mip 链
- `web` 类型壁纸不处理（它本来就是散文件，直接拿就行）
- 解不出来的情况请带上报错信息开 issue，注明壁纸的 workshop id

## 从源码打包 exe

```bash
pip install pyinstaller
pyinstaller --onefile --console --icon assets/app.ico --name we-extract we_extract.py
```

产物在 `dist/we-extract.exe`。仓库里配置了 GitHub Actions：
打一个 `v*` 的 tag 就会自动构建并上传到 Release（见 `.github/workflows/release.yml`）。

## 格式文档

逆向出来的格式说明在 [`docs/FORMAT.md`](docs/FORMAT.md)，包含 `scene.pkg` 的目录结构、
`.tex` 的两级头、mipmap 表、LZ4 与 DXT 的细节，以及几个踩过的坑（比如 BC2/BC3 的颜色块
按 DirectX 规范必须恒用 4 色模式）。想自己写解析器的话可以直接参考。

## 版权与免责

- 本项目**不包含** Wallpaper Engine 的任何代码，格式信息来自对文件本身的分析
- 提取出来的图片版权归**原画师/壁纸作者**所有。本项目只帮你把自己已经拥有的壁纸取出来，
  请勿将提取结果二次分发或商用
- 使用本项目产生的任何后果由使用者自行承担

## License

[MIT](LICENSE)
