#include "chat_view.h"

#include <windows.h>
#include <windowsx.h>
#include <gdiplus.h>
#include <shellapi.h>
#include <algorithm>
#include <fstream>
#include <memory>
#include <sstream>
#include <vector>
#include <utility>

using namespace Gdiplus;

namespace {

constexpr wchar_t kClass[] = L"TwilightChatView";
constexpr UINT kSetMarkdown = WM_APP + 30;
constexpr UINT kSetFonts = WM_APP + 31;

enum class Kind { Paragraph, Heading, List, Code, Rule, Image, Gallery, Table, Avatar, Meta };

struct Block {
    Kind kind = Kind::Paragraph;
    std::wstring text;
    std::wstring path;
    std::vector<std::vector<std::wstring>> rows;
    std::vector<std::wstring> paths;
    int level = 0;
};

struct LinkRect { RECT rect{}; std::wstring url; };

struct ViewState {
    Gdiplus::Image* hero = nullptr; // unused placeholder for ABI stability
    std::wstring markdown;
    std::vector<Block> blocks;
    std::vector<RECT> imageRects;
    std::vector<std::wstring> imagePaths;
    std::vector<LinkRect> linkRects;
    int scroll = 0;
    int totalHeight = 0;
    bool scrollBottom = true;
    COLORREF background = RGB(247, 239, 255);
    Font* body = nullptr;
    Font* heading = nullptr;
    Font* mono = nullptr;
    Font* bold = nullptr;
    Font* meta = nullptr;
};

std::wstring FromUtf8(const std::string& value) {
    if (value.empty()) return {};
    const int size = MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0);
    std::wstring out(size, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), size);
    return out;
}

std::wstring StripInline(std::wstring value) {
    std::wstring out;
    for (wchar_t ch : value) {
        if (ch == L'`' || ch == L'*') continue;
        out += ch;
    }
    return out;
}

std::wstring Trim(const std::wstring& value) {
    const auto first = value.find_first_not_of(L" \t\r");
    if (first == std::wstring::npos) return {};
    const auto last = value.find_last_not_of(L" \t\r");
    return value.substr(first, last - first + 1);
}

bool IsRule(const std::wstring& line) {
    std::wstring value;
    for (wchar_t ch : line) if (ch != L' ' && ch != L'\t') value += ch;
    if (value.size() < 3) return false;
    const wchar_t first = value[0];
    if (first != L'-' && first != L'*' && first != L'_') return false;
    for (wchar_t ch : value) if (ch != first) return false;
    return true;
}

std::vector<std::wstring> TableCells(const std::wstring& line) {
    std::vector<std::wstring> cells;
    std::wstring current;
    for (std::size_t i = 1; i < line.size(); ++i) {
        if (line[i] == L'|') {
            cells.push_back(Trim(current));
            current.clear();
        } else {
            current += line[i];
        }
    }
    return cells;
}

