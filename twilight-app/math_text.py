"""Small, dependency-free LaTeX-to-Unicode converter for chat formulas.

The Tk Text widget cannot typeset LaTeX.  This converter covers the common
math notation produced by chat models: Greek letters, fractions, roots,
superscripts/subscripts, sums, integrals, comparison operators and function
names.  It deliberately keeps unknown commands readable instead of throwing
them away.
"""

from __future__ import annotations

import re

GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "omicron": "ο",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}

SYMBOLS = {
    "infty": "∞", "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬",
    "iiint": "∭", "oint": "∮", "partial": "∂", "nabla": "∇",
    "pm": "±", "mp": "∓", "times": "×", "div": "÷", "cdot": "·",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "sim": "∼", "propto": "∝",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "forall": "∀", "exists": "∃", "nexists": "∄", "angle": "∠",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "uparrow": "↑", "downarrow": "↓", "degree": "°", "circ": "∘",
    "perp": "⊥", "parallel": "∥", "therefore": "∴", "because": "∵",
}

FUNCTIONS = {
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos",
    "arctan", "sinh", "cosh", "tanh", "log", "ln", "lg", "exp",
    "lim", "max", "min", "sup", "inf", "det", "gcd", "mod", "bmod",
}

SUPERSCRIPT = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
SUBSCRIPT = str.maketrans("0123456789+-=()aeox", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓ")


def _simple_command(name: str) -> str:
    if name in GREEK:
        return GREEK[name]
    if name in SYMBOLS:
        return SYMBOLS[name]
    if name in FUNCTIONS:
        return name
    if name in {"text", "mathrm", "mathbf", "mathit", "operatorname"}:
        return ""
    return name


def latex_to_unicode(text: str) -> str:
    text = text or ""
    text = text.replace("\\left", "").replace("\\right", "")
    text = re.sub(r"\\(?:,|;|!|quad|qquad| )", " ", text)
    # Resolve text wrappers before fraction/root parsing so nested constructs
    # such as \frac{\text{a}}{\text{b}} keep their arguments together.
    text = re.sub(
        r"\\(?:text|mathrm|mathbf|mathit|operatorname)\{([^{}]*)\}",
        r"\1",
        text,
    )
    # Fractions and roots (repeat for modest nesting).
    for _ in range(4):
        updated = re.sub(
            r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
            lambda m: f"({m.group(1)})/({m.group(2)})",
            text,
        )
        updated = re.sub(
            r"\\sqrt\s*\{([^{}]*)\}",
            lambda m: "√(" + m.group(1) + ")",
            updated,
        )
        if updated == text:
            break
        text = updated
    # Superscript / subscript groups.
    text = re.sub(
        r"\^\{([^{}]*)\}",
        lambda m: "".join(ch.translate(SUPERSCRIPT) for ch in m.group(1))
        if all(ch.isdigit() or ch in "+-=()ni" for ch in m.group(1))
        else "^(" + m.group(1) + ")",
        text,
    )
    text = re.sub(
        r"_\{([^{}]*)\}",
        lambda m: "".join(ch.translate(SUBSCRIPT) for ch in m.group(1))
        if all(ch.isdigit() or ch in "+-=()aeox" for ch in m.group(1))
        else "_(" + m.group(1) + ")",
        text,
    )
    text = re.sub(
        r"\^([0-9+\-=ni])",
        lambda m: m.group(1).translate(SUPERSCRIPT),
        text,
    )
    text = re.sub(
        r"_([0-9+\-=aeox])",
        lambda m: m.group(1).translate(SUBSCRIPT),
        text,
    )
    # Remove formatting wrappers and resolve simple commands.
    text = re.sub(
        r"\\(?:text|mathrm|mathbf|mathit|operatorname)\{([^{}]*)\}",
        r"\1",
        text,
    )
    text = re.sub(
        r"\\([A-Za-z]+)",
        lambda m: _simple_command(m.group(1)),
        text,
    )
    text = text.replace("\\{", "{").replace("\\}", "}")
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def transform_markdown_math(content: str) -> str:
    """Convert TeX delimiters to Unicode outside fenced code blocks."""
    if not content:
        return content
    parts = re.split(r"(```.*?```)", content, flags=re.DOTALL)
    converted: list[str] = []
    for part in parts:
        if part.startswith("```"):
            converted.append(part)
            continue
        part = re.sub(
            r"\$\$(.+?)\$\$",
            lambda m: "\n" + latex_to_unicode(m.group(1)) + "\n",
            part,
            flags=re.DOTALL,
        )
        part = re.sub(
            r"\\\[(.+?)\\\]",
            lambda m: "\n" + latex_to_unicode(m.group(1)) + "\n",
            part,
            flags=re.DOTALL,
        )
        part = re.sub(
            r"\\\((.+?)\\\)",
            lambda m: latex_to_unicode(m.group(1)),
            part,
            flags=re.DOTALL,
        )
        part = re.sub(
            r"(?<!\\)\$([^$\n]+?)(?<!\\)\$",
            lambda m: latex_to_unicode(m.group(1)),
            part,
        )
        converted.append(part)
    return "".join(converted)
