# MiniMax H3 接入

## 代码入口

- `twilight-app/minimax_h3_runtime.py`
- `/video [--ref] <prompt>`
- 本地工具 `video`
- `local_benchmark.py` 的 `h3` 项

## 运行策略

- `TWILIGHT_H3_QUANT=int4|int8|bf16`，默认 `int4`（NF4）。
- Transformer 与 Qwen3-VL conditioner 以 BitsAndBytes 量化加载。
- `ComponentsManager.enable_auto_cpu_offload()` 自动换入换出。
- Transformer 使用 block-level group offload；文本编码器 leaf-level offload。
- 8GB 目标：INT4 + CPU RAM 常驻权重，GPU 只保留当前 block/VAE。
- 输出先由 diffusers 编码，再统一转 H.264 + AAC + MP4。
- 首帧缩略图沿用 VHS 视频缩略图路径。

## 当前权重状态

下载目录：

```text
runtime/portable/ai/models/MiniMax-H3
```

官方根组件约 210GB，完整仓库（含 FL2VA/Ref2VA 副本）约 498.5GB。

当前网络下大文件下载多次停顿，只完成约 5.3GB，尚未齐备；因此：

```text
minimax_h3_runtime.available() == False
/video -> 权重未安装
```

没有伪造 INT4/INT8/bf16 的速度、质量或多卡/CPU/DirectML 结果。

## 还需要的验证

1. 完成根组件：`transformer/`、`text_encoder/`、`vae/`、`audio_vae/`、`tokenizer/`、`processor/`、scheduler。
2. 首次加载与 8GB offload 冒烟。
3. 文生视频 / 首帧图生视频 / Ref2VA 参考图。
4. H.264/AAC/MP4、缩略图和点击播放。
5. `/benchmark full` 中 H3 项。
6. INT4/INT8/bf16 速度和画质对比：`python tools/h3_quant_compare.py`。