void Parse(ViewState& state) {
    state.blocks.clear();
    std::wstringstream stream(state.markdown);
    std::wstring line;
    std::vector<std::wstring> table;
    bool code = false;

    auto flushTable = [&]() {
        if (table.empty()) return;
        Block block;
        block.kind = Kind::Table;
        for (const auto& row : table) {
            const auto cells = TableCells(row);
            bool separator = true;
            for (const auto& cell : cells) {
                for (wchar_t ch : cell) {
                    if (ch != L'-' && ch != L':' && ch != L' ') separator = false;
                }
            }
            if (!separator) block.rows.push_back(cells);
        }
        if (!block.rows.empty()) state.blocks.push_back(std::move(block));
        table.clear();
    };

    while (std::getline(stream, line)) {
        if (!line.empty() && line.back() == L'\r') line.pop_back();
        if (line.rfind(L"```", 0) == 0) {
            flushTable();
            code = !code;
            continue;
        }
        if (code) {
            Block block;
            block.kind = Kind::Code;
            block.text = line;
            state.blocks.push_back(std::move(block));
            continue;
        }
        if (line.size() > 2 && line.front() == L'|' && line.back() == L'|') {
            table.push_back(line);
            continue;
        }
        flushTable();
        if (line.empty()) continue;
        if (IsRule(line)) {
            Block block;
            block.kind = Kind::Rule;
            state.blocks.push_back(std::move(block));
            continue;
        }
        if (line[0] == L'#') {
            std::size_t level = 0;
            while (level < line.size() && line[level] == L'#') ++level;
            Block block;
            block.kind = Kind::Heading;
            block.level = static_cast<int>(level);
            block.text = StripInline(Trim(line.substr(level)));
            state.blocks.push_back(std::move(block));
            continue;
        }
        if (line.rfind(L"[[META:", 0) == 0 && line.back() == L']') {
            const auto close = line.find(L"]]", 7);
            if (close != std::wstring::npos) {
                Block block;
                block.kind = Kind::Meta;
                block.text = Trim(line.substr(7, close - 7));
                state.blocks.push_back(std::move(block));
                continue;
            }
        }
        if (line.rfind(L"[[GALLERY:", 0) == 0) {
            const auto close = line.find(L"]]", 10);
            if (close != std::wstring::npos) {
                Block block;
                block.kind = Kind::Gallery;
                const std::wstring payload = line.substr(10, close - 10);
                size_t start = 0;
                while (start <= payload.size()) {
                    const auto pipe = payload.find(L'|', start);
                    std::wstring path = payload.substr(start, pipe == std::wstring::npos ? std::wstring::npos : pipe - start);
                    std::replace(path.begin(), path.end(), '\\', '/');
                    if (!path.empty()) block.paths.push_back(path);
                    if (pipe == std::wstring::npos) break;
                    start = pipe + 1;
                }
                state.blocks.push_back(std::move(block));
                continue;
            }
        }
        if (line.rfind(L"[[AVATAR:", 0) == 0 && line.size() > 12 && line.back() == L']') {
            const auto close = line.find(L"]]", 9);
            if (close != std::wstring::npos) {
                const std::wstring payload = line.substr(9, close - 9);
                const auto pipe = payload.find(L'|');
                Block block;
                block.kind = Kind::Avatar;
                block.text = pipe == std::wstring::npos ? L"assistant" : payload.substr(0, pipe);
                block.path = pipe == std::wstring::npos ? L"" : payload.substr(pipe + 1);
                state.blocks.push_back(std::move(block));
                continue;
            }
        }
        const auto imageStart = line.find(L"![");
        if (imageStart != std::wstring::npos) {
            const auto close = line.find(L']', imageStart);
            const auto open = close == std::wstring::npos ? close : line.find(L'(', close);
            const auto end = open == std::wstring::npos ? open : line.find(L')', open);
            if (close != std::wstring::npos && open != std::wstring::npos && end != std::wstring::npos) {
                if (imageStart > 0) {
                    Block prefix;
                    prefix.kind = Kind::Paragraph;
                    prefix.text = Trim(line.substr(0, imageStart));
                    if (!prefix.text.empty()) state.blocks.push_back(std::move(prefix));
                }
                Block block;
                block.kind = Kind::Image;
                block.path = line.substr(open + 1, end - open - 1);
                std::replace(block.path.begin(), block.path.end(), '\\', '/');
                state.blocks.push_back(std::move(block));
                const std::wstring suffix = Trim(line.substr(end + 1));
                if (!suffix.empty()) {
                    Block tail;
                    tail.kind = Kind::Paragraph;
                    tail.text = suffix;
                    state.blocks.push_back(std::move(tail));
                }
                continue;
            }
        }
        Block block;
        if (line.rfind(L"- ", 0) == 0 || line.rfind(L"* ", 0) == 0) {
            block.kind = Kind::List;
            block.text = Trim(line.substr(2));
        } else {
            block.kind = Kind::Paragraph;
            block.text = Trim(line);
        }
        state.blocks.push_back(std::move(block));
    }
    flushTable();
}

