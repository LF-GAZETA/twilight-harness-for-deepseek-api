# Twilight Harness 当前功能目录

说明：README 中部分版本/功能仍是历史快照；本目录按当前代码、运行时和自测整理。

## 1. 客户端与界面

- 发布入口：原生 Win32 C++ 客户端 `native/build/twilight-native.exe`
  - 双栏聊天：推理过程 + 最终回答
  - 流式输出、停止生成、Markdown、表格、链接点击
  - 附件 chip（最多 32 个，多选）、图片画廊
  - 独立效果窗口：做旧 / 压片 / 补帧
  - 12 种界面语言
  - 新 Twilight 图标
- Tk 客户端：已停止维护，不作为发布入口
- Qt 客户端：不纳入当前原生发布
- sidecar 后端 `sidecar/server.py`
  - HTTP/SSE、token 鉴权、硬件探测、附件、聊天、TTS、工具

## 2. 聊天与模型

- OpenAI Chat Completions 兼容 API
- DeepSeek / Gemini OpenAI 兼容端点
- 流式 reasoning/content 双通道
- 历史裁剪、停止生成、错误详情
- API Key 掩码、环境变量 Key 不落盘
- 多语言 persona / system prompt
- 链接点击、图片点击、Markdown 表格、数学公式
- 本地工具调用协议：
  `[[LOCAL_TOOL: {...}]]`

## 3. 附件与视觉

- 图片附件：PNG/JPG/WebP/BMP/GIF/TIFF
- 视频附件：MP4/MOV/MKV/AVI/WebM/M4V/WMV/FLV/TS
- PDF 文本/OCR
- Office/旧 Office/Visio 文本提取
- PPT 全页渲染 + 每页视觉描述
- 视频懒加载：
  - 导入时只读元数据，不拆帧
  - 显式分析时才抽帧/OCR/联系表
- 图片本地视觉：BLIP caption/VQA
- 视频视觉：抽帧联系表、OCR
- 多图视觉输入
- 附件大文件切片

## 4. 图像与视频处理

- Real-ESRGAN 图片 4× 超分
- Real-ESRGAN 视频逐帧超分
- RIFE 补帧：
  - `/frame`
  - `/frame-interpolation`
  - `/frame-upscale` 兼容
- VHS 化 `/vhs化`
- 极限压片 `/压片` / 自然语言“压个片”
- 包浆 `/包浆`
- 音频分离 `/audio`
- 转码 `/transcode`：MP4/WebM/MKV/MOV/GIF
- 帧率、分辨率、码率参数
- 独立进度窗口、OpenCL 多 GPU 条带渲染
- 特效工具：Beold/effects_core 内部实现，OpenCL VHS/压片/包浆

## 5. 文生图

### Stable Diffusion 1.5

- `/sd <prompt> [size]`（Tk）
- INT8 ONNX
- 固定采样器 Euler Ancestral、25 步、CFG 8
- CPU Offload
- LoRA 槽位
- 自然语言转正负提示词

### Qwen-Image-2.1-Turbo

- `/image`：优先 Qwen，失败回退 SD1.5
- `/qwen <prompt>`：强制 Qwen
- 模型工具：
  - `image`（auto Qwen → SD）
  - `qwen_image`（强制 Qwen）
- Universal INT4 Adaptive 包：10.634 GiB
  - 8GB：两阶段
  - 10GB：裁剪文本加载器双常驻
  - 16GB：可全常驻
  - 多卡：数据并行
- 自定义文本加载器：
  - 去 lm_head
  - 去 vision tower
  - INT8/INT4 token embedding
- 512/768/1024 尺寸，10GB 自动限制 512
- 视觉/文本/Transformer 分阶段加载与回退

## 6. 语音

- GPT-SoVITS v2 音色克隆
- TTS 自动朗读最终回答
- TTS 语言：
  - zh / en / ja / ko / yue / auto / auto_yue
- 动作、工具协议、斜杠命令清理
- 缩写字母拆分朗读（VHS、OCR 等）
- UVR MDX-Net 人声/伴奏分离
- Whisper.cpp Q5_1 语音识别槽位
- `/voice` 状态检查（Tk）
- `/tts` sidecar API
- `/tts/languages`

## 7. 搜索与下载

- 联网搜索 `/search`
- Derpibooru：
  - `/derpi` / `/derpibooru`
  - 自然语言 tag 转换
  - 时间段 after/before
  - 数量 1–5000
  - 本地 20,000 tag 表与别名
  - 安全评级过滤
  - CDN 回退、重试、Cloudflare IP
  - 每张图独立/批次进度
  - 下载目录回退
- 图片画廊、结果链接

## 8. 开发与诊断

- `/review` 代码审查
- 本地工具 `code_review`
- `/benchmark` 硬件/本地推理片段
- `full_selftest.py`：19 项当前功能
- `multigpu_regression.py`
- `tts_multigpu_regression.py`
- Qwen benchmark/量化脚本
- sidecar 日志、debug console、崩溃日志
- 单实例、只读目录回退、临时目录清理

## 9. 硬件与后端

- NVIDIA CUDA
- AMD ROCm（Linux）
- ZLUDA 预留
- DirectML 预留（AMD/Intel）
- Vulkan/ncnn（Real-ESRGAN）
- CPU 兼容路径
- Qwen provider 选择：
  `TWILIGHT_QWEN_BACKEND=cuda|dml|cpu`
- 多 GPU：
  - Qwen 数据并行
  - ESRGAN/RIFE 多 GPU 条带/分帧
  - 调度器与自适应并发
- CPU Offload：
  - BF16 顺序 Offload
  - Qwen 两阶段/双常驻
  - 8GB/10GB 自适应

## 10. 训练与资源

- `ai-individuals/`
- SD1.5 LoRA 训练流程
- GPT-SoVITS 训练流程
- UVR/Whisper 资源槽
- 量化、内嵌、单 EXE 自检工具
- 模型目录：
  ```text
  runtime/portable/ai/models
  ```
- 训练数据：
  `training_workspace`（用户数据，未纳入运行包）

## 11. 当前模型占用

```text
Qwen universal      10.634 GiB
SD1.5 INT8           3.62 GiB
Vision BLIP          2.36 GiB
GPT-SoVITS           0.54 GiB
ESRGAN + RIFE        0.04 GiB
```

## 12. 已验证

`full_selftest.py` 19 项全部通过：

```text
sidecar health / hardware
image attachment + local vision
PDF OCR
PPT all slides
/review
video lazy metadata
video visual frames
/derpi search
/vhs化 /压片 /包浆 /frame
/search
SD1.5
Real-ESRGAN image/video
RIFE
```

额外验证：

- `/image` Qwen 优先并回退 SD；
- `/qwen`；
- TTS 返回 WAV；
- Qwen 8GB/10GB 双 INT4；
- Qwen 多卡数据并行；
- 原生 exe 图标。

## 13. 未完全验证/限制

- CPU Qwen 生成极慢，未完成完整基准；
- AMD/Intel DirectML 未在当前机器验证；
- 三值/1.58bit / 非对称文本量化质量不可用；
- 4GB 峰值需标准格式逐层 staging，未实现；
- Qwen 10GB 双常驻余量不足 1GiB；
- 训练与内嵌需外部工具链和用户权重；
- README 中部分版本/体积信息未随当前代码更新。
