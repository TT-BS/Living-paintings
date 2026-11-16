# Living Paintings · 让画活起来

一个给 **Claude Code** 和 **Codex** 用的技能（skill）：把一幅静止的画——水彩、油画、插画、作品照片——变成在网页上轻轻活起来的“动态画”。风吹过树枝、光从墙上移过、屋顶上的猫眨一下眼，而笔触、构图和气质仍是原来那幅画。

全部在你自己的电脑上完成：不上传图片，不下载模型。

![示例：技能自带的示例画，芦苇和灯笼随风摆动，烟慢慢飘，窗里的灯光闪烁](docs/demo.gif)

<sub>上图是技能自带的示例（`examples/night-house`，由脚本画出），用来演示文件怎样跑通。</sub>

## 它怎么工作

代理（Claude 或 Codex）负责“看”和“判断”，工具只做眼睛和判断做不到的事：

1. **读画**：先用一句话写下画意，选出最能传达它的主导动态（风、光、水面、一个眨眼……），再写清因果：风从哪来、先到谁、谁该静止、为什么。
2. **切层、补底**：沿真实的轮廓把要动的东西切成图层，用原画自己的颜料补上它挡住的地方。
3. **建运动**：全画只有一个风、一个光、一个镜头，各层按自己的材料响应；生命体的眨眼、偏头在自身层内完成；散落的花瓣会翻转、按所在处的明暗重新受光。
4. **打包**：生成一个无依赖的 WebGL 播放器，双击就能打开的单文件 `index.html`，以及放进网站的 `site/`。离屏自动暂停、对开启“减少动态效果”的人显示原画、带暂停按钮。
5. **自己检查**：用本机 Chrome 无头渲染逐帧取证，看破洞、残影、滑动、笔触变形和节奏，修原因而不是缩小幅度。

## 快速开始

```bash
git clone https://github.com/TT-BS/Living-paintings.git ~/living-paintings
```

```bash
~/living-paintings/scripts/install.sh --apply --with-cli
```

```bash
living-picture doctor
```

然后在 Claude Code 里说一句“让这幅画活起来”，附上图片或给出路径即可。

## 环境要求

| 需要 | 用途 | 说明 |
|---|---|---|
| Python 3.9+，带 numpy、scipy、Pillow | 读图、切层、补底、打包 | `python3 -m pip install numpy scipy pillow` |
| Node.js 22+ | 取帧检查、录视频 | `brew install node` |
| Chrome、Chromium 或 Edge | 无头渲染检查 | 用临时配置运行，不碰你的 Chrome 资料和钥匙串 |
| ffmpeg（可选） | 输出 MP4 | Mac 上没有或运行出错时自动改用系统的 AVFoundation |
| Xcode 命令行工具（可选，仅 Mac） | Vision 识别、AVFoundation 视频、精确读取 HEIC 和相机 RAW | `xcode-select --install`；没有它时，不透明的 HEIC 和 RAW 改用系统自带的 `sips` 转换 |

核心工具（切层、补底、打包、检查）是跨平台的 Python 和 Node 脚本，但目前只在 macOS 上实测过；在其他系统上，Mac 专属功能会明确提示不可用，`install.sh` 和 `living-picture` 需要类 Unix 的 shell。

`living-picture doctor` 会逐项检查以上环境，并告诉你缺什么、怎么装。

## 安装

一份正本，用符号链接装给各个工具。安装脚本默认只预演，加 `--apply` 才真正执行；从不覆盖已有的文件夹或指向别处的链接。

```bash
cd ~/living-paintings
scripts/install.sh                     # 预演：列出将建哪些链接
scripts/install.sh --apply --with-cli  # 执行
```

| 链接 | 给谁用 | 何时创建 |
|---|---|---|
| `~/.claude/skills/living-picture` | Claude Code | 默认 |
| `~/.agents/skills/living-picture` | Codex（它的用户技能目录） | 存在 `~/.agents` 或用过 Codex 时；`--only agents` 强制创建 |
| `~/.local/bin/living-picture` | 终端命令 | 加 `--with-cli` 时 |

- 更新：`cd ~/living-paintings && git pull`（链接指向这个文件夹，无需重装）。
- 卸载：`scripts/install.sh --uninstall --apply`，只删除指向这里的链接。
- `~/.local/bin` 不在 PATH 里时，安装脚本会提示要加到 `~/.zprofile` 的那一行。

## 使用

### 在 Claude Code 里（推荐）

直接说出你的要求，附上图片或路径：

> 让这幅画活起来：~/Pictures/garden.jpg，放在网站首页，宽约 1200 px，安静一点。

