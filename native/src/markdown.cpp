#include "markdown.h"

#include <windows.h>
#include <gdiplus.h>
#include <fstream>
#include <sstream>
#include <vector>

using namespace Gdiplus;

namespace {

std::wstring FromUtf8(const std::string& value) {
    if (value.empty()) return {};
    const int size = MultiByteToWideChar(CP_UTF8, 0, value.data(),
                                         static_cast<int>(value.size()), nullptr, 0);
    std::wstring out(size, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                        out.data(), size);
    return out;
}

std::string EscapeRtf(const std::wstring& text) {
    std::string out;
    for (wchar_t ch : text) {
        switch (ch) {
            case L'\\': out += "\\\\"; break;
            case L'{': out += "\\{"; break;
            case L'}': out += "\\}"; break;
            case L'\n': out += "\\line "; break;
            case L'\r': break;
            case L'\t': out += "\\tab "; break;
            default:
                if (ch < 128) {
                    out += static_cast<char>(ch);
                } else {
                    out += "\\u";
                    out += std::to_string(static_cast<int>(ch));
                    out += "?";
                }
        }
    }
    return out;
}

std::string Hex(const std::vector<unsigned char>& data) {
    static const char* digits = "0123456789abcdef";
    std::string out;
    out.reserve(data.size() * 2);
    for (unsigned char byte : data) {
        out += digits[byte >> 4];
        out += digits[byte & 15];
    }
    return out;
}

std::string ImageRtf(const std::wstring& path) {
    Bitmap source(path.c_str());
    if (source.GetLastStatus() != Ok) return {};
    const int width = static_cast<int>(source.GetWidth());
    const int height = static_cast<int>(source.GetHeight());
    if (width <= 0 || height <= 0) return {};

    HBITMAP bitmap = nullptr;
    if (source.GetHBITMAP(Color(255, 255, 255), &bitmap) != Ok || !bitmap) return {};

    BITMAPINFO info{};
    info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    info.bmiHeader.biWidth = width;
    info.bmiHeader.biHeight = height;
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 24;
    info.bmiHeader.biCompression = BI_RGB;
    const int stride = ((width * 24 + 31) / 32) * 4;
    std::vector<unsigned char> pixels(static_cast<size_t>(stride) * height);
    HDC screen = GetDC(nullptr);
    const int lines = GetDIBits(screen, bitmap, 0, height, pixels.data(), &info, DIB_RGB_COLORS);
    ReleaseDC(nullptr, screen);
    DeleteObject(bitmap);
    if (lines != height) return {};

    std::vector<unsigned char> dib;
    const auto* header = reinterpret_cast<const unsigned char*>(&info.bmiHeader);
    dib.insert(dib.end(), header, header + sizeof(BITMAPINFOHEADER));
    dib.insert(dib.end(), pixels.begin(), pixels.end());

    std::string rtf = "{\pict\dibitmap0";
    rtf += "\picw" + std::to_string(width);
    rtf += "\pich" + std::to_string(height);
    rtf += "\picwgoal" + std::to_string(width * 15);
    rtf += "\pichgoal" + std::to_string(height * 15);
    rtf += " ";
    rtf += Hex(dib);
    rtf += "}";
    return rtf;
}

std::string Inline(const std::wstring& text) {
    std::string out;
    std::wstring buffer;
    auto flush = [&]() {
        if (!buffer.empty()) {
            out += EscapeRtf(buffer);
            buffer.clear();
        }
    };
    for (std::size_t i = 0; i < text.size(); ++i) {
        if (i + 1 < text.size() && text[i] == L'*' && text[i + 1] == L'*') {
            flush();
            const auto close = text.find(L"**", i + 2);
            if (close == std::wstring::npos) {
                buffer += L"**";
                ++i;
            } else {
                out += "{\\b " + EscapeRtf(text.substr(i + 2, close - i - 2)) + "}";
                i = close + 1;
            }
            continue;
        }
        if (text[i] == L'*') {
            flush();
            const auto close = text.find(L'*', i + 1);
            if (close == std::wstring::npos) {
                buffer += L'*';
            } else {
                out += "{\\i " + EscapeRtf(text.substr(i + 1, close - i - 1)) + "}";
                i = close;
            }
            continue;
        }
        if (i + 1 < text.size() && text[i] == L'~' && text[i + 1] == L'~') {
            flush();
            const auto close = text.find(L"~~", i + 2);
            if (close == std::wstring::npos) {
                buffer += L"~~";
                ++i;
            } else {
                out += "{\\strike " + EscapeRtf(text.substr(i + 2, close - i - 2)) + "}";
                i = close + 1;
            }
            continue;
        }
        if (text[i] == L'`') {
            flush();
            const auto close = text.find(L'`', i + 1);
            if (close == std::wstring::npos) {
                buffer += L'`';
            } else {
                out += "{\\f1 " + EscapeRtf(text.substr(i + 1, close - i - 1)) + "}";
                i = close;
            }
            continue;
        }
        if (text[i] == L'[') {
            const auto close = text.find(L']', i + 1);
            const auto open = close == std::wstring::npos ? close : text.find(L'(', close + 1);
            const auto end = open == std::wstring::npos ? open : text.find(L')', open + 1);
            if (close != std::wstring::npos && open != std::wstring::npos && end != std::wstring::npos) {
                flush();
                out += "{\\field{\\*\\fldinst HYPERLINK \"";
                out += EscapeRtf(text.substr(open + 1, end - open - 1));
                out += "\"}{\\fldrslt\\cf2\\ul ";
                out += EscapeRtf(text.substr(i + 1, close - i - 1));
                out += "}}";
                i = end;
                continue;
            }
        }
        buffer += text[i];
    }
    flush();
    return out;
}

bool IsTableRow(const std::wstring& line) {
    return line.size() > 2 && line.front() == L'|' && line.back() == L'|';
}

std::vector<std::wstring> Cells(const std::wstring& line) {
    std::vector<std::wstring> cells;
    std::wstring current;
    for (size_t i = 1; i < line.size(); ++i) {
        if (line[i] == L'|') {
            cells.push_back(current);
            current.clear();
        } else {
            current += line[i];
        }
    }
    return cells;
}

bool IsHorizontalRule(const std::wstring& line) {
    std::wstring trimmed;
    for (wchar_t ch : line) {
        if (ch != L' ' && ch != L'\t') trimmed += ch;
    }
    if (trimmed.size() < 3) return false;
    const wchar_t first = trimmed[0];
    if (first != L'-' && first != L'*' && first != L'_') return false;
    for (wchar_t ch : trimmed) {
        if (ch != first) return false;
    }
    return true;
}

bool IsNumberedItem(const std::wstring& line, std::wstring& content) {
    std::size_t i = 0;
    while (i < line.size() && line[i] >= L'0' && line[i] <= L'9') ++i;
    if (i == 0 || i + 1 >= line.size()) return false;
    if (line[i] != L'.' && line[i] != L')') return false;
    if (line[i + 1] != L' ') return false;
    content = line.substr(i + 2);
    return true;
}

std::string RenderTable(const std::vector<std::wstring>& rows, int totalWidth) {
    std::string out;
    for (std::size_t rowIndex = 0; rowIndex < rows.size(); ++rowIndex) {
        const auto& row = rows[rowIndex];
        const auto cells = Cells(row);
        if (cells.empty()) continue;
        bool separator = true;
        for (const auto& cell : cells) {
            for (wchar_t ch : cell) {
                if (ch != L'-' && ch != L':' && ch != L' ') separator = false;
            }
        }
        if (separator) continue;
        out += "\\trowd\\trgaph80\\trleft0";
        std::vector<int> weights;
        for (const auto& cell : cells) {
            int weight = static_cast<int>(cell.size());
            if (weight < 4) weight = 4;
            if (weight > 30) weight = 30;
            weights.push_back(weight);
        }
        int totalWeight = 0;
        for (int weight : weights) totalWeight += weight;
        int edge = 0;
        for (int weight : weights) {
            edge += totalWidth * weight / totalWeight;
            out += "\\cellx" + std::to_string(edge);
        }
        for (const auto& cell : cells) {
            out += "\\pard\\intbl\\f0\\fs20 ";
            if (rowIndex == 0) out += "\\b ";
            out += Inline(cell);
            if (rowIndex == 0) out += "\\b0 ";
            out += "\\cell ";
        }
        out += "\\row\n";
    }
    return out;
}

} // namespace