void DrawTable(Graphics& graphics, ViewState& state, const Block& block,
               int x, int& y, int width, int& totalHeight) {
    if (block.rows.empty()) return;
    const int columns = static_cast<int>(block.rows.front().size());
    if (columns <= 0) return;
    std::vector<int> widths(columns, 0);
    for (const auto& row : block.rows) {
        for (int c = 0; c < columns && c < static_cast<int>(row.size()); ++c) {
            widths[c] = max(widths[c], min(30, static_cast<int>(row[c].size())));
        }
    }
    int totalWeight = 0;
    for (int weight : widths) totalWeight += max(4, weight);
    const int rowHeight = 30;
    int left = x;
    std::vector<int> edges;
    for (int c = 0; c < columns; ++c) {
        left += width * max(4, widths[c]) / totalWeight;
        edges.push_back(left);
    }
    for (std::size_t r = 0; r < block.rows.size(); ++r) {
        int cellLeft = x;
        for (int c = 0; c < columns; ++c) {
            const int cellRight = edges[c];
            RectF cell(static_cast<REAL>(cellLeft), static_cast<REAL>(y),
                       static_cast<REAL>(cellRight - cellLeft), static_cast<REAL>(rowHeight));
            graphics.DrawRectangle(&Pen(Color(255, 205, 190, 220)), cell);
            if (c < static_cast<int>(block.rows[r].size())) {
                StringFormat format;
                format.SetLineAlignment(StringAlignmentCenter);
                format.SetTrimming(StringTrimmingEllipsisCharacter);
                graphics.DrawString(block.rows[r][c].c_str(), -1,
                                    r == 0 ? state.bold : state.body, cell, &format, &SolidBrush(Color(255, 36, 24, 51)));
            }
            cellLeft = cellRight;
        }
        y += rowHeight;
    }
    y += 8;
    totalHeight = y;
}