也可以输入 `/living-picture`。代理会按 `SKILL.md` 走完整个流程，把结果放进一个工作目录，并告诉你：让什么活了、为什么，什么故意留着不动，哪里还不够好。

### 在 Codex 里

安装后输入 `$living-picture`，或在 `/skills` 里选择它，再给出图片。两边读的是同一份 `SKILL.md`。

### 命令行（分步）

`living-picture` 是所有工具的统一入口，每个命令都有 `--help`，输出 JSON：

```bash
living-picture new ~/Pictures/garden.jpg --job ~/LivingPictures/garden   # 建工作目录：去平台水印、准备工作图、网格图、notes.md
living-picture vision ~/LivingPictures/garden/work/picture.png --out ~/LivingPictures/garden/work/vision   # 仅 Mac：识别起点
living-picture cut ~/LivingPictures/garden/plan.json                     # 按计划切层、补底，写审查图
living-picture bundle ~/LivingPictures/garden/scene.json --out ~/LivingPictures/garden/out   # 打包播放器
living-picture capture ~/LivingPictures/garden/out/index.html --out ~/LivingPictures/garden/check --every 0.5 --duration 12   # 取帧
living-picture capture ~/LivingPictures/garden/out/index.html --out ~/LivingPictures/garden/video --video 20                 # 录 20 秒视频
```

`plan.json`（怎么切层）和 `scene.json`（怎么动）通常由代理根据读画来写；格式和完整词汇见 `references/cutting.md` 与 `references/motion.md`。其他工具：`look`（带刻度网格看图）、`measure`（量边界、眼睛、轮廓）、`palette`（取色）、`inspect`（逐层或逐件检查）、`sheet`（帧条、节奏图）、`diff`、`dewatermark`、`paths`。

### 一次处理多幅画

`workflows/living-picture-batch.js` 是一个 Claude Code 工作流：每幅画由一个代理完整制作，再由一个没看过制作过程的代理独立审查，有严重问题时再修复，多幅画并行。在 Claude Code 里说：

> 用 workflows/living-picture-batch.js 批量处理 ~/Pictures/paintings 里的画，结果放在 ~/LivingPictures。

参数是 `images`（图片的绝对路径）、`root`（工作目录根）、可选的 `notes`（对所有画的共同要求）和 `concurrency`（同时工作的代理数，默认 4）。详见 `references/invocation.md`。

### 放进网页

把工作目录里的 `out/site/` 复制到你的网站，然后：

```html
<div id="art" style="position:relative">
  <img src="art/picture.png" alt="画面说明" style="display:block;width:100%;height:auto">
</div>
<script src="art/living-picture.js"></script>
<script>
fetch('art/scene.json').then(r => r.json()).then(scene => {
  LivingPicture.mount('#art', scene, {base: 'art/'});
});
</script>
```

原图 `<img>` 既是加载前和出错时的回退，也是“减少动态效果”时显示的画面。组件卸载时调用 `LivingPicture.unmount(element)`。细节见 `references/embedding.md`。

### 之后怎么调

用感觉来说就行，由代理翻译成参数：

- “更静一点”“再活一点点”“风慢一点”“阵风再少些”
- “景深再多一点”“镜头别晃”
- “花瓣太刻意了”“猫别眨眼了”

## 去掉分享平台的水印

从小红书等平台保存的图，右下角常带平台水印（圆角标志加一行 ID 文字）。`living-picture new` 会自动找到并去掉它：

- 标志和 ID 文字**都在**才动手，画家签名不会被当成水印；
- 平坦的底上，反推还原标志框下被提亮的画面；纹理密的底上，用周围的颜料重画；最后补回画布颗粒；
- 原图不改，修复结果是工作目录里的 `clean.png`，对比图是 `clean-review.jpg`（原图｜修复后｜改动处）；
- 不想处理时加 `--keep-watermark`；没自动找到时，用 `living-picture dewatermark 图片 --out clean.png --pill x0 y0 x1 y1 --line x0 y0 x1 y1` 给出粗框。

修复不是复原：请对照对比图核对。只处理你有权使用的图片；对他人作品去水印后再公开发布，可能侵犯权利或违反平台规定。

## Mac 专属功能

- **Apple Vision 识别起点**（`living-picture vision`）：主体和人物遮罩（可直接作为选区）、脸和眼睛（现成的转头和眨眼参数）、肢体转轴、视觉焦点、文字和签名（作为固定层）。每一条都只是起点，先看它输出的 `review.jpg` 再用。
- **iPhone 照片和相机 RAW**：HEIC、HEIF、广色域（Display P3）、16 位、带方向信息的照片，以及 DNG、CR2、NEF 等 RAW 都能直接读（RAW 用系统的解码器冲洗），统一转成 sRGB 工作图。
- **没装 ffmpeg 也能出视频**：`capture --encoder avfoundation`；找不到 ffmpeg 或它运行出错时也会自动改用。边长超过 4096 像素时自动改用 HEVC。
- Swift 小工具按需编译一次，缓存在 `~/Library/Caches/living-picture`。

