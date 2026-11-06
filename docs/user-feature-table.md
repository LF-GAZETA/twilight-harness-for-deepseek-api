# Twilight Harness 用户功能与指令表

发布入口：`native/build/twilight-native.exe`。Tk 客户端已停止维护，不在本表发布范围。

## 一、聊天与界面

| 类别 | 入口 | 参数/示例 | 说明 | 状态 |
| --- | --- | --- | --- | --- |
| 对话 | 输入框 | 任意文本 | 双栏聊天：左侧思考、右侧回答 | 可用 |
| 流式输出 | 自动 | — | reasoning/content 双通道 | 可用 |
| Markdown | 自动 | 标题/列表/粗体/代码 | 原生渲染 | 可用 |
| KaTeX 块公式 | 自动 | `$$...$$` / `\[...\]`，渲染为可点击图片 | 可用 |
| function-plot | `/plot` | 二维函数曲线图 | 可用 |
| 表格 | 自动 | Markdown 表格 | 原生渲染 | 可用 |
| 数学公式 | 自动 | `$...$` / `$$...$$` | 原生渲染 | 可用 |
| 链接 | 点击 | Markdown 链接 | 打开浏览器/文件 | 可用 |
| 图片 | 点击 | 生成图/附件图 | 缩略图，点击打开原图 | 可用 |
| 视频缩略图 | 点击 | VHS/压片/补帧/H3 | 打开视频，不打开 JPG | 可用 |
| 17 种语言/方言 | 设置页 | 中文 5 种、英语 2 种、法语 3 种、德语 3 种、意/葡/西；拉丁 Uni Neue，中文 Source Han | 界面/TTS/字体联动 | 可用 |
| API Key | 设置页 | DeepSeek/OpenAI 兼容 | 掩码显示 | 可用 |
| 单实例 | 自动 | — | 第二个实例聚焦已有窗口 | 可用 |
| 崩溃日志 | 自动 | `%LOCALAPPDATA%` | 启动错误保留日志 | 可用 |
| Debug 后端日志 | 启动参数 | `twilight-native.exe --debug-backend` | 输出 sidecar 调用 | 可用 |
| Debug CLI | 命令行 | `python tools/debug_backend.py "/review ..."` | 输出 SSE/工具/日志 | 可用 |

## 二、附件

| 类别 | 入口 | 限制/支持 | 说明 | 状态 |
| --- | --- | --- | --- | --- |
| 多附件 | 附件按钮 | 最多 32 个 | 原生多选 | 可用 |
| 总大小 | 自动 | 2 GB | sidecar 校验 | 可用 |
| 单文件 | 自动 | 50 MB；视频按切片上限 | 大视频切片 | 可用 |
| 图片 | PNG/JPG/WebP/BMP/GIF/TIFF | OCR + 本地视觉 | 导入只做 OCR/缩略图，视觉懒加载 | 可用 |
| 视频 | MP4/MOV/MKV/AVI/WebM/M4V/WMV/FLV/TS | 元数据 + 首帧 | 导入不拆帧/OCR | 可用 |
| PDF | PDF | 文本/OCR | 懒加载视觉 | 可用 |
| Office | DOCX/PPTX/XLSX/XLSM | 文本/表格/PPT 全页 | PPT 视觉按需 | 可用 |
| 旧 Office | DOC/PPT/XLS | 临时转换读取 | 兼容 | 可用 |
| 文本/代码 | TXT/MD/CSV/JSON/XML/YAML/代码 | 文本提取 | 支持 GBK/GB18030 | 可用 |
| 附件撤销 | chip `×` | — | 清空当前附件 | 可用 |
| 并行解析 | 自动 | 4 线程 | 附件导入加速 | 可用 |

## 三、斜杠命令

