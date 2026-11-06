#pragma once

#include <windows.h>
#include <string>
#include <functional>

class SidecarClient {
public:
    SidecarClient() = default;
    ~SidecarClient();

    SidecarClient(const SidecarClient&) = delete;
    SidecarClient& operator=(const SidecarClient&) = delete;

    bool Start(const std::wstring& python, const std::wstring& script,
               const std::wstring& workdir, const std::wstring& handshakeFile);
    void Stop();

    bool ready() const { return port_ > 0; }
    int port() const { return port_; }
    const std::wstring& token() const { return token_; }

    bool Get(const std::string& path, std::string& response, std::string& error);
    bool Post(const std::string& path, const std::string& body,
              std::string& response, std::string& error);
    bool PostStream(const std::string& path, const std::string& body,
                    const std::function<void(const std::string&)>& onLine,
                    std::string& error);

private:
    bool Request(const std::wstring& method, const std::string& path,
                 const std::string& body, std::string& response, std::string& error);
    void ParseHandshake(const std::string& line);

    PROCESS_INFORMATION process_{};
    HANDLE stdoutRead_ = nullptr;
    HANDLE stdoutWrite_ = nullptr;
    int port_ = 0;
    std::wstring token_;
};