std::string MarkdownToRtf(const std::string& markdownUtf8, int widthTwips) {
    const std::wstring text = FromUtf8(markdownUtf8);
    std::wstringstream lines(text);
    std::wstring line;
    std::string body;
    std::vector<std::wstring> table;
    bool codeBlock = false;

    auto flushTable = [&]() {
        if (!table.empty()) {
            body += RenderTable(table, widthTwips);
            table.clear();
        }
    };

    while (std::getline(lines, line)) {
        if (!line.empty() && line.back() == L'\r') line.pop_back();

        if (line.rfind(L"```", 0) == 0) {
            flushTable();
            codeBlock = !codeBlock;
            body += codeBlock ? "\\pard\\f1\\fs18\\highlight1\\par\n"
                              : "\\pard\\f0\\fs20\\highlight0\\par\n";
            continue;
        }
        if (codeBlock) {
            body += "\\pard\\f1\\fs18 " + EscapeRtf(line) + "\\par\n";
            continue;
        }
        if (IsTableRow(line)) {
            table.push_back(line);
            continue;
        }
        flushTable();

        if (line.empty()) {
            body += "\\par\n";
            continue;
        }
        if (IsHorizontalRule(line)) {
            std::wstring rule; for (int i = 0; i < 48; ++i) rule += L"─";
            body += "\\pard\\cf3 " + Inline(rule) + "\\cf0\\par\n";
            continue;
        }
        std::wstring numbered;
        if (IsNumberedItem(line, numbered)) {
            body += "\\pard\\li360\\fi-360 " + Inline(numbered) + "\\par\n";
            continue;
        }
        if (line[0] == L'#') {
            size_t level = 0;
            while (level < line.size() && line[level] == L'#') ++level;
            const int size = level == 1 ? 32 : (level == 2 ? 28 : 24);
            body += "\\pard\\b\\fs" + std::to_string(size) + " " +
                    Inline(line.substr(level)) + "\\b0\\fs20\\par\n";
            continue;
        }
        if (line.rfind(L"- ", 0) == 0 || line.rfind(L"* ", 0) == 0) {
            body += "\\pard\\li360\\fi-360\\bullet\\tab " + Inline(line.substr(2)) + "\\par\n";
            continue;
        }
        if (line.rfind(L"> ", 0) == 0) {
            body += "\\pard\\li360\\cf3 " + Inline(line.substr(2)) + "\\cf0\\par\n";
            continue;
        }
        if (line.rfind(L"![", 0) == 0) {
            const auto close = line.find(L']');
            const auto open = close == std::wstring::npos ? close : line.find(L'(', close);
            const auto end = open == std::wstring::npos ? open : line.find(L')', open);
            if (close != std::wstring::npos && open != std::wstring::npos && end != std::wstring::npos) {
                const std::wstring path = line.substr(open + 1, end - open - 1);
                const std::string picture = ImageRtf(path);
                if (!picture.empty()) {
                    body += "\\pard " + picture + "\\par\n";
                    continue;
                }
            }
        }
        body += "\\pard " + Inline(line) + "\\par\n";
    }
    flushTable();

    std::string rtf = "{\\rtf1\\ansi\\ansicpg1252\\deff0"
                      "{\\fonttbl{\\f0\\fnil\\fcharset134 Microsoft YaHei;}"
                      "{\\f1\\fmodern\\fcharset0 Consolas;}}"
                      "{\\colortbl;\\red244\\green231\\blue255;\\red90\\green60\\blue140;"
                      "\\red120\\green120\\blue120;}"
                      "\\viewkind4\\uc1\\pard\\f0\\fs20 ";
    rtf += body;
    rtf += "}";
    return rtf;
}