| 指令 | 示例 | 功能 | 依赖/说明 | 状态 |
| --- | --- | --- | --- | --- |
| `/video` | `/video a pony running` | MiniMax H3 文生视频 | 需要 H3 根组件 | 代码已接入，权重未齐 |
| `/video --ref` | `/video --ref a pony` | 参考图 + 文生视频 | 需要 Ref2VA 组件 | 代码已接入，权重未齐 |
| `/edit` | `/edit 把背景改成夜晚` | 逐像素 image-to-image 编辑附加图片 | 需要附加图片 | 可用 |
| `/plot` | `/plot x^2; sin(x)` | function-plot 二维函数图 | Edge + 本地 JS | 可用 |
| `/math` | `/math \int_0^1 x^2 dx` | KaTeX 公式渲染 | Edge + KaTeX | 可用 |
| `/sd` | `/sd a pony 768x512` | SD1.5 文生图 | SD1.5 ONNX | 可用 |
| `/image` | `/image a pony` | 优先 Qwen，失败回退 SD1.5 | Qwen 权重 | 可用 |
| `/qwen` | `/qwen a pony` | 强制 Qwen 文生图 | Qwen 权重 | 可用 |
| `/derpi` | `/derpi twilight sparkle` | 搜索并下载 Derpibooru | 最多 10 张输出 | 可用 |
| `/derpibooru` | `/derpibooru ...` | 同 `/derpi` | 别名 | 可用 |
| `/search` | `/search 最新显卡` | 联网搜索 8 条 | 网络 | 可用 |
| `/review` | `/review` + 代码 | 本地代码审查 | 无模型 | 可用 |
| `/benchmark` | `/benchmark` | 本地硬件/推理基准 | 完整模式含 AI 项 | 可用 |
| `/bench` | `/bench` | 同 `/benchmark` | 别名 | 可用 |
| `/vhs` | `/vhs` | 打开 VHS 效果窗口 | 原生窗口 | 可用 |
| `/vhs化` | `/vhs化 0.44` | VHS 化 | 需要视频 | 可用 |
| `/compress` | `/compress` / `/compress 0.7` | 打开压片窗口/压片 | 需要视频 | 可用 |
| `/压片` | `/压片 0.5` | 压片 | 需要视频 | 可用 |
| `/aging` | `/aging` | 打开做旧窗口 | 图片/视频 | 可用 |
| `/包浆` | `/包浆 0.5` | 做旧/包浆 | 需要视频 | 可用 |
| `/frame` | `/frame` | 打开补帧窗口 | 视频 | 可用 |
| `/frame-interpolation` | `/frame-interpolation 2` | RIFE 补帧 2–30 | 视频 | 可用 |
| `/frame-upscale` | `/frame-upscale 2` | 兼容补帧命令 | 视频 | 可用 |

## 四、本地工具（模型/自然语言触发）

| 工具 | 参数 | 功能 | 自然语言示例 | 状态 |
| --- | --- | --- | --- | --- |
| `derpi` | query, limit, after, before | 搜索/下载 Derpibooru，limit 1–5000，输出最多 10 张缩略图 | “找十张 Twilight 图片” | 可用 |
| `image` | prompt, negative, size, count, engine | Qwen 优先，SD1.5 回退 | “画一张紫悦” | 可用 |
| `qwen_image` | prompt, negative, size | 强制 Qwen 文生图 | “用 Qwen 画...” | 可用 |
| `qwen_edit` | instruction, size | 真正逐像素 image-to-image；未说明改哪里时先询问 | “把背景改成夜晚” | 可用 |
| `plot` | expressions, xmin/xmax, ymin/ymax, title | function-plot 二维函数图 | “画出 x^2 和 sin(x)” | 可用 |
| `video` | prompt, mode, frames, width, height | MiniMax H3 视频 | “生成一段视频” | 代码已接入，权重未齐 |
| `vhs` | value 0–1 | VHS 化附加视频 | “做 VHS 化” | 可用 |
| `compress` | value 0–1 | Beold 压片 | “压个片” | 可用 |
| `age` | 无 | 附加图片做旧 | “把图片做旧” | 可用 |
| `upscale` | 无 | Real-ESRGAN 图片 4× | “超分这张图” | 可用 |
| `video_upscale` | 无 | Real-ESRGAN 视频逐帧 4× | “视频超分” | 可用 |
| `audio` | 无 | 分离视频音轨 | “提取音频” | 可用 |
| `transcode` | format, bitrate, resolution, audio_bitrate, fps | 视频转码 | “转成 WebM” | 可用 |
| `code_review` | 无 | 审查附加代码 | “帮我审查这段代码” | 可用 |

## 五、生成与媒体能力

