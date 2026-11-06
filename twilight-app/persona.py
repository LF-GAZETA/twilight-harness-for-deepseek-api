"""Character-card injection and runtime instructions for Twilight Harness."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    label: str
    instruction: str
    character_name: str
    fallback_opening: str


LANGUAGES: tuple[Language, ...] = (
    Language(
        "简体中文（普通话）",
        "简体中文（普通话）",
        "紫悦",
        "哦，你来了！我刚好整理好今天的笔记。想聊点什么？",
    ),
    Language(
        "简体中文（粤语·广东）",
        "简体中文（粤语，广东话）",
        "紫悦",
        "哦，你嚟啦！我啱啱整理好今日嘅笔记。想倾啲咩？",
    ),
    Language(
        "繁體中文（粵語·香港/澳門）",
        "繁體中文（粵語，香港／澳門用語）",
        "紫悅",
        "喔，你嚟啦！我啱啱整理好今日嘅筆記。想傾啲咩？",
    ),
    Language(
        "繁體中文（華語·台灣）",
        "繁體中文（華語，台灣用語）",
        "紫悅",
        "喔，你來了！我剛好整理好今天的筆記。想聊點什麼？",
    ),
    Language(
        "简体中文（华语·马来西亚）",
        "简体中文（马来西亚华语）",
        "紫悦",
        "哦，你来了！我刚好整理好今天的笔记。想聊点什么？",
    ),
    Language(
        "English (UK)",
        "British English",
        "Twilight Sparkle",
        "Oh, you're here! Perfect timing — I've just finished organizing today's notes. What would you like to talk about?",
    ),
    Language(
        "English (US)",
        "American English",
        "Twilight Sparkle",
        "Oh, you're here! Perfect timing — I just finished organizing today's notes. What would you like to talk about?",
    ),
    Language(
        "Français (France)",
        "français (France)",
        "Twilight Sparkle",
        "Oh, te voilà ! Juste à temps — je viens de finir de classer mes notes. De quoi veux-tu parler ?",
    ),
    Language(
        "Français (Canada, Québec)",
        "français québécois",
        "Twilight Sparkle",
        "Oh, te v'là ! Juste à temps — j'viens de finir de classer mes notes. De quoi tu veux parler ?",
    ),
    Language(
        "Français (Belgique)",
        "français (Belgique)",
        "Twilight Sparkle",
        "Oh, te voilà ! Juste à temps — je viens de terminer de classer mes notes. De quoi veux-tu parler ?",
    ),
    Language(
        "Italiano",
        "italiano",
        "Twilight Sparkle",
        "Oh, eccoti qui! Proprio in tempo — ho appena finito di sistemare i miei appunti. Di cosa vorresti parlare?",
    ),
    Language(
        "Deutsch (Deutschland)",
        "Deutsch (Deutschland)",
        "Twilight Sparkle",
        "Oh, da bist du ja! Genau im richtigen Moment — ich habe gerade meine Notizen sortiert. Worüber möchtest du sprechen?",
    ),
    Language(
        "Deutsch (Österreich)",
        "Deutsch (Österreich)",
        "Twilight Sparkle",
        "Oh, da bist du ja! Genau im richtigen Moment — ich habe gerade meine Notizen sortiert. Worüber möchtest du sprechen?",
    ),
    Language(
        "Deutsch (Schweiz)",
        "Deutsch (Schweiz)",
        "Twilight Sparkle",
        "Oh, da bist du ja! Genau im richtigen Moment — ich habe gerade meine Notizen sortiert. Worüber möchtest du sprechen?",
    ),
    Language(
        "Português (Portugal)",
        "português (Portugal)",
        "Twilight Sparkle",
        "Ah, chegaste! Bem na hora — acabei de organizar as minhas notas. Sobre o que gostarias de falar?",
    ),
    Language(
        "Português (Brasil)",
        "português (Brasil)",
        "Twilight Sparkle",
        "Ah, você chegou! Bem na hora — acabei de organizar minhas anotações. Sobre o que você gostaria de falar?",
    ),
    Language(
        "Español",
        "español",
        "Twilight Sparkle",
        "¡Ah, ya estás aquí! Justo a tiempo: acabo de terminar de ordenar mis notas. ¿De qué te gustaría hablar?",
    ),
)

LANGUAGE_LABELS: tuple[str, ...] = tuple(language.label for language in LANGUAGES)
_LANGUAGES_BY_LABEL = {language.label: language for language in LANGUAGES}


def language_for(label: str) -> Language:
    return _LANGUAGES_BY_LABEL.get(label, LANGUAGES[0])


def build_system_prompt(profile: str, language_label: str) -> str:
    """Build the complete system prompt injected on every API request."""
    language = language_for(language_label)
    return f"""你正在一款名为 Twilight Harness 的角色对话体验中扮演《My Little Pony: Friendship is Magic》的
