# 原生版功能分类与 rc-261026 对照

当前发布目标：

```text
native/build/twilight-native.exe
```

Tk 客户端停止维护；`twilight-app/` 仅保留为工具/测试和 sidecar 共享代码，不再作为发布入口。

## 1. 聊天与界面

- 双栏聊天、流式 reasoning/content、停止生成
- Markdown、表格、数学公式、链接/图片点击
- 12 种界面语言、API Key 掩码、单实例
- 原生自绘效果窗口：做旧 / 压片 / 补帧
- 附件 chip、图片画廊、进度窗口

## 2. 附件与上传

- 最多 32 个附件；原生多选
- 图片 / 视频 / PDF / Office / 文本 / 代码
- 图片导入只做 OCR 和缩略图，不加载 BLIP
- 视频导入只读元数据和首帧，不抽帧/OCR
- 多附件由 sidecar 并行解析（最多 4 线程）
- 超大视频切片、PPT 全页渲染
- 显式视觉请求才加载图片/视频视觉模型

## 3. 本地生成

- Qwen-Image-2.1-Turbo Universal INT4
- Qwen 图片编辑 `qwen_edit`
- SD1.5 回退与 `/sd`
- Real-ESRGAN 图片/视频
- RIFE 补帧
- VHS / 压片 / 包浆
- 音频分离、转码
- 生成图片缩略图，视频首帧缩略图
- 视频缩略图点击打开视频

## 4. 检索与下载

- `/search` 联网搜索
- `/derpi` Derpibooru 搜索/下载
- 安全评级过滤、CDN 回退、批量进度
- 本地 20k tag 表

## 5. 语音

- GPT-SoVITS TTS
- 工具标记、路径、命令、Markdown 图片不会进入 TTS
- `/tts`、`/tts/languages`

## 6. 诊断

- `/review`
- `/benchmark`
- `tools/full_selftest.py`
- `tools/debug_backend.py` / `--debug-backend`
- 崩溃日志、sidecar 日志、debug console

## 与 rc-261026 对照

| 旧功能 | 当前状态 |
| --- | --- |
| 双栏 Markdown / 表格 / 公式 | 原生支持 |
| 12 种语言 | 原生支持 |
| 32 附件 / 2GB | 原生多选 + sidecar 上限，支持 |
| PDF / Office / OCR | sidecar 支持 |
| 图片视觉 / 视频视觉 | 懒加载支持 |
| Real-ESRGAN 图片/视频 | 支持 |
| 音频分离 / 转码 / 压片 | 支持 |
| 大型视频切片 | 支持 |
| Derpibooru + 20k tag | 支持 |
| 进度窗口 | 原生自绘 |
| Tk 训练/设置页 | 不随原生版发布 |
| Tk `/sd` / `/voice` UI | `/sd` 已补 sidecar；Tk 界面不发布 |
| Tk `/ai` `/embed` 训练编排 | 仅保留工具脚本，无原生 UI |

## 已知未启用

- MiniMax H3：官方权重受授权限制，当前运行时没有 H3 权重，未启用文生视频/图生视频。
- 训练编排没有原生 UI；需要命令行工具。