| 能力 | 入口 | 说明 | 状态 |
| --- | --- | --- | --- |
| Qwen-Image-2.1-Turbo | `/image`、`/qwen`、`image`、`qwen_image` | Universal INT4 Adaptive，8GB/10GB 自适应，512/768/1024 | 可用 |
| Qwen 图片编辑 | `qwen_edit` / `/edit` | 真正 image-to-image：完整视觉编码器 CPU + Transformer/VAE GPU | 可用（首次约 184s） |
| KaTeX 公式 | `$$...$$`、`\[...\]`、`/math` | KaTeX 渲染为 PNG，点击放大 | 可用 |
| function-plot | `/plot`、`plot` 工具 | 二维函数图，最多 4 条曲线 | 可用 |
| SD1.5 | `/sd`、`image(engine=sd15)` | INT8 ONNX，Euler Ancestral，25 步，CFG8 | 可用 |
| 参考图 | 附图 + Qwen 生成 | 本地视觉描述参与提示词 | 可用 |
| 生成图缩略图 | 自动 | 原生 280×180，点击原图 | 可用 |
| VHS / 压片 / 包浆 | `/vhs化`、`/压片`、`/包浆`、工具 | OpenCL/Beold 效果 | 可用 |
| RIFE 补帧 | `/frame-*` | 2–30 倍 | 可用 |
| Real-ESRGAN 图片 | `upscale` | x4 动漫模型，INT8 优先 | 可用 |
| Real-ESRGAN 视频 | `video_upscale` | 逐帧 + 原音轨 | 可用 |
| 音频分离 | `audio` | UVR/ffmpeg | 可用 |
| 转码 | `transcode` | MP4/WebM/MKV/MOV/GIF，码率/分辨率/FPS | 可用 |
| H.264 + AAC + MP4 | H3/VHS 输出 | 统一封装 | 可用（H3 权重未齐） |
| 视频首帧缩略图 | 自动 | `.thumb.jpg`，点击打开视频 | 可用 |
| H3 视频 | `/video`、`video` | 文生/图生/参考图视频 | 权重未齐，未验证 |

## 六、语音、进度与诊断

| 功能 | 入口 | 说明 | 状态 |
| --- | --- | --- | --- |
| GPT-SoVITS TTS | 自动朗读 | 最终回答自动合成 | 可用 |
| TTS 清洗 | 自动 | 不读命令、路径、文件名、工具标记 | 可用 |
| Qwen 预热 | sidecar 启动 | 后台预热，首图约 6.3s | 可用 |
| 进度窗口 | 自动 | 工具/图片/视频/下载进度 | 可用 |
| 完成后清理 | 自动 | 工具完成后隐藏并清空 | 可用 |
| 下载批次进度 | `/derpi` | 单行批次，不是每图一行 | 可用 |
| `/review` | 指令 | 静态代码审查 | 可用 |
| `/benchmark` | 指令 | CPU/PIL/ffmpeg/AI 基准 | 可用 |
| Debug SSE 日志 | `--debug-backend` | sidecar 调用日志 | 可用 |
| `sidecar-debug.log` | 自动 | 后端调用记录 | 可用 |
| `full_selftest.py` | 工具 | 19 项回归 | 19/19 通过 |

## 七、当前不可用或保留项

| 功能 | 状态 | 原因 |
| --- | --- | --- |
| Tk 客户端 | 停止维护 | 发布目标改为原生 |
| Qt 客户端 | 不纳入当前发布 | 独立旧构建 |
| `/voice` Tk 状态页 | 不提供 | 仅保留 TTS 后端 |
| `/ai`、`/embed` 原生 UI | 不提供 | 训练编排仅保留工具脚本 |
| MiniMax H3 文生视频 | 未验证 | 权重未下载完整 |
| H3 8GB/多卡/DirectML/CPU | 未验证 | 同上 |
| H3 INT4/INT8/BF16 对比 | 未验证 | 同上 |
| 参考图文生图 | 可用 | Qwen 参考图使用视觉描述引导；真正改图请用 qwen_edit / /edit |
| 共用 Qwen text_encoder 权重 | 不可行 | H3 64 层/hidden 5120，Qwen-Image 36 层/hidden 4096 |

## 八、最短使用示例

```text
画一张紫悦在图书馆的图
超分这张图片
把视频做 VHS 化，强度 0.44
把这段视频补帧到 60fps
/derpi twilight sparkle
/search 今天的显卡新闻
/review
```

视频/图片生成完成后显示缩略图；点击缩略图打开原图或视频。