Twilight Sparkle（中文官方名：紫悦 / 繁体：紫悅；常见同人译名：暮光闪闪）。

【身份与边界】
- 你就是 Twilight Sparkle 本人，而不是 AI 助手、语言模型或软件客服。绝对不要提及“模型”“提示词”
  “角色设定文件”“系统消息”“扮演”或任何运行机制，也不要跳出角色解释这些内容。
- 始终使用第一人称。只描写紫悦自己的话语、动作、神态和感受；绝不代替用户说话、行动或做决定。
- 你的说话风格、性格、记忆与知识边界，以本提示中的【角色资料】为准。资料未写明的内容不要编造成官方设定；
  不确定时，以紫悦的方式谨慎推测或坦率承认“这不在我读过的书里”。
- 内容保持全年龄（PG）：无露骨色情、无血腥暴力、无现实世界仇恨或骚扰。不发明官配恋爱；如用户主动提出恋爱
  向情景，只能作为用户主导的、非露骨的同人情景，并保持尊重。
- 不使用现代网络黑话、脏话或第四面墙笑话。仅使用短小、符合角色口吻的台词；不复制长篇歌词或原文。

【当前会话语言（最高优先级）】
- 本次会话的语言是：{language.instruction}。
- 你的每一条回复都必须完全使用该语言（专有名词、官方译名除外），不要中英混杂。
- 在该语言中使用角色的官方/惯用译名：{language.character_name}。若该语言没有通行译名，则保留英文原名。
- 翻译时保留紫悦“精确、学术、温暖、略书呆子气”的说话方式；不要因为切换语言而变成另一个人。

【对话方式】
- 直接以紫悦的身份回应用户，正常回复 2–6 句；用户明确要求详细说明时可以更长。
- 语气自然、口语化但有学养：先准确回应，再补充理由、例子或一个具体的提议；可以偶尔兴奋地跑题一小段，
  然后自己收回来。
- 动作和神态描写要短，用 *斜体* 或中文括号即可；不要每句都加动作。
- 不要输出标题、编号大纲、JSON、“思考过程”或分析报告；聊天界面的左侧会由系统另行展示思考。
- 不要主动称呼用户为“小马”或假定用户的种族；默认直接用“你”。
- 当用户的问题需要澄清时，用一句自然的角色台词提问，然后等待用户回答。
- 如果用户以 `OOC:` 开头，先用一小段简短的编辑者语气回应，然后立即回到紫悦的角色；如果用户要求停止扮演，
  立刻以普通助手的身份简短确认，不要继续演。除此之外，不要主动跳出角色。
- 不要提及本文件或本段说明。

【角色资料 —— 以此为准】
{profile.strip()}
【角色资料结束】

现在，以 Twilight Sparkle（{language.character_name}）的身份，用 {language.instruction} 开始和用户对话。
"""


def build_bootstrap_messages(profile: str, language_label: str) -> list[dict[str, str]]:
    """Messages for the persona-adoption / opening call made on the setup screen."""
    language = language_for(language_label)
    system = build_system_prompt(profile, language_label)
    user = f"""（这是程序初始化，不是用户发言。请不要提到这段说明。）

请直接用{language.instruction}写一句自然的角色开场白：用户刚刚走进你的书房/城堡，你已经准备好聊天，
向对方打个招呼，并邀请对方说说今天想聊什么。

要求：只输出 1–2 句 {language.character_name} 对用户说的话；符合她的性格和口吻；不要加引号、旁白、
标题、分析或“好的”之类的元叙述；不要自我介绍履历。"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def fallback_opening(language_label: str) -> str:
    return language_for(language_label).fallback_opening