void DrawRichWrapped(Graphics& graphics, const std::wstring& text, Font* base,
                     Font* mono, int x, int& y, int width, ViewState* state = nullptr) {
    struct Run { std::wstring text; int style; bool code; bool strike; bool link; std::wstring url; };
    std::vector<Run> runs;
    int style = 0;
    bool code = false;
    bool strike = false;
    std::wstring current;
    for (size_t i = 0; i < text.size();) {
        if (text[i] == L'`') {
            if (!current.empty()) { runs.push_back({current, style, code, strike, false}); current.clear(); }
            code = !code; ++i; continue;
        }
        if (text.compare(i, 2, L"~~") == 0) {
            if (!current.empty()) { runs.push_back({current, style, code, strike, false}); current.clear(); }
            strike = !strike; i += 2; continue;
        }
        if (text[i] == L'[' && !code) {
            const auto close = text.find(L"](", i + 1);
            const auto endLink = close == std::wstring::npos ? close : text.find(L')', close + 2);
            if (close != std::wstring::npos && endLink != std::wstring::npos) {
                if (!current.empty()) { runs.push_back({current, style, code, strike, false}); current.clear(); }
                runs.push_back({text.substr(i + 1, close - i - 1), style, code, strike, true,
                               text.substr(close + 2, endLink - close - 2)});
                i = endLink + 1;
                continue;
            }
        }
        if (text.compare(i, 3, L"***") == 0) {
            if (!current.empty()) { runs.push_back({current, style, code, strike, false}); current.clear(); }
            style ^= 3; i += 3; continue;
        }
        if (text.compare(i, 2, L"**") == 0) {
            if (!current.empty()) { runs.push_back({current, style, code, strike, false}); current.clear(); }
            style ^= 1; i += 2; continue;
        }
        if (text[i] == L'*' && !code) {
            if (!current.empty()) { runs.push_back({current, style, code, strike, false}); current.clear(); }
            style ^= 2; i += 1; continue;
        }
        current += text[i++];
    }
    if (!current.empty()) runs.push_back({current, style, code, strike, false});
    if (runs.empty()) return;

    FontFamily family;
    base->GetFamily(&family);
    const REAL fontSize = base->GetSize();
    Font bold(&family, fontSize, FontStyleBold, UnitPixel);
    Font italic(&family, fontSize, FontStyleItalic, UnitPixel);
    Font boldItalic(&family, fontSize, FontStyleBold | FontStyleItalic, UnitPixel);
    Font* fonts[4] = {base, &bold, &italic, &boldItalic};
    SolidBrush normalBrush(Color(255, 36, 24, 51));
    SolidBrush linkBrush(Color(255, 92, 52, 160));
    SolidBrush codeBackground(Color(255, 236, 223, 248));
    REAL cursor = static_cast<REAL>(x);
    const REAL limit = static_cast<REAL>(x + width);
    const REAL lineHeight = base->GetHeight(&graphics) + 4.0f;

    auto measure = [&](const std::wstring& value, Font* font) -> REAL {
        if (value.empty()) return 0.0f;
        RectF bounds;
        graphics.MeasureString(value.c_str(), static_cast<INT>(value.size()), font,
                               PointF(0, 0), &bounds);
        return bounds.Width;
    };

    auto drawPiece = [&](const std::wstring& piece, Font* font, const Run& run) {
        const REAL pieceWidth = measure(piece, font);
        if (run.code) {
            graphics.FillRectangle(&codeBackground,
                                   RectF(cursor - 2.0f, static_cast<REAL>(y) - 1.0f,
                                         pieceWidth + 4.0f, lineHeight - 3.0f));
        }
        Brush* brush = run.link ? &linkBrush : &normalBrush;
        graphics.DrawString(piece.c_str(), static_cast<INT>(piece.size()), font,
                            PointF(cursor, static_cast<REAL>(y)), brush);
        if (run.strike) {
            Pen line(Color(255, 36, 24, 51), 1.4f);
            graphics.DrawLine(&line, PointF(cursor, static_cast<REAL>(y) + lineHeight * 0.55f),
                              PointF(cursor + pieceWidth, static_cast<REAL>(y) + lineHeight * 0.55f));
        }
        if (run.link) {
            if (state != nullptr && !run.url.empty()) {
                state->linkRects.push_back({RECT{static_cast<LONG>(cursor), static_cast<LONG>(y),
                                                 static_cast<LONG>(cursor + pieceWidth),
                                                 static_cast<LONG>(y + lineHeight)}, run.url});
            }
            Pen underline(Color(255, 92, 52, 160), 1.2f);
            graphics.DrawLine(&underline, PointF(cursor, static_cast<REAL>(y) + lineHeight - 5.0f),
                              PointF(cursor + pieceWidth, static_cast<REAL>(y) + lineHeight - 5.0f));
        }
        cursor += pieceWidth;
    };

    for (const auto& run : runs) {
        Font* font = run.code ? mono : fonts[run.style & 3];
        size_t offset = 0;
        while (offset < run.text.size()) {
            std::wstring remaining = run.text.substr(offset);
            if (cursor + measure(remaining, font) <= limit) {
                drawPiece(remaining, font, run);
                offset = run.text.size();
                continue;
            }
            size_t low = 0;
            size_t high = remaining.size();
            while (low < high) {
                const size_t mid = (low + high + 1) / 2;
                if (cursor + measure(remaining.substr(0, mid), font) <= limit) low = mid;
                else high = mid - 1;
            }
            if (low == 0) {
                y += static_cast<int>(lineHeight);
                cursor = static_cast<REAL>(x);
                continue;
            }
            if (low < remaining.size()) {
                const size_t space = remaining.rfind(L' ', low);
                if (space != std::wstring::npos && space > 0) low = space + 1;
            }
            std::wstring piece = remaining.substr(0, low);
            drawPiece(piece, font, run);
            offset += low;
            y += static_cast<int>(lineHeight);
            cursor = static_cast<REAL>(x);
        }
    }
    y += static_cast<int>(lineHeight + 2.0f);
}

