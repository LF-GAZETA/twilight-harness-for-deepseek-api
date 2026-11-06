#pragma once

#include <windows.h>
#include <string>

HWND CreateChatView(HWND parent, int id);
void SetChatViewMarkdown(HWND view, const std::string& markdown);
void ScrollChatViewToBottom(HWND view);
void SetChatViewBackground(HWND view, COLORREF color);
void SetChatViewFonts(HWND view, const std::wstring& sans, const std::wstring& serif);
