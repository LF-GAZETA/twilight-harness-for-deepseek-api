#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <windows.h>

#include "sidecar_client.h"
#include <vector>
#include <fstream>
#include <thread>

#pragma comment(lib, "ws2_32.lib")

namespace {
void Log(const std::string& text) {
    std::ofstream out("sidecar-client.log", std::ios::app);
    out << text << "\n";
}

std::string Narrow(const std::wstring& value) {
    if (value.empty()) return {};
    const int size = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                                         nullptr, 0, nullptr, nullptr);
    std::string out(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                        out.data(), size, nullptr, nullptr);
    return out;
}

std::wstring Quote(const std::wstring& value) {
    return L"\"" + value + L"\"";
}

std::string JsonString(const std::string& raw, const std::string& key) {
    const std::string needle = "\"" + key + "\"";
    auto pos = raw.find(needle);
    if (pos == std::string::npos) return {};
    pos = raw.find(':', pos + needle.size());
    if (pos == std::string::npos) return {};
    pos = raw.find('"', pos + 1);
    if (pos == std::string::npos) return {};
    const auto end = raw.find('"', pos + 1);
    if (end == std::string::npos) return {};
    return raw.substr(pos + 1, end - pos - 1);
}
} // namespace

SidecarClient::~SidecarClient() { Stop(); }