void DrawWrapped(Graphics& graphics, const std::wstring& text, Font* font,
                 Brush* brush, int x, int& y, int width, bool rightAlign = false) {
    if (text.empty()) return;
    const REAL lineHeight = font->GetHeight(&graphics) + 3.0f;
    std::wstring line;
    auto flush = [&]() {
        if (line.empty()) return;
        REAL drawX = static_cast<REAL>(x);
        if (rightAlign) {
            RectF bounds;
            graphics.MeasureString(line.c_str(), -1, font, PointF(0, 0), &bounds);
            drawX = static_cast<REAL>(x + width) - bounds.Width;
        }
        graphics.DrawString(line.c_str(), -1, font,
                            PointF(drawX, static_cast<REAL>(y)), brush);
        y += static_cast<int>(lineHeight);
        line.clear();
    };
    for (wchar_t ch : text) {
        std::wstring candidate = line;
        candidate += ch;
        RectF bounds;
        graphics.MeasureString(candidate.c_str(), -1, font, PointF(0, 0), &bounds);
        if (bounds.Width > width && !line.empty()) {
            flush();
        }
        line += ch;
    }
    flush();
}

void UpdateScrollbar(HWND hwnd, ViewState& state, int clientHeight);

void PaintView(HWND hwnd, ViewState& state, HDC target) {
    RECT rc{};
    GetClientRect(hwnd, &rc);
    const int width = max(120, rc.right - 24);
    const int left = 12;

    RECT client{};
    GetClientRect(hwnd, &client);
    const int maxScroll = max(0, state.totalHeight - client.bottom);
    if (state.scroll < 0) state.scroll = 0;
    if (state.scroll > maxScroll) state.scroll = maxScroll;
    int y = 10 - state.scroll;
    state.imageRects.clear();
    state.imagePaths.clear();
    state.linkRects.clear();

    Graphics graphics(target);
    graphics.SetSmoothingMode(SmoothingModeAntiAlias);
    graphics.SetTextRenderingHint(TextRenderingHintClearTypeGridFit);
    graphics.SetClip(RectF(0, 0, static_cast<REAL>(rc.right), static_cast<REAL>(rc.bottom)));

    bool rightAligned = false;
    for (const auto& block : state.blocks) {
        if (block.kind == Kind::Avatar) {
            rightAligned = block.text == L"user";
            Gdiplus::Image avatar(block.path.c_str());
            if (avatar.GetLastStatus() == Ok) {
                const int side = 64;
                const int x = block.text == L"user" ? left + width - side : left;
                graphics.DrawImage(&avatar, RectF(static_cast<REAL>(x), static_cast<REAL>(y),
                                                  static_cast<REAL>(side), static_cast<REAL>(side)));
                y += side + 12;
            }
        } else if (block.kind == Kind::Meta) {
            DrawWrapped(graphics, block.text, state.meta,
                        &SolidBrush(Color(255, 130, 112, 150)), left, y, width, true);
            y += 4;
        } else if (block.kind == Kind::Rule) {
            graphics.DrawLine(&Pen(Color(255, 170, 150, 195), 1),
                              static_cast<REAL>(left), static_cast<REAL>(y + 8),
                              static_cast<REAL>(left + width), static_cast<REAL>(y + 8));
            y += 24;
        } else if (block.kind == Kind::Heading) {
            Font* font = block.level <= 1 ? state.heading : state.bold;
            DrawWrapped(graphics, block.text, font,
                        &SolidBrush(Color(255, 36, 24, 51)), left, y, width);
            y += 10;
        } else if (block.kind == Kind::List) {
            graphics.FillEllipse(&SolidBrush(Color(255, 107, 46, 145)),
                                 static_cast<REAL>(left + 4), static_cast<REAL>(y + 9), 5.0f, 5.0f);
            DrawWrapped(graphics, block.text, state.body,
                        &SolidBrush(Color(255, 36, 24, 51)), left + 18, y, width - 18);
            y += 4;
        } else if (block.kind == Kind::Code) {
            RectF box(static_cast<REAL>(left), static_cast<REAL>(y),
                      static_cast<REAL>(width), 24);
            graphics.FillRectangle(&SolidBrush(Color(255, 237, 224, 250)), box);
            graphics.DrawString(block.text.c_str(), -1, state.mono,
                                PointF(static_cast<REAL>(left + 8), static_cast<REAL>(y + 3)),
                                &SolidBrush(Color(255, 60, 40, 80)));
            y += 26;
        } else if (block.kind == Kind::Gallery) {
            const int count = static_cast<int>(block.paths.size());
            const int gap = 8;
            int columns = width / 150;
            columns = (std::max)(1, (std::min)(columns, count));
            columns = (std::min)(columns, 8);
            const int cell = (width - gap * (columns - 1)) / columns;
            for (int i = 0; i < count; ++i) {
                const int column = i % columns;
                const int row = i / columns;
                const int x = left + column * (cell + gap);
                const int yCell = y + row * (cell + gap);
                Gdiplus::Image image(block.paths[i].c_str());
                if (image.GetLastStatus() != Ok) continue;
                const REAL side = static_cast<REAL>((std::min)(static_cast<int>(image.GetWidth()), static_cast<int>(image.GetHeight())));
                const REAL srcX = (image.GetWidth() - side) / 2.0f;
                const REAL srcY = (image.GetHeight() - side) / 2.0f;
                graphics.DrawImage(&image, RectF(static_cast<REAL>(x), static_cast<REAL>(yCell),
                                                  static_cast<REAL>(cell), static_cast<REAL>(cell)),
                                   srcX, srcY, side, side, UnitPixel);
                graphics.DrawRectangle(&Pen(Color(255, 30, 20, 40), 2.0f),
                                       static_cast<REAL>(x), static_cast<REAL>(yCell),
                                       static_cast<REAL>(cell), static_cast<REAL>(cell));
                state.imageRects.push_back({x, yCell, x + cell, yCell + cell});
                state.imagePaths.push_back(block.paths[i]);
            }
            const int rows = (count + columns - 1) / columns;
            y += rows * cell + (rows - 1) * gap + 12;
        } else if (block.kind == Kind::Image) {
            Gdiplus::Image image(block.path.c_str());
            if (image.GetLastStatus() == Ok) {
                const bool rendered = block.path.find(L"/render/") != std::wstring::npos ||
                                      block.path.find(L"\\render\\") != std::wstring::npos;
                const int maxW = rendered ? (std::min)(width - 20, 560) : (std::min)(width, 280);
                const int maxH = rendered ? 420 : 180;
                const REAL maxScale = rendered ? 2.5f : 1.0f;
                const REAL scale = min(static_cast<REAL>(maxW) / image.GetWidth(),
                                       static_cast<REAL>(maxH) / image.GetHeight());
                const int w = max(40, static_cast<int>(image.GetWidth() * min(scale, maxScale)));
                const int h = max(30, static_cast<int>(image.GetHeight() * min(scale, maxScale)));
                RECT rect{left, y, left + w, y + h};
                state.imageRects.push_back(rect);
                state.imagePaths.push_back(block.path);
                graphics.DrawImage(&image, RectF(static_cast<REAL>(rect.left), static_cast<REAL>(rect.top),
                                                 static_cast<REAL>(w), static_cast<REAL>(h)));
                y += h + 12;
            }
        } else if (block.kind == Kind::Table) {
            DrawTable(graphics, state, block, left, y, width, y);
        } else {
            const bool userLine = block.text.rfind(L"你：", 0) == 0;
            if (rightAligned || userLine) {
                DrawWrapped(graphics, block.text, state.body,
                            &SolidBrush(Color(255, 36, 24, 51)), left, y, width, true);
            } else if (block.text.find(L'*') != std::wstring::npos || block.text.find(L'`') != std::wstring::npos || block.text.find(L'~') != std::wstring::npos || block.text.find(L"](") != std::wstring::npos) {
                DrawRichWrapped(graphics, block.text, state.body, state.mono, left, y, width, &state);
            } else {
                DrawWrapped(graphics, block.text, state.body,
                            &SolidBrush(Color(255, 36, 24, 51)), left, y, width, false);
            }
            y += 6;
        }
    }
    state.totalHeight = y + state.scroll + 20;
    UpdateScrollbar(hwnd, state, client.bottom);
}

