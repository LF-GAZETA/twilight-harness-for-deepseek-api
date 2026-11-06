# 语言与方言支持

客户端当前支持 17 个界面语言/方言。切换语言会立即更新初始化界面、提示文字、效果/进度窗口标题、按钮、字体和对话视图字体。

## 语言表

| # | 语言/方言 | 文字 | 界面字体 | TTS checkpoint | TTS 参考音色 | TTS 文本语言 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 简体中文（普通话） | 中文 | Source Han Sans / 思源宋体 | 中文 ckpt/pth | 中文参考音频 | zh |
| 2 | 简体中文（粤语·广东） | 中文 | Source Han Sans / 思源宋体 | 中文 ckpt/pth | 中文参考音频 | yue |
| 3 | 繁體中文（粵語·香港/澳門） | 中文 | Source Han Sans / 思源宋体 | 中文 ckpt/pth | 中文参考音频 | yue |
| 4 | 繁體中文（華語·台灣） | 中文 | Source Han Sans / 思源宋体 | 中文 ckpt/pth | 中文参考音频 | zh |
| 5 | 简体中文（华语·马来西亚） | 中文 | Source Han Sans / 思源宋体 | 中文 ckpt/pth | 中文参考音频 | zh |
| 6 | English (UK) | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 7 | English (US) | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 8 | Français (France) | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 9 | Français (Canada, Québec) | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 10 | Français (Belgique) | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 11 | Italiano | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 12 | Deutsch (Deutschland) | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 13 | Deutsch (Österreich) | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 14 | Deutsch (Schweiz) | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 15 | Português (Portugal) | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 16 | Português (Brasil) | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |
| 17 | Español | 拉丁 | Uni Neue | 英语 ckpt/pth | 英语参考音频 | en |

## TTS 说明

- 中文全部使用中文 ckpt/pth 和中文参考音频。
- 粤语使用中文 ckpt/pth、`text_lang=yue`。
- 所有非中文语言使用英语 ckpt/pth 和英语参考音频。
- 法语、德语、意大利语、西班牙语、葡萄牙语当前通过英语模型模拟发音，不是原生法/德/意/西/葡模型。
- 英语参考音频旁的 `.txt` 提示文本已加载，用于改善音色和韵律。
- 英文动作/旁白 Markdown 会从 TTS 输入中移除。

## 字体说明

- 拉丁语言：
  - 正文、按钮、输入框、语言标签：`Uni Neue`
  - 对话视图正文/元信息：`Uni Neue`
  - 标题：`Uni Neue`
- 中文：
  - 非衬线：`Source Han Sans`
  - 衬线：`思源宋体`（私有加载 `SourceHanSerifSC-Regular.otf`）
  - 对话视图标题：思源宋体；正文：Source Han Sans

## 界面本地化范围

- API Key 提示
- “语言：”标签
- 效果窗口：标题、做旧、压片、补帧、应用
- 进度窗口标题、下载提示
- 侧栏启动提示
- 附件对话框标题、附件前缀
- 错误前缀、Sidecar 超时
- 对话视图字体

## 验证

```text
简体中文 → English (UK) → Français (France)
启动界面文字、语言标签、字体均随选择变化
```

TTS 实测：

```text
Français (France) → 英语 ckpt 输出成功
简体中文（粤语·广东） → 中文 ckpt，text_lang=yue 输出成功
```
