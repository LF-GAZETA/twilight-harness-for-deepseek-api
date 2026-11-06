#include "text_util.h"

#include <windows.h>

std::wstring CleanTextUtf8(const std::string& text) {
    std::string value = text;
    for (int pass = 0; pass < 3; ++pass) {
        std::string replaced;
        for (std::size_t i = 0; i < value.size(); ++i) {
            if (value[i] == '\\' && i + 1 < value.size()) {
                const char n = value[i + 1];
                if (n == 'n') {
                    replaced += '\n';
                    ++i;
                    continue;
                }
                if (n == 'r') {
                    replaced += '\r';
                    ++i;
                    continue;
                }
                if (n == 't') {
                    replaced += '\t';
                    ++i;
                    continue;
                }
            }
            replaced += value[i];
        }
        if (replaced == value) break;
        value = replaced;
    }
    if (value.empty()) return {};
    const int size = MultiByteToWideChar(CP_UTF8, 0, value.data(),
                                         static_cast<int>(value.size()), nullptr, 0);
    std::wstring out(size, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                        out.data(), size);
    return out;
}