void UpdateScrollbar(HWND hwnd, ViewState& state, int clientHeight) {
    SCROLLINFO info{};
    info.cbSize = sizeof(info);
    info.fMask = SIF_RANGE | SIF_PAGE | SIF_POS;
    info.nMin = 0;
    info.nMax = max(0, state.totalHeight);
    info.nPage = max(1, clientHeight);
    info.nPos = state.scroll;
    SetScrollInfo(hwnd, SB_VERT, &info, TRUE);
}

LRESULT CALLBACK ChatViewProc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
    auto* state = reinterpret_cast<ViewState*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (message) {
    case WM_CREATE: {
        auto* created = new ViewState();
        created->body = new Font(L"Source Han Sans", 17.0f);
        created->heading = new Font(L"Noto Serif SC", 25.0f, FontStyleBold);
        created->bold = new Font(L"Source Han Sans", 18.0f, FontStyleBold);
        created->mono = new Font(L"Consolas", 15.0f);
        created->meta = new Font(L"Source Han Sans", 12.0f);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(created));
        return 0;
    }
    case kSetFonts: {
        auto* families = reinterpret_cast<std::pair<std::wstring, std::wstring>*>(lparam);
        if (state && families) {
            delete state->body; delete state->heading; delete state->bold; delete state->meta;
            state->body = new Font(families->first.c_str(), 17.0f);
            state->heading = new Font(families->second.c_str(), 25.0f, FontStyleBold);
            state->bold = new Font(families->first.c_str(), 18.0f, FontStyleBold);
            state->meta = new Font(families->first.c_str(), 12.0f);
            delete families;
            InvalidateRect(hwnd, nullptr, TRUE);
        }
        return 0;
    }
    case kSetMarkdown: {
        auto* text = reinterpret_cast<std::wstring*>(lparam);
        if (state && text) {
            state->markdown = *text;
            Parse(*state);
            state->scroll = 0;
            state->scrollBottom = true;
            delete text;
            InvalidateRect(hwnd, nullptr, TRUE);
        }
        return 0;
    }
    case WM_MOUSEWHEEL: {
        if (!state) return 0;
        const int delta = GET_WHEEL_DELTA_WPARAM(wparam);
        RECT client{};
        GetClientRect(hwnd, &client);
        const int maxScroll = max(0, state->totalHeight - client.bottom);
        state->scroll -= delta / 120 * 54;
        if (state->scroll < 0) state->scroll = 0;
        if (state->scroll > maxScroll) state->scroll = maxScroll;
        InvalidateRect(hwnd, nullptr, TRUE);
        return 0;
    }
    case WM_LBUTTONUP: {
        if (!state) return 0;
        POINT point{GET_X_LPARAM(lparam), GET_Y_LPARAM(lparam)};
        for (size_t i = 0; i < state->imageRects.size(); ++i) {
            const RECT& rect = state->imageRects[i];
            if (PtInRect(&rect, point)) {
                std::wstring target = state->imagePaths[i];
                const std::wstring suffix = L".thumb.jpg";
                if (target.size() > suffix.size() &&
                    _wcsicmp(target.c_str() + target.size() - suffix.size(), suffix.c_str()) == 0) {
                    const std::wstring stem = target.substr(0, target.size() - suffix.size());
                    for (const wchar_t* ext : {L".mp4", L".mov", L".mkv", L".avi", L".webm", L".m4v", L".wmv", L".flv", L".ts"}) {
                        const std::wstring video = stem + ext;
                        if (GetFileAttributesW(video.c_str()) != INVALID_FILE_ATTRIBUTES) {
                            target = video;
                            break;
                        }
                    }
                }
                ShellExecuteW(hwnd, L"open", target.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
                break;
            }
        }
        return 0;
    }
    case WM_VSCROLL: {
        if (!state) return 0;
        SCROLLINFO info{};
        info.cbSize = sizeof(info);
        info.fMask = SIF_ALL;
        GetScrollInfo(hwnd, SB_VERT, &info);
        int position = info.nPos;
        switch (LOWORD(wparam)) {
            case SB_LINEUP: position -= 40; break;
            case SB_LINEDOWN: position += 40; break;
            case SB_PAGEUP: position -= info.nPage; break;
            case SB_PAGEDOWN: position += info.nPage; break;
            case SB_THUMBTRACK:
            case SB_THUMBPOSITION: position = info.nTrackPos; break;
            case SB_TOP: position = 0; break;
            case SB_BOTTOM: position = info.nMax; break;
            default: break;
        }
        position = max(info.nMin, min(info.nMax, position));
        state->scroll = position;
        state->scrollBottom = false;
        SetScrollInfo(hwnd, SB_VERT, &info, TRUE);
        InvalidateRect(hwnd, nullptr, TRUE);
        return 0;
    }
    case WM_PAINT: {
        PAINTSTRUCT ps{};
        HDC dc = BeginPaint(hwnd, &ps);
        if (state) {
            RECT rc{};
            GetClientRect(hwnd, &rc);
            HDC memory = CreateCompatibleDC(dc);
            HBITMAP bitmap = CreateCompatibleBitmap(dc, max(1, rc.right), max(1, rc.bottom));
            HGDIOBJ old = SelectObject(memory, bitmap);
            HBRUSH bg = CreateSolidBrush(state->background);
            FillRect(memory, &rc, bg);
            DeleteObject(bg);
            PaintView(hwnd, *state, memory);
            if (state->scrollBottom && state->totalHeight > rc.bottom) {
                state->scroll = state->totalHeight - rc.bottom + 16;
                state->scrollBottom = false;
                HBRUSH background = CreateSolidBrush(state->background);
                FillRect(memory, &rc, background);
                DeleteObject(background);
                PaintView(hwnd, *state, memory);
            }
            BitBlt(dc, 0, 0, rc.right, rc.bottom, memory, 0, 0, SRCCOPY);
            SelectObject(memory, old);
            DeleteObject(bitmap);
            DeleteDC(memory);
        }
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_ERASEBKGND:
        return 1;
    case WM_SIZE:
        InvalidateRect(hwnd, nullptr, TRUE);
        return 0;
    case WM_DESTROY:
        if (state) {
            delete state->body;
            delete state->heading;
            delete state->mono;
            delete state->bold;
            delete state;
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
        }
        return 0;
    default:
        return DefWindowProcW(hwnd, message, wparam, lparam);
    }
}

} // namespace