bool SidecarClient::Start(const std::wstring& python, const std::wstring& script,
                          const std::wstring& workdir, const std::wstring& handshakeFile) {
    if (process_.hProcess) return ready();

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    HANDLE stderrFile = CreateFileW(L"sidecar-stderr.txt", FILE_APPEND_DATA,
                                    FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                                    OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (stderrFile != INVALID_HANDLE_VALUE) {
        SetHandleInformation(stderrFile, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdOutput = stderrFile;
        si.hStdError = stderrFile;
    }

    std::wstring command = Quote(python) + L" " + Quote(script) +
                           L" --port 0 --token " + std::to_wstring(GetCurrentProcessId()) +
                           L"-" + std::to_wstring(GetTickCount64()) +
                           L" --write-handshake " + Quote(handshakeFile);

    DeleteFileW(handshakeFile.c_str());
    Log("starting: " + Narrow(command));
    if (!CreateProcessW(nullptr, command.data(), nullptr, nullptr, TRUE,
                        CREATE_NO_WINDOW, nullptr, workdir.c_str(), &si, &process_)) {
        Log("CreateProcess failed: " + std::to_string(GetLastError()));
        return false;
    }

    if (stderrFile != INVALID_HANDLE_VALUE) CloseHandle(stderrFile);
    for (int attempt = 0; attempt < 1800; ++attempt) {
        std::ifstream in(handshakeFile);
        if (in) {
            std::string content((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
            if (!content.empty()) {
                Log("handshake file: " + content);
                ParseHandshake(content);
                if (port_ > 0) return true;
            }
        }
        Sleep(100);
    }
    return false;
}

void SidecarClient::ParseHandshake(const std::string& line) {
    auto pos = line.find("\"port\"");
    if (pos == std::string::npos) return;
    pos = line.find(':', pos);
    if (pos == std::string::npos) return;
    ++pos;
    while (pos < line.size() && (line[pos] == ' ' || line[pos] == '	')) ++pos;
    std::string portText;
    if (pos < line.size() && line[pos] == '"') {
        ++pos;
        while (pos < line.size() && line[pos] != '"') portText.push_back(line[pos++]);
    } else {
        while (pos < line.size() && line[pos] >= '0' && line[pos] <= '9') portText.push_back(line[pos++]);
    }
    if (portText.empty()) return;
    try {
        port_ = std::stoi(portText);
    } catch (...) {
        port_ = 0;
    }
    const std::string token = JsonString(line, "token");
    token_.assign(token.begin(), token.end());
}

bool SidecarClient::Request(const std::wstring& method, const std::string& path,
                            const std::string& body, std::string& response, std::string& error) {
    response.clear();
    error.clear();
    if (!ready()) {
        error = "sidecar not ready";
        return false;
    }
    WSADATA wsa{};
    WSAStartup(MAKEWORD(2, 2), &wsa);
    SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock == INVALID_SOCKET) {
        error = "socket failed";
        WSACleanup();
        return false;
    }
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<u_short>(port_));
    addr.sin_addr.s_addr = inet_addr("127.0.0.1");
    bool connected = false;
    for (int attempt = 0; attempt < 15; ++attempt) {
        if (connect(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == 0) {
            connected = true;
            break;
        }
        Sleep(200);
    }
    if (!connected) {
        error = "connect failed: " + std::to_string(WSAGetLastError());
        closesocket(sock);
        WSACleanup();
        return false;
    }

    const std::wstring wideMethod = method;
    const std::string methodText(wideMethod.begin(), wideMethod.end());
    const std::string tokenText(token_.begin(), token_.end());
    std::string request = methodText + " " + path + " HTTP/1.1\r\n";
    request += "Host: 127.0.0.1\r\n";
    request += "X-Twilight-Token: " + tokenText + "\r\n";
    request += "Content-Type: application/json\r\n";
    request += "Content-Length: " + std::to_string(body.size()) + "\r\n";
    request += "Connection: close\r\n\r\n";
    request += body;
    send(sock, request.data(), static_cast<int>(request.size()), 0);

    std::string raw;
    char buffer[8192];
    for (;;) {
        int got = recv(sock, buffer, sizeof(buffer), 0);
        if (got <= 0) break;
        raw.append(buffer, got);
    }
    closesocket(sock);
    WSACleanup();

    const auto split = raw.find("\r\n\r\n");
    if (split == std::string::npos) {
        error = "malformed HTTP response";
        return false;
    }
    response = raw.substr(split + 4);
    return true;
}

bool SidecarClient::PostStream(const std::string& path, const std::string& body,
                               const std::function<void(const std::string&)>& onLine,
                               std::string& error) {
    error.clear();
    if (!ready()) {
        error = "sidecar not ready";
        return false;
    }
    WSADATA wsa{};
    WSAStartup(MAKEWORD(2, 2), &wsa);
    SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock == INVALID_SOCKET) {
        error = "socket failed";
        WSACleanup();
        return false;
    }
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<u_short>(port_));
    addr.sin_addr.s_addr = inet_addr("127.0.0.1");
    if (connect(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        error = "connect failed: " + std::to_string(WSAGetLastError());
        closesocket(sock);
        WSACleanup();
        return false;
    }
    const std::string tokenText(token_.begin(), token_.end());
    std::string request = "POST " + path + " HTTP/1.1\r\nHost: 127.0.0.1\r\n";
    request += "X-Twilight-Token: " + tokenText + "\r\nContent-Type: application/json\r\n";
    request += "Content-Length: " + std::to_string(body.size()) + "\r\nConnection: close\r\n\r\n";
    request += body;
    send(sock, request.data(), static_cast<int>(request.size()), 0);

    std::string raw;
    bool headersDone = false;
    char buffer[4096];
    for (;;) {
        int got = recv(sock, buffer, sizeof(buffer), 0);
        if (got <= 0) break;
        raw.append(buffer, got);
        if (!headersDone) {
            const auto split = raw.find("\r\n\r\n");
            if (split != std::string::npos) {
                raw.erase(0, split + 4);
                headersDone = true;
            }
        }
        if (headersDone) {
            size_t pos = 0;
            while ((pos = raw.find('\n', pos)) != std::string::npos) {
                std::string line = raw.substr(0, pos);
                if (!line.empty() && line.back() == '\r') line.pop_back();
                raw.erase(0, pos + 1);
                pos = 0;
                onLine(line);
            }
        }
    }
    closesocket(sock);
    WSACleanup();
    return true;
}

bool SidecarClient::Get(const std::string& path, std::string& response, std::string& error) {
    return Request(L"GET", path, {}, response, error);
}

bool SidecarClient::Post(const std::string& path, const std::string& body,
                         std::string& response, std::string& error) {
    return Request(L"POST", path, body, response, error);
}

void SidecarClient::Stop() {
    if (ready()) {
        std::string response;
        std::string error;
        Post("/shutdown", "{}", response, error);
    }
    if (process_.hProcess) {
        WaitForSingleObject(process_.hProcess, 2000);
        TerminateProcess(process_.hProcess, 0);
        CloseHandle(process_.hThread);
        CloseHandle(process_.hProcess);
        process_ = {};
    }
    if (stdoutRead_) {
        CloseHandle(stdoutRead_);
        stdoutRead_ = nullptr;
    }
    port_ = 0;
}