详见 `references/macos-vision.md` 和 `references/macos-media.md`。

## 隐私

- 图片、图层、帧和视频都只在你的电脑上处理和保存，工作目录由你指定。
- 取帧用的 Chrome 每次用一次性的临时配置启动，不读你的浏览资料，也不访问 macOS 钥匙串。
- 唯一会把内容发到外部的是可选的 Codex 复审（`living-picture codex review`）：不加 `--consent-upload` 时只打印将要执行的命令和隐私说明，什么也不发送。
- 工作目录不要放在会同步到云端（iCloud 的“桌面与文稿”等）或会公开发布的地方，除非你愿意。

## 常见问题

**每次取帧都弹“找不到钥匙串”或 Chrome Safe Storage 对话框？**
请更新到最新版本：取帧的 Chrome 已用 `--use-mock-keychain` 启动，不再访问钥匙串。若仍有对话框留在屏幕上，点“取消”，**不要**点“还原为默认设置”（那会清空你保存的密码）。

**`living-picture` 找不到 Python 或提示缺少 numpy？**
运行 `living-picture doctor`，它会说明选了哪个 Python、为什么。也可以设置 `LIVING_PICTURE_PYTHON` 指向装好依赖的那个 `python3`。

**没有 Chrome？**
装 Chrome、Chromium 或 Edge 任意一个，或用 `CHROME_PATH` 指定浏览器路径。

**画面里某个动作看起来不对？**
直接告诉代理哪里不对、像什么。技能的原则是“宁可关掉一个动作，也不交一个错的”，坏边会回到切层或补底去修，而不是靠缩小幅度遮掩。

**最多能处理多大的图？**
工作图默认最长边 1600 px（`--max` 可调）；更大的原图会等比缩小，坐标都以工作图为准。

## 目录结构

```
SKILL.md                     技能本体：规则与流程（代理首先读它）
bin/living-picture           统一命令行入口（new、doctor、capture、picture.py 的全部工具）
scripts/
  picture.py                 prepare / look / cut / bundle / measure / palette / inspect / vision / dewatermark …
  capture.mjs                用本机 Chrome 取帧、录视频
  install.sh                 用符号链接安装给 Claude Code、Codex 和终端
  codex_bridge.py            可选的 Codex 复审（需明确同意才发送）
  lp/                        切层、补底、打包、测量、去水印、Apple 识别等实现
  macos/                     按需编译的 Swift 小工具（Vision、ImageIO、AVFoundation）
runtime/src/                 网页播放器源码，打包时拼成 living-picture.js
references/                  读画、切层、运动词汇、检查、网页接入、安装与调用、Mac 功能
workflows/                   多幅画并行处理的工作流
examples/night-house/        一张脚本画出的小图的完整示例（plan、scene 都在）
docs/                        说明用的图片
```

## English

**Living Paintings** is a skill for Claude Code and Codex that turns a still painting (watercolour, oil, illustration, or a photo of an artwork) into a gently living picture for the web: wind in the branches, light moving across a wall, a blink. The brushwork, composition and mood stay the painting's own. Everything runs locally; no uploads, no model downloads.

The agent reads the picture, decides what should lead and why, cuts depth layers along real edges, repaints what they hid with the painting's own paint, builds the motion (one wind, one light, one camera), bundles a dependency-free WebGL player (a single `index.html` plus embeddable `site/`), and checks its own work frame by frame in a headless browser.

```bash
git clone https://github.com/TT-BS/Living-paintings.git ~/living-paintings
~/living-paintings/scripts/install.sh --apply --with-cli
living-picture doctor
```

Then ask your agent: "Bring this painting to life: ~/Pictures/garden.jpg". In Codex, use `$living-picture` or `/skills`. Requirements: Python 3.9+ with numpy, scipy and Pillow; Node 22+; Chrome, Chromium or Edge; optionally ffmpeg and, on macOS, the Xcode command line tools for Apple Vision, exact HEIC and camera RAW input, and AVFoundation video (without them, opaque HEIC and RAW pictures are converted with the built-in `sips`, and video falls back to AVFoundation whenever ffmpeg is missing or fails). See `SKILL.md` and `references/invocation.md` for the full workflow, the CLI and batch processing.