HWND CreateChatView(HWND parent, int id) {
    static bool registered = false;
    if (!registered) {
        WNDCLASSEXW wc{};
        wc.cbSize = sizeof(wc);
        wc.style = CS_HREDRAW | CS_VREDRAW;
        wc.lpfnWndProc = ChatViewProc;
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        wc.hbrBackground = nullptr;
        wc.lpszClassName = kClass;
        RegisterClassExW(&wc);
        registered = true;
    }
    return CreateWindowExW(0, kClass, L"", WS_CHILD | WS_VSCROLL,
                           0, 0, 0, 0, parent, reinterpret_cast<HMENU>(id),
                           GetModuleHandleW(nullptr), nullptr);
}

void SetChatViewMarkdown(HWND view, const std::string& markdown) {
    if (!view) return;
    auto* text = new std::wstring(FromUtf8(markdown));
    SendMessageW(view, kSetMarkdown, 0, reinterpret_cast<LPARAM>(text));
}

void SetChatViewBackground(HWND view, COLORREF color) {
    if (!view) return;
    auto* state = reinterpret_cast<ViewState*>(GetWindowLongPtrW(view, GWLP_USERDATA));
    if (state) {
        state->background = color;
        InvalidateRect(view, nullptr, TRUE);
    }
}

void ScrollChatViewToBottom(HWND view) {
    if (view) {
        SendMessageW(view, WM_VSCROLL, SB_BOTTOM, 0);
    }
}

void SetChatViewFonts(HWND view, const std::wstring& sans, const std::wstring& serif) {
    if (!view) return;
    auto* families = new std::pair<std::wstring, std::wstring>(sans, serif);
    SendMessageW(view, kSetFonts, 0, reinterpret_cast<LPARAM>(families));
}
