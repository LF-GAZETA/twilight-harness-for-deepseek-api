#pragma once

#include <string>

// Converts a small markdown subset to RTF understood by RichEdit 4.1+.
// Supports headings, bold, inline code, code fences, lists, tables,
// links and local images: ![alt](C:/path/image.png)
std::string MarkdownToRtf(const std::string& markdownUtf8, int widthTwips = 6500);
