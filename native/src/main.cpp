#include <windows.h>
#include <mmsystem.h>
#include <commdlg.h>
#include <richedit.h>
#include <imm.h>
#include <shellapi.h>
#include <windowsx.h>
#include <gdiplus.h>
#include <commctrl.h>
#include <uxtheme.h>
#pragma comment(lib, "uxtheme.lib")
#include <string>
#include <fstream>
#include <vector>
#include <thread>
#include <mutex>
#include <algorithm>
#include <atomic>
#include <deque>
#include <condition_variable>

#include "sidecar_client.h"
#include "text_util.h"
#include "markdown.h"
#include "chat_view.h"

#pragma comment(lib, "comctl32.lib")

using namespace Gdiplus;

namespace {

constexpr wchar_t kClass[] = L"TwilightHarnessNativeWindow";
constexpr wchar_t kTitle[] = L"Twilight Harness";
constexpr int kLeftWidth = 410;
constexpr const wchar_t* kLanguages[] = {
    L"简体中文（普通话）", L"简体中文（粤语·广东）", L"繁體中文（粵語·香港/澳門）",
    L"繁體中文（華語·台灣）", L"简体中文（华语·马来西亚）",
    L"English (UK)", L"English (US)",
    L"Français (France)", L"Français (Canada, Québec)", L"Français (Belgique)",
    L"Italiano", L"Deutsch (Deutschland)", L"Deutsch (Österreich)", L"Deutsch (Schweiz)",
    L"Português (Portugal)", L"Português (Brasil)", L"Español"
};

constexpr COLORREF kBg = RGB(247, 239, 255);
constexpr COLORREF kLeftBg = RGB(61, 17, 79);
constexpr COLORREF kRightBg = RGB(244, 231, 255);
constexpr COLORREF kPurple = RGB(107, 46, 145);
constexpr COLORREF kInk = RGB(36, 24, 51);

HINSTANCE g_instance = nullptr;
ULONG_PTR g_gdiplus = 0;
Image* g_hero = nullptr;
Image* g_panel = nullptr;

struct UiMetrics {
    RECT key{};
    RECT label{};
    RECT combo{};
};

UiMetrics Metrics(HWND hwnd) {
    RECT rc{};
    GetClientRect(hwnd, &rc);
    const int rightW = rc.right - kLeftWidth;
    const int fieldW = rightW - 96;
    const int fieldX = kLeftWidth + 48;
    const int keyTop = rc.bottom - 190;
    UiMetrics m{};
    m.key = RECT{fieldX, keyTop, fieldX + fieldW, keyTop + 44};
    m.label = RECT{fieldX, rc.bottom - 126, fieldX + 112, rc.bottom - 88};
    m.combo = RECT{fieldX + 118, rc.bottom - 128, fieldX + fieldW, rc.bottom - 88};
    return m;
}

SidecarClient g_sidecar;

HWND g_arrow = nullptr;
HWND g_key = nullptr;
HWND g_lang = nullptr;
HWND g_langLabel = nullptr;
HWND g_langButton = nullptr;
std::wstring g_language = L"简体中文（普通话）";
std::string g_variant = "plus";

enum class UiKey {
    KeyHint, Language, Effects, Aging, Squeeze, Frame, Apply, Vhs, Progress,
    AddAttachment, Attachment, NeedKey, SidecarTimeout, Error, FrameMultiplier,
    StartVoice, StartTheme, StartChat, DerpiDownload
};

int UiGroup() {
    if (g_language.rfind(L"简体中文", 0) == 0) return 0;
    if (g_language.rfind(L"繁體中文", 0) == 0) return 1;
    if (g_language.rfind(L"English", 0) == 0) return 2;
    if (g_language.rfind(L"Français", 0) == 0) return 3;
    if (g_language.rfind(L"Italiano", 0) == 0) return 4;
    if (g_language.rfind(L"Deutsch", 0) == 0) return 5;
    if (g_language.rfind(L"Português", 0) == 0) return 6;
    return 7;
}

const wchar_t* Ui(UiKey key) {
    static const wchar_t* table[][8] = {
        {L"输入 API Key", L"輸入 API Key", L"Enter API Key", L"Saisir la clé API", L"Inserisci chiave API", L"API-Schlüssel eingeben", L"Inserir chave API", L"Introduce la clave API"},
        {L"语言：", L"語言：", L"Language:", L"Langue :", L"Lingua:", L"Sprache:", L"Idioma:", L"Idioma:"},
        {L"效果设置", L"效果設定", L"Effects", L"Effets", L"Effetti", L"Effekte", L"Efeitos", L"Efectos"},
        {L"做旧", L"做舊", L"Aging", L"Vieillissement", L"Invecchiamento", L"Altern", L"Envelhecimento", L"Envejecimiento"},
        {L"压片", L"壓片", L"Crush", L"Compression", L"Compressione", L"Komprimieren", L"Compressão", L"Compresión"},
        {L"补帧", L"補幀", L"Frame interpolation", L"Interpolation", L"Interpolazione", L"Interpolation", L"Interpolação", L"Interpolación"},
        {L"应用", L"套用", L"Apply", L"Appliquer", L"Applica", L"Anwenden", L"Aplicar", L"Aplicar"},
        {L"VHS化", L"VHS化", L"VHS", L"VHS", L"VHS", L"VHS", L"VHS", L"VHS"},
        {L"进度", L"進度", L"Progress", L"Progression", L"Avanzamento", L"Fortschritt", L"Progresso", L"Progreso"},
        {L"添加附件（可多选，最多 32 个）", L"新增附件（可多選，最多 32 個）", L"Add attachments (up to 32)", L"Ajouter des pièces jointes (max. 32)", L"Aggiungi allegati (max 32)", L"Anhänge hinzufügen (max. 32)", L"Adicionar anexos (máx. 32)", L"Añadir adjuntos (máx. 32)"},
        {L"附件：", L"附件：", L"Attachments:", L"Pièces jointes :", L"Allegati:", L"Anhänge:", L"Anexos:", L"Adjuntos:"},
        {L"请先填写 API Key。", L"請先填寫 API Key。", L"Please enter an API Key first.", L"Veuillez d'abord saisir une clé API.", L"Inserisci prima una chiave API.", L"Bitte zuerst einen API-Schlüssel eingeben.", L"Insira primeiro uma chave API.", L"Introduce primero una clave API."},
        {L"Sidecar 启动超时", L"Sidecar 啟動逾時", L"Sidecar startup timed out", L"Délai de démarrage du sidecar dépassé", L"Timeout avvio sidecar", L"Sidecar-Start Zeitüberschreitung", L"Tempo de arranque do sidecar esgotado", L"Tiempo de inicio del sidecar agotado"},
        {L"[错误] ", L"[錯誤] ", L"[Error] ", L"[Erreur] ", L"[Errore] ", L"[Fehler] ", L"[Erro] ", L"[Error] "},
        {L"补帧倍数：", L"補幀倍數：", L"Interpolation factor: ", L"Facteur d'interpolation : ", L"Fattore di interpolazione: ", L"Interpolationsfaktor: ", L"Fator de interpolação: ", L"Factor de interpolación: "},
        {L"正在启动 GPT-SoVITS...", L"正在啟動 GPT-SoVITS...", L"Starting GPT-SoVITS...", L"Démarrage de GPT-SoVITS...", L"Avvio di GPT-SoVITS...", L"GPT-SoVITS wird gestartet...", L"A iniciar GPT-SoVITS...", L"Iniciando GPT-SoVITS..."},
        {L"正在引入主题...", L"正在套用主題...", L"Applying theme...", L"Application du thème...", L"Applicazione del tema...", L"Theme wird angewendet...", L"A aplicar tema...", L"Aplicando tema..."},
        {L"正在进入对话...", L"正在進入對話...", L"Entering chat...", L"Entrée dans la conversation...", L"Ingresso nella chat...", L"Chat wird geöffnet...", L"A entrar na conversa...", L"Entrando al chat..."},
        {L"下载 Derpibooru 图片中...", L"下載 Derpibooru 圖片中...", L"Downloading Derpibooru images...", L"Téléchargement des images Derpibooru...", L"Download immagini Derpibooru...", L"Derpibooru-Bilder werden heruntergeladen...", L"A transferir imagens Derpibooru...", L"Descargando imágenes de Derpibooru..."},
    };
    return table[static_cast<int>(key)][UiGroup()];
}

std::wstring KeyHintText() { return Ui(UiKey::KeyHint); }

HWND g_userLog = nullptr;
HWND g_assistantLog = nullptr;
HWND g_input = nullptr;
HWND g_send = nullptr;
HWND g_attach = nullptr;
HWND g_avatarUser = nullptr;
HWND g_avatarTwilight = nullptr;
HWND g_chip = nullptr;
HWND g_imageView = nullptr;
std::wstring g_imagePath;
std::wstring g_chipName;
HBITMAP g_avatarGreen = nullptr;
HBITMAP g_avatarBlue = nullptr;
HBITMAP g_avatarGray = nullptr;
HBITMAP g_avatarThink = nullptr;
Image* g_imgThink = nullptr;
Image* g_imgGreen = nullptr;
Image* g_imgBlue = nullptr;
Image* g_imgGray = nullptr;
Image* g_twilightImage = nullptr;
Image* g_imgUser = nullptr;
std::string g_attachmentText;
std::string g_attachmentImageUri;
std::vector<std::string> g_attachmentImageUris;
std::wstring g_attachmentPath;
std::vector<std::wstring> g_attachmentPaths;
std::wstring g_attachmentMeta;
HWND g_frameSlider = nullptr;
HWND g_frameLabel = nullptr;
HWND g_frameApply = nullptr;
bool g_framePanel = false;
HWND g_paramSlider = nullptr;
HWND g_paramLabel = nullptr;
HWND g_paramApply = nullptr;
bool g_paramPanel = false;
std::wstring g_paramCommand = L"/aging";
std::wstring g_paramTitle = std::wstring(Ui(UiKey::Aging)) + L" · 0.70";
HWND g_choiceVhs = nullptr;
HWND g_choiceDig = nullptr;
bool g_choicePanel = false;
std::wstring g_lastUserPrompt;
std::wstring g_pendingUserPrompt;
std::mutex g_speechMutex;
std::condition_variable g_speechCv;
std::deque<std::wstring> g_speechQueue;
bool g_speechStop = false;
std::thread g_speechThread;
size_t g_spokenOffset = 0;
std::wstring g_lastToolLabel;
std::wstring g_pendingAttachmentLabel;
HWND g_progressWindow = nullptr;
struct ProgressTask { std::wstring id; std::wstring label; std::wstring detail; std::wstring name; long long bytesDone = 0; long long bytesTotal = 0; int batchDone = 0; int batchTotal = 0; float fraction = 0.0f; bool done = false; };
std::vector<ProgressTask> g_progressTasks;
struct ProgressUpdate { std::wstring id; std::wstring label; std::wstring detail; std::wstring name; long long bytesDone = 0; long long bytesTotal = 0; int batchDone = 0; int batchTotal = 0; float fraction = 0.0f; bool done = false; };
HFONT g_titleFont = nullptr;
HFONT g_versionFont = nullptr;
HFONT g_uiFont = nullptr;
HFONT g_langLabelFont = nullptr;
bool g_chatMode = false;
bool g_richEdit = false;
bool g_firstTurn = true;
std::string g_assistantMarkdown;
std::string g_streamingAnswer;
bool g_loadingMode = false;
int g_loadingStep = 0;
constexpr UINT WM_APP_BOOT_OK = WM_APP + 11;
constexpr UINT WM_APP_BOOT_ERR = WM_APP + 12;
constexpr UINT WM_APP_TEST_SEND = WM_APP + 99;
constexpr UINT WM_APP_RECREATE_INPUT = WM_APP + 98;
std::string g_reasoningMarkdown;
constexpr UINT WM_APP_CHUNK = WM_APP + 1;
constexpr UINT WM_APP_DONE = WM_APP + 2;
constexpr UINT WM_APP_ERROR = WM_APP + 3;
constexpr UINT WM_APP_TOOL = WM_APP + 4;
constexpr UINT WM_APP_REASONING = WM_APP + 6;
constexpr UINT WM_APP_PROGRESS = WM_APP + 17;
constexpr UINT WM_APP_CHAT_TEST = WM_APP + 5;
std::vector<std::pair<std::string, std::string>> g_messages;
int ChipWidth();
std::wstring FileLabel(const std::wstring& path);

std::wstring ExeDir() {
    wchar_t path[MAX_PATH]{};
    GetModuleFileNameW(nullptr, path, MAX_PATH);
    std::wstring value(path);
    const auto slash = value.find_last_of(L"\\/");
    return slash == std::wstring::npos ? value : value.substr(0, slash);
}

std::wstring AssetPath(const wchar_t* name);
std::string ToUtf8(const std::wstring& value);

std::string AvatarMarker(const wchar_t* role, const wchar_t* file) {
    return "[[AVATAR:" + ToUtf8(std::wstring(role)) + "|" + ToUtf8(AssetPath(file)) + "]]";
}

std::wstring AssetPath(const wchar_t* name) {
    return ExeDir() + L"\\assets\\" + name;
}

int g_privateFonts = 0;

void RegisterBundledFonts() {
    const std::wstring serif = AssetPath(L"SourceHanSerifSC-Regular.otf");
    if (GetFileAttributesW(serif.c_str()) != INVALID_FILE_ATTRIBUTES) {
        g_privateFonts += AddFontResourceExW(serif.c_str(), FR_PRIVATE, nullptr);
    }
}

std::string ReadVariant() {
    std::ifstream in(ExeDir() + L"/variant.txt");
    std::string value;
    std::getline(in, value);
    while (!value.empty() && (value.back() == '\r' || value.back() == ' ')) value.pop_back();
    if (value == "plus" || value == "debug" || value == "standard" || value == "chat" || value == "benchmark") {
        return value;
    }
    return "plus";
}

std::wstring VariantVersion() {
    if (g_variant == "debug") return L"rc-261106-debug";
    if (g_variant == "standard") return L"rc-261106";
    if (g_variant == "chat") return L"rc-261106-chat";
    if (g_variant == "benchmark") return L"rc-261106-benchmark";
    return L"rc-261106-plus";
}

void LoadHero() {
    std::wstring heroPath = AssetPath(L"pony_hero_opaque.png");
    if (GetFileAttributesW(heroPath.c_str()) == INVALID_FILE_ATTRIBUTES) {
        heroPath = AssetPath(L"pony_hero.png");
    }
    if (GetFileAttributesW(heroPath.c_str()) != INVALID_FILE_ATTRIBUTES) {
        g_hero = Image::FromFile(heroPath.c_str());
    }
    const std::wstring panelPath = AssetPath(L"pony_panel.png");
    if (GetFileAttributesW(panelPath.c_str()) != INVALID_FILE_ATTRIBUTES) {
        g_panel = Image::FromFile(panelPath.c_str());
    }
}

void DrawRoundedFill(HDC dc, RECT rc, int radius, COLORREF color) {
    Graphics graphics(dc);
    graphics.SetSmoothingMode(SmoothingModeAntiAlias);
    SolidBrush brush(Color(255, GetRValue(color), GetGValue(color), GetBValue(color)));
    GraphicsPath path;
    const int d = radius * 2;
    path.AddArc(rc.left, rc.top, d, d, 180, 90);
    path.AddArc(rc.right - d, rc.top, d, d, 270, 90);
    path.AddArc(rc.right - d, rc.bottom - d, d, d, 0, 90);
    path.AddArc(rc.left, rc.bottom - d, d, d, 90, 90);
    path.CloseFigure();
    graphics.FillPath(&brush, &path);
}

void DrawRoundedStroke(HDC dc, RECT rc, int radius, COLORREF color, int width) {
    Graphics graphics(dc);
    graphics.SetSmoothingMode(SmoothingModeAntiAlias);
    Pen pen(Color(255, GetRValue(color), GetGValue(color), GetBValue(color)), static_cast<REAL>(width));
    GraphicsPath path;
    const int d = radius * 2;
    path.AddArc(rc.left, rc.top, d, d, 180, 90);
    path.AddArc(rc.right - d, rc.top, d, d, 270, 90);
    path.AddArc(rc.right - d, rc.bottom - d, d, d, 0, 90);
    path.AddArc(rc.left, rc.bottom - d, d, d, 90, 90);
    path.CloseFigure();
    graphics.DrawPath(&pen, &path);
}

void DrawHero(HDC dc, RECT client) {
    HBRUSH bg = CreateSolidBrush(RGB(61, 17, 79));
    RECT left{0, 0, kLeftWidth, client.bottom};
    FillRect(dc, &left, bg);
    DeleteObject(bg);
    Graphics graphics(dc);
    graphics.SetInterpolationMode(InterpolationModeHighQualityBicubic);
    graphics.SetClip(RectF(0, 0, static_cast<REAL>(kLeftWidth), static_cast<REAL>(client.bottom)));
    if (g_hero) {
        const REAL scale = min(static_cast<REAL>(kLeftWidth) / g_hero->GetWidth(),
                               static_cast<REAL>(client.bottom) / g_hero->GetHeight()) * 1.08f;
        const REAL w = g_hero->GetWidth() * scale;
        const REAL h = g_hero->GetHeight() * scale;
        graphics.DrawImage(g_hero, RectF((kLeftWidth - w) / 2.0f, client.bottom - h, w, h));
    }
}

void Layout(HWND hwnd) {
    RECT rc{};
    GetClientRect(hwnd, &rc);
    if (g_chatMode) {
        const int margin = 18;
        const int leftW = static_cast<int>((rc.right - margin * 3) * 0.34);
        const int rightX = margin * 2 + leftW;
        const int rightW = rc.right - rightX - margin;
        const int avatar = 64;
        const int logTop = margin;
        const int composerTop = rc.bottom - 180;
        const bool effectPanelVisible = g_framePanel || g_paramPanel || g_choicePanel;
        MoveWindow(g_avatarUser, margin, margin, avatar, avatar, TRUE);
        MoveWindow(g_avatarTwilight, rightX, margin, avatar, avatar, TRUE);
        MoveWindow(g_userLog, margin, logTop, leftW, rc.bottom - logTop - 18, TRUE);
        RECT userRect{10, 8, leftW - 10, rc.bottom - logTop - 26};
        SendMessageW(g_userLog, EM_SETRECT, 0, reinterpret_cast<LPARAM>(&userRect));
        const int rightTop = logTop;
        const int assistantBottom = effectPanelVisible ? composerTop - 68 : composerTop - 16;
        MoveWindow(g_assistantLog, rightX, rightTop, rightW, assistantBottom - rightTop, TRUE);
        const bool hasChip = IsWindowVisible(g_chip);
        MoveWindow(g_chip, rightX + 16, composerTop + 12, rightW - 32, 30, TRUE);
        MoveWindow(g_imageView, rightX + rightW - 240, composerTop - 168, 230, 148, TRUE);
        MoveWindow(g_input, rightX + 16, composerTop + (hasChip ? 50 : 16),
                   rightW - 112, hasChip ? 70 : 72, TRUE);
        RECT inputRect{14, hasChip ? 4 : 10, rightW - 126, hasChip ? 64 : 62};
        SendMessageW(g_input, EM_SETRECT, 0, reinterpret_cast<LPARAM>(&inputRect));
        MoveWindow(g_attach, rc.right - 96, composerTop + 128, 34, 34, TRUE);
        MoveWindow(g_send, rc.right - 56, composerTop + 128, 34, 34, TRUE);
        if (g_framePanel) {
            const int panelTop = composerTop - 56;
            MoveWindow(g_frameLabel, rightX + 16, panelTop + 4, 140, 26, TRUE);
            MoveWindow(g_frameSlider, rightX + 160, panelTop, rightW - 260, 36, TRUE);
            MoveWindow(g_frameApply, rightX + rightW - 92, panelTop, 76, 34, TRUE);
        }
        if (g_paramPanel) {
            const int panelTop = composerTop - 56;
            MoveWindow(g_paramLabel, rightX + 16, panelTop + 4, 170, 26, TRUE);
            MoveWindow(g_paramSlider, rightX + 190, panelTop, rightW - 290, 36, TRUE);
            MoveWindow(g_paramApply, rightX + rightW - 92, panelTop, 76, 34, TRUE);
        }
        if (g_choicePanel) {
            const int panelTop = composerTop - 56;
            MoveWindow(g_choiceVhs, rightX + 16, panelTop + 4, 88, 26, TRUE);
            MoveWindow(g_choiceDig, rightX + 108, panelTop + 4, 76, 26, TRUE);
            MoveWindow(g_paramLabel, rightX + 190, panelTop - 18, 170, 22, TRUE);
            MoveWindow(g_paramSlider, rightX + 190, panelTop, rightW - 290, 36, TRUE);
            MoveWindow(g_paramApply, rightX + rightW - 92, panelTop, 76, 34, TRUE);
        }
        if (effectPanelVisible) {
            HWND controls[] = {g_frameLabel, g_frameSlider, g_frameApply,
                               g_paramLabel, g_paramSlider, g_paramApply,
                               g_choiceVhs, g_choiceDig};
            for (HWND control : controls) {
                if (!control || !IsWindowVisible(control)) continue;
                EnableWindow(control, TRUE);
                SetWindowPos(control, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            }
        }
        return;
    }
    const UiMetrics m = Metrics(hwnd);
    if (g_arrow) MoveWindow(g_arrow, rc.right - 88, 24, 48, 48, TRUE);
    if (g_key) {
        MoveWindow(g_key, m.key.left + 2, m.key.top + 2, m.key.right - m.key.left - 4, m.key.bottom - m.key.top - 4, TRUE);
        SendMessageW(g_key, EM_SETMARGINS, EC_LEFTMARGIN | EC_RIGHTMARGIN, MAKELPARAM(14, 14));
    }
    if (g_langLabel) MoveWindow(g_langLabel, m.label.left, m.label.top, m.label.right - m.label.left, m.label.bottom - m.label.top, TRUE);
    if (g_langButton) MoveWindow(g_langButton, m.combo.left, m.combo.top, m.combo.right - m.combo.left, m.combo.bottom - m.combo.top, TRUE);
}

HBITMAP LoadPng(const std::wstring& path, int width, int height) {
    Bitmap source(path.c_str());
    if (source.GetLastStatus() != Ok) return nullptr;
    Bitmap scaled(width, height, PixelFormat32bppPARGB);
    Graphics graphics(&scaled);
    graphics.SetInterpolationMode(InterpolationModeHighQualityBicubic);
    graphics.DrawImage(&source, 0, 0, width, height);
    HBITMAP bitmap = nullptr;
    scaled.GetHBITMAP(Color(0, 0, 0), &bitmap);
    return bitmap;
}

WNDPROC g_inputOldProc = nullptr;

LRESULT CALLBACK InputWindowProc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
    if ((message == WM_KEYDOWN || message == WM_SYSKEYDOWN || message == WM_IME_KEYDOWN) &&
        wparam == VK_RETURN) {
        if ((GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0) {
            PostMessageW(GetParent(hwnd), WM_COMMAND, MAKEWPARAM(1013, BN_CLICKED), 0);
            return 0;
        }
        if ((GetAsyncKeyState(VK_MENU) & 0x8000) != 0) {
            PostMessageW(GetParent(hwnd), WM_COMMAND, MAKEWPARAM(1014, BN_CLICKED), 0);
            return 0;
        }
    }
    if (message == WM_CHAR && wparam == VK_RETURN &&
        ((GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0 || (GetAsyncKeyState(VK_MENU) & 0x8000) != 0)) {
        return 0;
    }
    return g_inputOldProc ? CallWindowProcW(g_inputOldProc, hwnd, message, wparam, lparam)
                          : DefWindowProcW(hwnd, message, wparam, lparam);
}

LRESULT CALLBACK InputSubclass(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam,
                              UINT_PTR id, DWORD_PTR ref) {
    if (message == WM_KEYDOWN && wparam == VK_RETURN) {
        if ((GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0) {
            PostMessageW(GetParent(hwnd), WM_COMMAND, MAKEWPARAM(1013, BN_CLICKED), 0);
            return 0;
        }
        if ((GetAsyncKeyState(VK_MENU) & 0x8000) != 0) {
            PostMessageW(GetParent(hwnd), WM_COMMAND, MAKEWPARAM(1014, BN_CLICKED), 0);
            return 0;
        }
    }
    if (message == WM_CHAR && wparam == VK_RETURN &&
        ((GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0 || (GetAsyncKeyState(VK_MENU) & 0x8000) != 0)) {
        return 0;
    }
    return DefSubclassProc(hwnd, message, wparam, lparam);
}

void SetTwilightAvatar(HWND hwnd, HBITMAP bitmap) {
    if (!hwnd || !bitmap) return;
    SendMessageW(hwnd, STM_SETIMAGE, IMAGE_BITMAP, reinterpret_cast<LPARAM>(bitmap));
}

void CreateChatInput(HWND hwnd) {
    g_input = CreateWindowExW(0, L"EDIT", L"",
                              WS_CHILD | WS_VISIBLE | ES_MULTILINE | ES_AUTOVSCROLL,
                              0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1012), g_instance, nullptr);
    g_inputOldProc = reinterpret_cast<WNDPROC>(SetWindowLongPtrW(
        g_input, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(InputWindowProc)));
    SendMessageW(g_input, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    SendMessageW(g_input, EM_SETMARGINS, EC_LEFTMARGIN | EC_RIGHTMARGIN, MAKELPARAM(16, 16));
    SendMessageW(g_input, EM_SETLIMITTEXT, 8000, 0);
}

std::wstring UiFontFamily(bool serif) {
    if (g_language.rfind(L"简体中文", 0) == 0 || g_language.rfind(L"繁體中文", 0) == 0) {
        return serif ? L"思源宋体" : L"Source Han Sans";
    }
    return L"Uni Neue";
}

void CreateControls(HWND hwnd) {
    g_titleFont = CreateFontW(-34, 0, 0, 0, FW_SEMIBOLD, FALSE, FALSE, FALSE,
                              DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                              CLEARTYPE_QUALITY, DEFAULT_PITCH, UiFontFamily(true).c_str());
    g_versionFont = CreateFontW(-17, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Consolas");
    g_uiFont = CreateFontW(-19, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                           DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                           CLEARTYPE_QUALITY, DEFAULT_PITCH, UiFontFamily(false).c_str());
    g_langLabelFont = CreateFontW(-19, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                  DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                  CLEARTYPE_QUALITY, DEFAULT_PITCH, UiFontFamily(true).c_str());

    g_arrow = CreateWindowExW(0, L"BUTTON", L"\u203A", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                              0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1001), g_instance, nullptr);
    g_key = CreateWindowExW(0, L"EDIT", L"", WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL,
                            0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1002), g_instance, nullptr);
    SendMessageW(g_key, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    SetWindowTextW(g_key, KeyHintText().c_str());
    g_langLabel = CreateWindowExW(0, L"STATIC", L"\u8BED\u8A00\uFF1A", WS_CHILD | WS_VISIBLE | SS_CENTERIMAGE,
                                  0, 0, 0, 0, hwnd, nullptr, g_instance, nullptr);
    if (g_langLabelFont) {
        SendMessageW(g_langLabel, WM_SETFONT, reinterpret_cast<WPARAM>(g_langLabelFont), TRUE);
    }
    g_langButton = CreateWindowExW(0, L"BUTTON", L"", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                                   0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1003), g_instance, nullptr);

    LoadLibraryW(L"Msftedit.dll");
    const DWORD logStyle = WS_CHILD | ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL;
    g_userLog = CreateChatView(hwnd, 1010);
    g_assistantLog = CreateChatView(hwnd, 1011);
    SetChatViewBackground(g_userLog, RGB(233, 214, 255));
    SetChatViewBackground(g_assistantLog, kRightBg);
    g_richEdit = true;
    ShowWindow(g_userLog, SW_HIDE);
    ShowWindow(g_assistantLog, SW_HIDE);
    g_input = CreateWindowExW(0, L"EDIT", L"",
                              WS_CHILD | WS_VISIBLE | ES_MULTILINE | ES_AUTOVSCROLL,
                              0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1012), g_instance, nullptr);
    g_inputOldProc = reinterpret_cast<WNDPROC>(SetWindowLongPtrW(
        g_input, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(InputWindowProc)));
    g_send = CreateWindowExW(0, L"BUTTON", L">", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                             0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1013), g_instance, nullptr);
    g_attach = CreateWindowExW(0, L"BUTTON", L"+", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                               0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1014), g_instance, nullptr);
    SendMessageW(g_input, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    SendMessageW(g_send, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    SendMessageW(g_attach, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    g_avatarUser = CreateWindowExW(0, L"STATIC", L"", WS_CHILD | SS_BITMAP | SS_REALSIZECONTROL,
                                   0, 0, 0, 0, hwnd, nullptr, g_instance, nullptr);
    g_avatarTwilight = CreateWindowExW(0, L"STATIC", L"", WS_CHILD | SS_BITMAP | SS_REALSIZECONTROL,
                                       0, 0, 0, 0, hwnd, nullptr, g_instance, nullptr);
    SendMessageW(g_avatarUser, STM_SETIMAGE, IMAGE_BITMAP,
                 reinterpret_cast<LPARAM>(LoadPng(AssetPath(L"avatar_user.png"), 56, 56)));
    g_avatarGreen = LoadPng(AssetPath(L"avatar_green.png"), 72, 72);
    g_avatarBlue = LoadPng(AssetPath(L"avatar_blue.png"), 72, 72);
    g_avatarGray = LoadPng(AssetPath(L"avatar_gray.png"), 72, 72);
    g_avatarThink = LoadPng(AssetPath(L"avatar_think.png"), 72, 72);
    g_imgThink = Image::FromFile(AssetPath(L"avatar_think.png").c_str());
    g_imgGreen = Image::FromFile(AssetPath(L"avatar_green.png").c_str());
    g_imgBlue = Image::FromFile(AssetPath(L"avatar_blue.png").c_str());
    g_imgGray = Image::FromFile(AssetPath(L"avatar_gray.png").c_str());
    g_twilightImage = g_imgGreen;
    g_imgUser = Image::FromFile(AssetPath(L"avatar_user.png").c_str());
    SendMessageW(g_avatarUser, STM_SETIMAGE, IMAGE_BITMAP,
                 reinterpret_cast<LPARAM>(LoadPng(AssetPath(L"avatar_user.png"), 72, 72)));
    SendMessageW(g_avatarTwilight, STM_SETIMAGE, IMAGE_BITMAP,
                 reinterpret_cast<LPARAM>(g_avatarGreen));
    g_chip = CreateWindowExW(0, L"BUTTON", L"", WS_CHILD | BS_OWNERDRAW,
                             0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1015), g_instance, nullptr);
    g_imageView = CreateWindowExW(0, L"STATIC", L"", WS_CHILD | SS_BITMAP | SS_REALSIZECONTROL | SS_NOTIFY,
                                  0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1016), g_instance, nullptr);
    g_frameLabel = CreateWindowExW(0, L"STATIC", L"", WS_CHILD,
                                   0, 0, 0, 0, hwnd, nullptr, g_instance, nullptr);
    g_frameSlider = CreateWindowExW(0, TRACKBAR_CLASSW, L"", WS_CHILD | TBS_HORZ | TBS_AUTOTICKS,
                                    0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1017), g_instance, nullptr);
    SendMessageW(g_frameSlider, TBM_SETRANGE, TRUE, MAKELPARAM(2, 30));
    SendMessageW(g_frameSlider, TBM_SETTICFREQ, 1, 0);
    SendMessageW(g_frameSlider, TBM_SETPOS, TRUE, 2);
    g_frameApply = CreateWindowExW(0, L"BUTTON", L"", WS_CHILD | BS_PUSHBUTTON,
                                   0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1019), g_instance, nullptr);
    SendMessageW(g_frameLabel, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    SendMessageW(g_frameApply, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    g_paramLabel = CreateWindowExW(0, L"STATIC", L"age · 包浆：0.70", WS_CHILD,
                                   0, 0, 0, 0, hwnd, nullptr, g_instance, nullptr);
    g_paramSlider = CreateWindowExW(0, TRACKBAR_CLASSW, L"", WS_CHILD | TBS_HORZ | TBS_AUTOTICKS,
                                    0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1020), g_instance, nullptr);
    SendMessageW(g_paramSlider, TBM_SETRANGE, TRUE, MAKELPARAM(0, 100));
    SendMessageW(g_paramSlider, TBM_SETTICFREQ, 10, 0);
    SendMessageW(g_paramSlider, TBM_SETPOS, TRUE, 70);
    g_paramApply = CreateWindowExW(0, L"BUTTON", L"", WS_CHILD | BS_PUSHBUTTON,
                                   0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1021), g_instance, nullptr);
    g_choiceVhs = CreateWindowExW(0, L"BUTTON", L"", WS_CHILD | BS_AUTORADIOBUTTON | WS_GROUP,
                                  0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1022), g_instance, nullptr);
    g_choiceDig = CreateWindowExW(0, L"BUTTON", L"", WS_CHILD | BS_AUTORADIOBUTTON,
                                  0, 0, 0, 0, hwnd, reinterpret_cast<HMENU>(1023), g_instance, nullptr);
    SendMessageW(g_choiceVhs, BM_SETCHECK, BST_CHECKED, 0);
    SendMessageW(g_paramLabel, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    SendMessageW(g_paramApply, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    SendMessageW(g_choiceVhs, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    SendMessageW(g_choiceDig, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    SetWindowTextW(g_langLabel, Ui(UiKey::Language));
    SetWindowTextW(g_frameLabel, (std::wstring(Ui(UiKey::FrameMultiplier)) + L"2×").c_str());
    SetWindowTextW(g_frameApply, Ui(UiKey::Apply));
    SetWindowTextW(g_paramApply, Ui(UiKey::Apply));
    SetWindowTextW(g_choiceVhs, Ui(UiKey::Vhs));
    SetWindowTextW(g_choiceDig, Ui(UiKey::Squeeze));
    ShowWindow(g_choiceVhs, SW_HIDE);
    ShowWindow(g_choiceDig, SW_HIDE);
    ShowWindow(g_paramLabel, SW_HIDE);
    ShowWindow(g_paramSlider, SW_HIDE);
    ShowWindow(g_paramApply, SW_HIDE);
    ShowWindow(g_frameLabel, SW_HIDE);
    ShowWindow(g_frameSlider, SW_HIDE);
    ShowWindow(g_frameApply, SW_HIDE);
    ShowWindow(g_avatarUser, SW_HIDE);
    ShowWindow(g_avatarTwilight, SW_HIDE);
    ShowWindow(g_chip, SW_HIDE);
    ShowWindow(g_imageView, SW_HIDE);
    SetFocus(g_arrow);
    SetChatViewFonts(g_userLog, UiFontFamily(false), UiFontFamily(true));
    SetChatViewFonts(g_assistantLog, UiFontFamily(false), UiFontFamily(true));
    ShowWindow(g_attach, SW_HIDE);
    ShowWindow(g_userLog, SW_HIDE);
    ShowWindow(g_assistantLog, SW_HIDE);
    ShowWindow(g_input, SW_HIDE);
    ShowWindow(g_send, SW_HIDE);
}

void ApplyLanguage(HWND hwnd, const std::wstring& oldHint) {
    if (g_titleFont) DeleteObject(g_titleFont);
    if (g_uiFont) DeleteObject(g_uiFont);
    if (g_langLabelFont) DeleteObject(g_langLabelFont);
    g_titleFont = CreateFontW(-34, 0, 0, 0, FW_SEMIBOLD, FALSE, FALSE, FALSE,
                              DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                              CLEARTYPE_QUALITY, DEFAULT_PITCH, UiFontFamily(true).c_str());
    g_uiFont = CreateFontW(-19, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                            DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                            CLEARTYPE_QUALITY, DEFAULT_PITCH, UiFontFamily(false).c_str());
    g_langLabelFont = CreateFontW(-19, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                   DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                   CLEARTYPE_QUALITY, DEFAULT_PITCH, UiFontFamily(true).c_str());
    HWND controls[] = {g_key, g_input, g_send, g_attach, g_langLabel, g_frameLabel,
                       g_frameApply, g_paramLabel, g_paramApply, g_choiceVhs, g_choiceDig};
    for (HWND control : controls) {
        if (control) SendMessageW(control, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
    }
    if (g_langLabel && g_langLabelFont) {
        SendMessageW(g_langLabel, WM_SETFONT, reinterpret_cast<WPARAM>(g_langLabelFont), TRUE);
    }
    wchar_t current[512]{};
    if (g_key) GetWindowTextW(g_key, current, 512);
    if (g_key && (current[0] == 0 || oldHint == current)) {
        SetWindowTextW(g_key, KeyHintText().c_str());
    }
    if (g_langLabel) SetWindowTextW(g_langLabel, Ui(UiKey::Language));
    if (g_frameApply) SetWindowTextW(g_frameApply, Ui(UiKey::Apply));
    if (g_paramApply) SetWindowTextW(g_paramApply, Ui(UiKey::Apply));
    if (g_choiceVhs) SetWindowTextW(g_choiceVhs, Ui(UiKey::Vhs));
    if (g_choiceDig) SetWindowTextW(g_choiceDig, Ui(UiKey::Squeeze));
    if (g_frameLabel && g_frameSlider) {
        const int factor = static_cast<int>(SendMessageW(g_frameSlider, TBM_GETPOS, 0, 0));
        SetWindowTextW(g_frameLabel, (std::wstring(Ui(UiKey::FrameMultiplier)) + std::to_wstring(factor) + L"×").c_str());
    }
    if (g_progressWindow) SetWindowTextW(g_progressWindow, Ui(UiKey::Progress));
    SetChatViewFonts(g_userLog, UiFontFamily(false), UiFontFamily(true));
    SetChatViewFonts(g_assistantLog, UiFontFamily(false), UiFontFamily(true));
    InvalidateRect(hwnd, nullptr, TRUE);
    if (g_langButton) InvalidateRect(g_langButton, nullptr, TRUE);
}

void Paint(HWND hwnd) {
    PAINTSTRUCT ps{};
    HDC dc = BeginPaint(hwnd, &ps);
    RECT rc{};
    GetClientRect(hwnd, &rc);

    if (g_loadingMode) {
        HBRUSH bg = CreateSolidBrush(kRightBg);
        FillRect(dc, &rc, bg);
        DeleteObject(bg);
        DrawHero(dc, rc);
        SetBkMode(dc, TRANSPARENT);
        SetTextColor(dc, kInk);
        SelectObject(dc, g_uiFont);
        const int barX = kLeftWidth + 40;
        const int barW = rc.right - barX - 40;
        const wchar_t* labels[3] = {Ui(UiKey::StartVoice), Ui(UiKey::StartTheme), Ui(UiKey::StartChat)};
        for (int i = 0; i < 3; ++i) {
            const int y = 120 + i * 100;
            RECT label{barX, y, barX + barW, y + 26};
            DrawTextW(dc, labels[i], -1, &label, DT_LEFT | DT_SINGLELINE | DT_VCENTER);
            RECT track{barX, y + 32, barX + barW, y + 52};
            HBRUSH trackBrush = CreateSolidBrush(RGB(255, 255, 255));
            FillRect(dc, &track, trackBrush);
            DeleteObject(trackBrush);
            const int filled = i < g_loadingStep ? barW : (i == g_loadingStep ? barW / 2 : 0);
            if (filled > 0) {
                RECT fill{barX, y + 32, barX + filled, y + 52};
                HBRUSH fillBrush = CreateSolidBrush(kPurple);
                FillRect(dc, &fill, fillBrush);
                DeleteObject(fillBrush);
            }
            FrameRect(dc, &track, reinterpret_cast<HBRUSH>(GetStockObject(BLACK_BRUSH)));
        }
        EndPaint(hwnd, &ps);
        return;
    }

    if (g_chatMode) {
        HBRUSH bg = CreateSolidBrush(kRightBg);
        FillRect(dc, &rc, bg);
        DeleteObject(bg);
        const int margin = 18;
        const int leftW = static_cast<int>((rc.right - margin * 3) * 0.34);
        const int rightX = margin * 2 + leftW;
        const int rightW = rc.right - rightX - margin;
        const int composerTop = rc.bottom - 180;
        HBRUSH thinkBg = CreateSolidBrush(RGB(233, 214, 255));
        RECT think{0, 0, rightX - margin, rc.bottom};
        FillRect(dc, &think, thinkBg);
        DeleteObject(thinkBg);
        RECT card{rightX, composerTop, rightX + rightW, rc.bottom - 18};
        DrawRoundedFill(dc, card, 22, RGB(255, 255, 255));
        EndPaint(hwnd, &ps);
        return;
    }
    HBRUSH background = CreateSolidBrush(kRightBg);
    RECT right{kLeftWidth, 0, rc.right, rc.bottom};
    FillRect(dc, &right, background);
    DeleteObject(background);
    DrawHero(dc, rc);

    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, RGB(239, 224, 255));
    SelectObject(dc, g_versionFont);
    RECT version{22, 20, 340, 48};
    DrawTextW(dc, VariantVersion().c_str(), -1, &version, DT_LEFT | DT_SINGLELINE | DT_VCENTER);

    SetTextColor(dc, kInk);
    SelectObject(dc, g_uiFont);
    RECT hint{kLeftWidth + 44, rc.bottom - 200, rc.right - 44, rc.bottom - 172};
    DrawTextW(dc, L"", -1, &hint, DT_LEFT | DT_SINGLELINE);

    // White field backgrounds exactly matching the native control rectangles.
    const UiMetrics m = Metrics(hwnd);
    DrawRoundedFill(dc, m.key, 12, RGB(255, 255, 255));
    DrawRoundedStroke(dc, m.key, 12, RGB(220, 204, 235), 1);
    DrawRoundedFill(dc, m.combo, 12, RGB(255, 255, 255));

    EndPaint(hwnd, &ps);
}

void DrawArrow(const DRAWITEMSTRUCT* item) {
    HDC dc = item->hDC;
    RECT rc = item->rcItem;
    HBRUSH bg = CreateSolidBrush(kRightBg);
    FillRect(dc, &rc, bg);
    DeleteObject(bg);
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, kPurple);
    HFONT font = CreateFontW(-30, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                             DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                             CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI Symbol");
    HGDIOBJ old = SelectObject(dc, font);
    DrawTextW(dc, L"\u203A", -1, &rc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    SelectObject(dc, old);
    DeleteObject(font);
}

std::string ToUtf8(const std::wstring& value) {
    if (value.empty()) return {};
    const int size = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                                         nullptr, 0, nullptr, nullptr);
    std::string out(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                        out.data(), size, nullptr, nullptr);
    return out;
}

std::wstring ToWide(const std::string& value) {
    if (value.empty()) return {};
    const int size = MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                                         nullptr, 0);
    std::wstring out(size, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                        out.data(), size);
    return out;
}

void AppendText(HWND edit, const std::wstring& text) {
    if (!edit) return;
    const int length = GetWindowTextLengthW(edit);
    SendMessageW(edit, EM_SETSEL, length, length);
    SendMessageW(edit, EM_REPLACESEL, FALSE, reinterpret_cast<LPARAM>(text.c_str()));
    SendMessageW(edit, EM_REPLACESEL, FALSE, reinterpret_cast<LPARAM>(L"\r\n"));
    SendMessageW(edit, EM_SCROLLCARET, 0, 0);
}

void AppendRaw(HWND edit, const std::wstring& text) {
    if (!edit) return;
    const int length = GetWindowTextLengthW(edit);
    SendMessageW(edit, EM_SETSEL, length, length);
    SendMessageW(edit, EM_REPLACESEL, FALSE, reinterpret_cast<LPARAM>(text.c_str()));
    SendMessageW(edit, EM_SCROLLCARET, 0, 0);
}

DWORD CALLBACK RtfStreamIn(DWORD_PTR cookie, LPBYTE buffer, LONG size, LONG* written) {
    auto* data = reinterpret_cast<std::string*>(cookie);
    const size_t left = data->size();
    const size_t take = left < static_cast<size_t>(size) ? left : static_cast<size_t>(size);
    if (take > 0) memcpy(buffer, data->data(), take);
    data->erase(0, take);
    *written = static_cast<LONG>(take);
    return 0;
}

void SetRichText(HWND edit, const std::string& rtf) {
    if (!edit) return;
    std::string data = rtf;
    EDITSTREAM stream{};
    stream.dwCookie = reinterpret_cast<DWORD_PTR>(&data);
    stream.pfnCallback = RtfStreamIn;
    SendMessageW(edit, EM_SETSEL, 0, -1);
    SendMessageW(edit, EM_STREAMIN, SF_RTF | SFF_SELECTION, reinterpret_cast<LPARAM>(&stream));
}

int RichWidthTwips(HWND edit) {
    RECT rc{};
    if (edit) GetClientRect(edit, &rc);
    const int pixels = rc.right > 0 ? rc.right : 500;
    return (pixels - 24) * 1440 / 96;
}

void RenderReasoning() {
    if (!g_userLog) return;
    if (g_reasoningMarkdown.empty()) {
        SetChatViewMarkdown(g_userLog, "");
    } else {
        SetChatViewMarkdown(g_userLog, AvatarMarker(L"think", L"avatar_think.png") + "\n" + g_reasoningMarkdown);
    }
}

void ShowLastImage() {
    if (g_imageView) ShowWindow(g_imageView, SW_HIDE);
    return;
    const std::string combined = g_assistantMarkdown + "\n" + g_reasoningMarkdown;
    const auto close = combined.rfind("](");
    const auto open = combined.rfind("![", close == std::string::npos ? 0 : close);
    if (close == std::string::npos || open == std::string::npos) {
        ShowWindow(g_imageView, SW_HIDE);
        Layout(GetParent(g_imageView));
        return;
    }
    const auto end = combined.find(')', close + 2);
    if (end == std::string::npos) {
        ShowWindow(g_imageView, SW_HIDE);
        Layout(GetParent(g_imageView));
        return;
    }
    const std::string path = combined.substr(close + 2, end - close - 2);
    g_imagePath = ToWide(path);
    HBITMAP bitmap = LoadPng(g_imagePath, 236, 150);
    if (!bitmap) {
        ShowWindow(g_imageView, SW_HIDE);
        Layout(GetParent(g_imageView));
        return;
    }
    SendMessageW(g_imageView, STM_SETIMAGE, IMAGE_BITMAP, reinterpret_cast<LPARAM>(bitmap));
    ShowWindow(g_imageView, SW_SHOW);
    Layout(GetParent(g_imageView));
}

void RenderAssistant() {
    if (g_assistantMarkdown.empty()) return;
    SetChatViewMarkdown(g_assistantLog, g_assistantMarkdown);
    ShowLastImage();
}

std::string JsonEscape(const std::string& value) {
    std::string out;
    for (char ch : value) {
        switch (ch) {
        case '"': out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        default: out += ch; break;
        }
    }
    return out;
}

std::string UnescapeJson(const std::string& value) {
    std::string out;
    for (size_t i = 0; i < value.size(); ++i) {
        if (value[i] == '\\' && i + 1 < value.size()) {
            const char n = value[++i];
            if (n == 'n') out += '\n';
            else if (n == 'r') out += '\r';
            else if (n == 't') out += '\t';
            else out += n;
        } else {
            out += value[i];
        }
    }
    return out;
}

std::string JsonField(const std::string& raw, const std::string& field) {
    const std::string needle = "\"" + field + "\"";
    auto pos = raw.find(needle);
    if (pos == std::string::npos) return {};
    pos = raw.find(':', pos + needle.size());
    if (pos == std::string::npos) return {};
    pos = raw.find_first_not_of(" \t\r\n", pos + 1);
    if (pos == std::string::npos) return {};
    if (raw[pos] != '"') {
        std::string value;
        while (pos < raw.size() && raw[pos] != ',' && raw[pos] != '}' && raw[pos] != ']') value += raw[pos++];
        while (!value.empty() && (value.back() == ' ' || value.back() == '\t')) value.pop_back();
        return value;
    }
    std::string value;
    for (++pos; pos < raw.size(); ++pos) {
        if (raw[pos] == '"') break;
        if (raw[pos] == '\\' && pos + 1 < raw.size()) {
            const char escaped = raw[++pos];
            if (escaped == 'n') value += '\n';
            else if (escaped == 'r') value += '\r';
            else if (escaped == 't') value += '\t';
            else if (escaped == 'b') value += '\b';
            else if (escaped == 'f') value += '\f';
            else value += escaped;
        } else {
            value += raw[pos];
        }
    }
    return value;
}

std::vector<std::string> JsonStringArray(const std::string& raw, const std::string& field) {
    const std::string needle = "\"" + field + "\"";
    auto pos = raw.find(needle);
    if (pos == std::string::npos) return {};
    pos = raw.find(':', pos + needle.size());
    if (pos == std::string::npos) return {};
    pos = raw.find('[', pos);
    if (pos == std::string::npos) return {};
    std::vector<std::string> values;
    ++pos;
    while (pos < raw.size()) {
        while (pos < raw.size() && (raw[pos] == ' ' || raw[pos] == '\t' || raw[pos] == '\r' || raw[pos] == '\n' || raw[pos] == ',')) ++pos;
        if (pos >= raw.size() || raw[pos] == ']') break;
        if (raw[pos] != '"') break;
        std::string value;
        for (++pos; pos < raw.size(); ++pos) {
            if (raw[pos] == '"') { ++pos; break; }
            if (raw[pos] == '\\' && pos + 1 < raw.size()) {
                const char escaped = raw[++pos];
                if (escaped == 'n') value += '\n';
                else if (escaped == 'r') value += '\r';
                else if (escaped == 't') value += '\t';
                else value += escaped;
            } else {
                value += raw[pos];
            }
        }
        values.push_back(std::move(value));
    }
    return values;
}

std::once_flag g_sidecarOnce;
std::atomic<bool> g_sidecarStarting{false};
HWND g_mainHwnd = nullptr;
HHOOK g_keyboardHook = nullptr;
HIMC g_detachedImc = nullptr;
bool g_hotkeyDown = false;

void StartSidecarAsync() {
    std::call_once(g_sidecarOnce, []() {
        std::thread([]() {
            const std::wstring root = ExeDir();
            const std::wstring projectRuntime = root + L"/../../runtime/portable";
            std::wstring portableAi = root + L"/ai";
            if (GetFileAttributesW(portableAi.c_str()) == INVALID_FILE_ATTRIBUTES) {
                portableAi = projectRuntime + L"/ai";
            }
            if (GetFileAttributesW(portableAi.c_str()) == INVALID_FILE_ATTRIBUTES) {
                portableAi = root + L"/../../../_ai_install/TwilightHarnessPortable/ai";
            }
            if (GetFileAttributesW(portableAi.c_str()) != INVALID_FILE_ATTRIBUTES) {
                SetEnvironmentVariableW(L"TWILIGHT_AI_HOME", portableAi.c_str());
                const std::wstring ca = portableAi + L"/../python/Lib/site-packages/certifi/cacert.pem";
                if (GetFileAttributesW(ca.c_str()) != INVALID_FILE_ATTRIBUTES) {
                    SetEnvironmentVariableW(L"TWILIGHT_CA_BUNDLE", ca.c_str());
                }
            }
            std::wstring python = root + L"/python/Scripts/python.exe";
            std::wstring script = root + L"/sidecar/server.py";
            wchar_t env[1024]{};
            if (GetEnvironmentVariableW(L"TWILIGHT_SIDECAR_PYTHON", env, 1024) > 0) python = env;
            if (GetEnvironmentVariableW(L"TWILIGHT_SIDECAR_SCRIPT", env, 1024) > 0) script = env;
            if (GetFileAttributesW(python.c_str()) == INVALID_FILE_ATTRIBUTES) {
                const std::wstring pys[] = {
                    projectRuntime + L"/python/Scripts/python.exe",
                    root + L"/../../.venv-twilight/Scripts/python.exe",
                    root + L"/../../../_ai_install/TwilightHarnessPortable/python/Scripts/python.exe",
                    L"H:/Github/_ai_install/TwilightHarnessPortable/python/Scripts/python.exe",
                };
                for (const auto& candidate : pys) {
                    if (GetFileAttributesW(candidate.c_str()) != INVALID_FILE_ATTRIBUTES) { python = candidate; break; }
                }
            }
            if (GetFileAttributesW(script.c_str()) == INVALID_FILE_ATTRIBUTES) {
                const std::wstring scripts[] = {
                    root + L"/../../sidecar/server.py",
                    root + L"/../../../sidecar/server.py",
                };
                for (const auto& candidate : scripts) {
                    if (GetFileAttributesW(candidate.c_str()) != INVALID_FILE_ATTRIBUTES) { script = candidate; break; }
                }
            }
            g_sidecarStarting = true;
            g_sidecar.Start(python, script, root, root + L"/sidecar/handshake.json");
            g_sidecarStarting = false;
        }).detach();
    });
}

std::wstring SpeechText(const std::wstring& input) {
    std::wstring source = input;
    for (;;) {
        const size_t begin = source.find(L"[[");
        if (begin == std::wstring::npos) break;
        const size_t end = source.find(L"]]", begin + 2);
        if (end == std::wstring::npos) { source.erase(begin); break; }
        source.erase(begin, end + 2 - begin);
    }
    std::wstring output;
    wchar_t closing = 0;
    for (wchar_t ch : source) {
        if (closing != 0) {
            if (ch == closing) closing = 0;
            continue;
        }
        if (ch == L'（' || ch == L'(') {
            closing = ch == L'（' ? L'）' : L')';
            continue;
        }
        if (ch == L'*' || ch == L'`' || ch == L'#' || ch == L'[' || ch == L']') continue;
        output += ch;
    }
    std::wstring cleaned;
    size_t start = 0;
    while (start <= output.size()) {
        const size_t newline = output.find(L'\n', start);
        std::wstring line = output.substr(start, newline == std::wstring::npos ? std::wstring::npos : newline - start);
        const size_t first = line.find_first_not_of(L" \t\r");
        if (first == std::wstring::npos || line[first] != L'/') {
            cleaned += line;
            if (newline != std::wstring::npos) cleaned += L'\n';
        }
        if (newline == std::wstring::npos) break;
        start = newline + 1;
    }
    std::wstring trimmed;
    bool space = false;
    for (wchar_t ch : cleaned) {
        if (ch == L' ' || ch == L'\t' || ch == L'\r' || ch == L'\n') {
            space = true;
            continue;
        }
        if (space && !trimmed.empty()) trimmed += L' ';
        space = false;
        trimmed += ch;
    }
    return trimmed;
}

void SpeechWorker() {
    for (;;) {
        std::wstring text;
        {
            std::unique_lock<std::mutex> lock(g_speechMutex);
            g_speechCv.wait(lock, [] { return g_speechStop || !g_speechQueue.empty(); });
            if (g_speechStop && g_speechQueue.empty()) break;
            text = std::move(g_speechQueue.front());
            g_speechQueue.pop_front();
        }
        if (text.empty()) continue;
        const std::string body = "{\"text\":\"" + JsonEscape(ToUtf8(text)) +
                                 "\",\"language\":\"" + JsonEscape(ToUtf8(g_language)) + "\"}";
        std::string response;
        std::string error;
        if (!g_sidecar.Post("/tts", body, response, error)) continue;
        const std::string path = JsonField(response, "path");
        if (!path.empty()) PlaySoundW(ToWide(path).c_str(), nullptr, SND_FILENAME);
    }
}

void PushSpeech(const std::wstring& text) {
    if (text.empty()) return;
    {
        std::lock_guard<std::mutex> lock(g_speechMutex);
        g_speechQueue.push_back(text);
    }
    g_speechCv.notify_one();
}

void SpeakAsync(const std::wstring& answer) {
    PushSpeech(SpeechText(answer));
}

void SpeakStreamingSentences(bool final) {
    // Speak only the completed answer.  Streaming fragments caused repeated
    // command words and audible stuttering when chunks split mid-sentence.
    if (!final || g_streamingAnswer.empty()) return;
    const std::wstring text = ToWide(g_streamingAnswer);
    if (g_spokenOffset >= text.size()) return;
    PushSpeech(SpeechText(text.substr(g_spokenOffset)));
    g_spokenOffset = text.size();
}
std::wstring FileLabel(const std::wstring& path) {
    const auto slash = path.find_last_of(L"\\/");
    return slash == std::wstring::npos ? path : path.substr(slash + 1);
}

int ChipWidth() {
    const int width = 62 + static_cast<int>(g_chipName.size()) * 8;
    return width < 120 ? 120 : (width > 300 ? 300 : width);
}

std::string UnescapeJsonPath(const std::string& input) {
    std::string output;
    for (size_t i = 0; i < input.size(); ++i) {
        if (input[i] == '\\' && i + 1 < input.size()) {
            const char next = input[++i];
            if (next == 'n') output += '\n';
            else if (next == 'r') output += '\r';
            else if (next == 't') output += '\t';
            else output += next;
        } else {
            output += input[i];
        }
    }
    return output;
}

std::wstring ToolSummary(const std::string& payload) {
    std::string lower = payload;
    for (auto& ch : lower) ch = static_cast<char>(tolower(static_cast<unsigned char>(ch)));
    auto isVideo = [](std::string value) {
        std::transform(value.begin(), value.end(), value.begin(),
                       [](unsigned char ch) { return static_cast<char>(tolower(ch)); });
        for (const char* ext : {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"}) {
            if (value.size() >= strlen(ext) &&
                value.compare(value.size() - strlen(ext), strlen(ext), ext) == 0) return true;
        }
        return false;
    };
    std::vector<std::string> previews = JsonStringArray(payload, "previews");
    std::vector<std::string> paths = JsonStringArray(payload, "paths");
    std::wstring summary;
    auto appendImage = [&](const std::string& path) {
        if (path.empty() || isVideo(path)) return;
        summary += L"\n\n![image](" + ToWide(path) + L")";
    };
    if (!previews.empty()) {
        for (const auto& path : previews) appendImage(path);
    } else {
        for (const auto& path : paths) appendImage(path);
    }
    return summary;
}

void EnterChat(HWND hwnd, const std::string& opening) {
    g_chatMode = true;
    for (HWND control : {g_arrow, g_key, g_langButton, g_langLabel}) {
        if (control) ShowWindow(control, SW_HIDE);
    }
    for (HWND control : {g_userLog, g_assistantLog, g_input, g_send, g_attach, g_imageView}) {
        if (control && !(g_variant == "chat" && control == g_attach)) ShowWindow(control, SW_SHOW);
    }
    g_assistantMarkdown = AvatarMarker(L"assistant", L"avatar_green.png") + "\n" + opening;
    RenderAssistant();
    Layout(hwnd);
    InvalidateRect(hwnd, nullptr, TRUE);
    SetFocus(g_input);
}

void CommitComposition() {
    HIMC context = ImmGetContext(g_input);
    if (!context) return;
    const LONG bytes = ImmGetCompositionStringW(context, GCS_COMPSTR, nullptr, 0);
    if (bytes > 0) {
        std::wstring composition(static_cast<size_t>(bytes / sizeof(wchar_t)), wchar_t(0));
        ImmGetCompositionStringW(context, GCS_COMPSTR, composition.data(), bytes);
        ImmNotifyIME(context, NI_COMPOSITIONSTR, CPS_COMPLETE, 0);
        SetWindowTextW(g_input, composition.c_str());
    } else {
        ImmNotifyIME(context, NI_COMPOSITIONSTR, CPS_COMPLETE, 0);
    }
    ImmReleaseContext(g_input, context);
}

std::wstring CompositionText() {
    HIMC context = ImmGetContext(g_input);
    if (!context) return {};
    const LONG bytes = ImmGetCompositionStringW(context, GCS_COMPSTR, nullptr, 0);
    std::wstring value;
    if (bytes > 0) {
        value.assign(static_cast<size_t>(bytes / sizeof(wchar_t)), wchar_t(0));
        ImmGetCompositionStringW(context, GCS_COMPSTR, value.data(), bytes);
        ImmNotifyIME(context, NI_COMPOSITIONSTR, CPS_CANCEL, 0);
    }
    ImmReleaseContext(g_input, context);
    return value;
}


HWND g_effectsWindow = nullptr;
std::wstring g_effectsMode;
HFONT g_effectsTitleFont = nullptr;
HWND g_effAgingVhs = nullptr;
HWND g_effAgingDig = nullptr;
HWND g_effAgingSlider = nullptr;
HWND g_effAgingLabel = nullptr;
HWND g_effSqueezeSlider = nullptr;
HWND g_effSqueezeLabel = nullptr;
HWND g_effFrameSlider = nullptr;
HWND g_effFrameLabel = nullptr;

void ApplyEffectsCommand(const std::wstring& command) {
    if (!g_mainHwnd || !g_input) return;
    SetWindowTextW(g_input, command.c_str());
    PostMessageW(g_mainHwnd, WM_COMMAND, MAKEWPARAM(1013, BN_CLICKED), 0);
}


struct SliderData { int minimum = 0; int maximum = 100; int value = 0; };

LRESULT CALLBACK TwilightSliderProc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
    auto* data = reinterpret_cast<SliderData*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (msg == WM_NCCREATE) {
        data = new SliderData();
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(data));
    }
    if (!data) return DefWindowProcW(hwnd, msg, wparam, lparam);
    switch (msg) {
    case WM_ERASEBKGND:
        return 1;
    case WM_PAINT: {
        PAINTSTRUCT ps{};
        HDC dc = BeginPaint(hwnd, &ps);
        RECT rc{}; GetClientRect(hwnd, &rc);
        HBRUSH bg = CreateSolidBrush(RGB(235, 222, 247));
        FillRect(dc, &rc, bg); DeleteObject(bg);
        const int left = 12, right = rc.right - 12, cy = rc.bottom / 2;
        const int trackTop = cy - 5, trackBottom = cy + 5;
        HBRUSH white = CreateSolidBrush(RGB(255, 255, 255));
        RECT track{left, trackTop, right, trackBottom};
        FillRect(dc, &track, white); DeleteObject(white);
        HPEN border = CreatePen(PS_SOLID, 1, RGB(92, 52, 160));
        HGDIOBJ oldPen = SelectObject(dc, border);
        HGDIOBJ oldBrush = SelectObject(dc, GetStockObject(NULL_BRUSH));
        Rectangle(dc, left, trackTop, right, trackBottom);
        SelectObject(dc, oldBrush); SelectObject(dc, oldPen); DeleteObject(border);
        const int span = (std::max)(1, data->maximum - data->minimum);
        const int pos = left + (right - left) * (data->value - data->minimum) / span;
        HBRUSH fill = CreateSolidBrush(RGB(126, 72, 176));
        RECT fillRect{left + 1, trackTop + 1, pos, trackBottom - 1};
        FillRect(dc, &fillRect, fill); DeleteObject(fill);
        for (int i = 0; i <= 10; ++i) {
            const int x = left + (right - left) * i / 10;
            HBRUSH tick = CreateSolidBrush(RGB(126, 72, 176));
            RECT tickRect{x, trackBottom + 4, x + 1, trackBottom + 8};
            FillRect(dc, &tickRect, tick); DeleteObject(tick);
        }
        HBRUSH thumb = CreateSolidBrush(RGB(126, 72, 176));
        HPEN thumbPen = CreatePen(PS_SOLID, 1, RGB(76, 40, 112));
        oldPen = SelectObject(dc, thumbPen); oldBrush = SelectObject(dc, thumb);
        RoundRect(dc, pos - 8, cy - 15, pos + 8, cy + 15, 6, 6);
        SelectObject(dc, oldBrush); SelectObject(dc, oldPen);
        DeleteObject(thumb); DeleteObject(thumbPen);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_LBUTTONDOWN: {
        SetCapture(hwnd);
        RECT rc{}; GetClientRect(hwnd, &rc);
        const int x = std::clamp(static_cast<int>(GET_X_LPARAM(lparam)), 12, static_cast<int>(rc.right) - 12);
        const int span = (std::max)(1, static_cast<int>(rc.right) - 24);
        data->value = data->minimum + (data->maximum - data->minimum) * (x - 12) / span;
        InvalidateRect(hwnd, nullptr, FALSE);
        SendMessageW(GetParent(hwnd), WM_HSCROLL, MAKEWPARAM(SB_THUMBPOSITION, data->value), reinterpret_cast<LPARAM>(hwnd));
        return 0;
    }
    case WM_MOUSEMOVE:
        if (GetCapture() == hwnd) {
            RECT rc{}; GetClientRect(hwnd, &rc);
            const int x = std::clamp(static_cast<int>(GET_X_LPARAM(lparam)), 12, static_cast<int>(rc.right) - 12);
            const int span = (std::max)(1, static_cast<int>(rc.right) - 24);
            const int next = data->minimum + (data->maximum - data->minimum) * (x - 12) / span;
            if (next != data->value) {
                data->value = next;
                InvalidateRect(hwnd, nullptr, FALSE);
                SendMessageW(GetParent(hwnd), WM_HSCROLL, MAKEWPARAM(SB_THUMBPOSITION, data->value), reinterpret_cast<LPARAM>(hwnd));
            }
        }
        return 0;
    case WM_LBUTTONUP:
        if (GetCapture() == hwnd) ReleaseCapture();
        return 0;
    default:
        return DefWindowProcW(hwnd, msg, wparam, lparam);
    }
}

void SliderSetRange(HWND hwnd, int minimum, int maximum) {
    auto* data = reinterpret_cast<SliderData*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (!data) return;
    data->minimum = minimum; data->maximum = (std::max)(minimum + 1, maximum);
    data->value = std::clamp(data->value, data->minimum, data->maximum);
    InvalidateRect(hwnd, nullptr, FALSE);
}
void SliderSetPos(HWND hwnd, int value) {
    auto* data = reinterpret_cast<SliderData*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (!data) return;
    data->value = std::clamp(value, data->minimum, data->maximum);
    InvalidateRect(hwnd, nullptr, FALSE);
}
int SliderGetPos(HWND hwnd) {
    auto* data = reinterpret_cast<SliderData*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    return data ? data->value : 0;
}

void UpdateEffectsLabel(HWND slider, HWND label, const wchar_t* prefix, bool percent) {
    const int value = SliderGetPos(slider);
    wchar_t text[64]{};
    if (percent) swprintf_s(text, L"%d%%", value);
    else swprintf_s(text, L"%d×", value);
    SetWindowTextW(label, text);
}

LRESULT CALLBACK EffectsWndProc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
    switch (msg) {
    case WM_ERASEBKGND: {
        RECT rc{}; GetClientRect(hwnd, &rc);
        HBRUSH brush = CreateSolidBrush(RGB(235, 222, 247));
        FillRect(reinterpret_cast<HDC>(wparam), &rc, brush);
        DeleteObject(brush);
        return 1;
    }
    case WM_CTLCOLORSTATIC: {
        HDC dc = reinterpret_cast<HDC>(wparam);
        SetBkMode(dc, OPAQUE);
        SetBkColor(dc, RGB(235, 222, 247));
        SetTextColor(dc, RGB(45, 25, 60));
        static HBRUSH effectsBackground = CreateSolidBrush(RGB(235, 222, 247));
        return reinterpret_cast<LRESULT>(effectsBackground);
    }
    case WM_HSCROLL:
        if (reinterpret_cast<HWND>(lparam) == g_effAgingSlider) UpdateEffectsLabel(g_effAgingSlider, g_effAgingLabel, Ui(UiKey::Aging), true);
        else if (reinterpret_cast<HWND>(lparam) == g_effSqueezeSlider) UpdateEffectsLabel(g_effSqueezeSlider, g_effSqueezeLabel, Ui(UiKey::Squeeze), true);
        else if (reinterpret_cast<HWND>(lparam) == g_effFrameSlider) UpdateEffectsLabel(g_effFrameSlider, g_effFrameLabel, Ui(UiKey::Frame), false);
        return 0;
    case WM_DRAWITEM: {
        auto* item = reinterpret_cast<DRAWITEMSTRUCT*>(lparam);
        if (!item) return 0;
        HDC dc = item->hDC;
        RECT rc = item->rcItem;
        HBRUSH bg = CreateSolidBrush(RGB(235, 222, 247));
        FillRect(dc, &rc, bg);
        DeleteObject(bg);
        HBRUSH fill = CreateSolidBrush(RGB(126, 72, 176));
        HPEN pen = CreatePen(PS_SOLID, 1, RGB(94, 49, 139));
        HGDIOBJ oldBrush = SelectObject(dc, fill);
        HGDIOBJ oldPen = SelectObject(dc, pen);
        RoundRect(dc, rc.left + 2, rc.top + 2, rc.right - 2, rc.bottom - 2, 8, 8);
        SelectObject(dc, oldBrush);
        SelectObject(dc, oldPen);
        DeleteObject(fill);
        DeleteObject(pen);
        SetBkMode(dc, TRANSPARENT);
        SetTextColor(dc, RGB(255, 255, 255));
        HGDIOBJ oldFont = SelectObject(dc, g_uiFont);
        DrawTextW(dc, L"√", -1, &rc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        SelectObject(dc, oldFont);
        return 0;
    }
    case WM_COMMAND: {
        const int id = LOWORD(wparam);
        if (id == 2005) {
            if (g_effectsMode == L"aging") {
                const int value = SliderGetPos(g_effAgingSlider);
                const bool vhs = SendMessageW(g_effAgingVhs, BM_GETCHECK, 0, 0) == BST_CHECKED;
                wchar_t command[64]{};
                swprintf_s(command, L"%s %.2f", vhs ? L"/vhs化" : L"/压片", value / 100.0);
                ApplyEffectsCommand(command);
            } else if (g_effectsMode == L"squeeze") {
                const int value = SliderGetPos(g_effSqueezeSlider);
                wchar_t command[64]{};
                swprintf_s(command, L"/压片 %.2f", value / 100.0);
                ApplyEffectsCommand(command);
            } else {
                const int value = SliderGetPos(g_effFrameSlider);
                wchar_t command[64]{};
                swprintf_s(command, L"/frame-interpolation %d", value);
                ApplyEffectsCommand(command);
            }
            ShowWindow(hwnd, SW_HIDE);
        }
        return 0;
    }
    case WM_CLOSE:
        ShowWindow(hwnd, SW_HIDE);
        return 0;
    default:
        return DefWindowProcW(hwnd, msg, wparam, lparam);
    }
}

void ShowEffectsWindow() {
    if (!g_effectsWindow) {
        WNDCLASSEXW wc{};
        wc.cbSize = sizeof(wc);
        wc.lpfnWndProc = EffectsWndProc;
        wc.hInstance = g_instance;
        wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        wc.hbrBackground = CreateSolidBrush(RGB(235, 222, 247));
        wc.lpszClassName = L"TwilightEffectsWindow";
        RegisterClassExW(&wc);
        g_effectsWindow = CreateWindowExW(WS_EX_TOOLWINDOW, L"TwilightEffectsWindow", Ui(UiKey::Effects),
                                          WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
                                          CW_USEDEFAULT, CW_USEDEFAULT, 760, 680,
                                          g_mainHwnd, nullptr, g_instance, nullptr);
        if (!g_effectsTitleFont) {
            g_effectsTitleFont = CreateFontW(-30, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                             DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                             CLEARTYPE_QUALITY, DEFAULT_PITCH, UiFontFamily(true).c_str());
        }
        auto add = [&](const wchar_t* cls, const wchar_t* text, DWORD style, int x, int y, int w, int h, int id, bool title = false) {
            HWND control = CreateWindowExW(0, cls, text, WS_CHILD | WS_VISIBLE | style,
                                           x, y, w, h, g_effectsWindow, reinterpret_cast<HMENU>(id), g_instance, nullptr);
            if (title) {
                if (g_effectsTitleFont) SendMessageW(control, WM_SETFONT, reinterpret_cast<WPARAM>(g_effectsTitleFont), TRUE);
            } else if (g_uiFont) {
                SendMessageW(control, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
            }
            return control;
        };

        add(L"STATIC", Ui(UiKey::Aging), SS_LEFT, 32, 24, 120, 40, 2000, true);
        g_effAgingVhs = add(L"BUTTON", L"VHS", BS_AUTORADIOBUTTON | WS_GROUP, 36, 76, 90, 28, 2001);
        g_effAgingDig = add(L"BUTTON", L"DIG", BS_AUTORADIOBUTTON, 136, 76, 82, 28, 2002);
        SendMessageW(g_effAgingVhs, BM_SETCHECK, BST_CHECKED, 0);
        g_effAgingSlider = add(TRACKBAR_CLASSW, L"", TBS_HORZ | TBS_AUTOTICKS, 230, 68, 440, 42, 2003);
        SendMessageW(g_effAgingSlider, TBM_SETRANGE, TRUE, MAKELPARAM(0, 100));
        SendMessageW(g_effAgingSlider, TBM_SETTICFREQ, 10, 0);
        SendMessageW(g_effAgingSlider, TBM_SETPOS, TRUE, 5);
        g_effAgingLabel = add(L"STATIC", L"5%", SS_RIGHT, 680, 75, 50, 28, 2004);
        add(L"BUTTON", L"√", BS_OWNERDRAW, 680, 126, 50, 42, 2005);

        add(L"STATIC", Ui(UiKey::Squeeze), SS_LEFT, 32, 224, 120, 40, 2010, true);
        g_effSqueezeSlider = add(TRACKBAR_CLASSW, L"", TBS_HORZ | TBS_AUTOTICKS, 36, 286, 634, 42, 2013);
        SendMessageW(g_effSqueezeSlider, TBM_SETRANGE, TRUE, MAKELPARAM(0, 100));
        SendMessageW(g_effSqueezeSlider, TBM_SETTICFREQ, 10, 0);
        SendMessageW(g_effSqueezeSlider, TBM_SETPOS, TRUE, 12);
        g_effSqueezeLabel = add(L"STATIC", L"12%", SS_RIGHT, 680, 292, 50, 28, 2014);
        add(L"BUTTON", L"√", BS_OWNERDRAW, 680, 342, 50, 42, 2015);

        add(L"STATIC", Ui(UiKey::Frame), SS_LEFT, 32, 424, 120, 40, 2020, true);
        g_effFrameSlider = add(TRACKBAR_CLASSW, L"", TBS_HORZ | TBS_AUTOTICKS, 36, 486, 634, 42, 2023);
        SendMessageW(g_effFrameSlider, TBM_SETRANGE, TRUE, MAKELPARAM(2, 30));
        SendMessageW(g_effFrameSlider, TBM_SETTICFREQ, 2, 0);
        SendMessageW(g_effFrameSlider, TBM_SETPOS, TRUE, 10);
        g_effFrameLabel = add(L"STATIC", L"10×", SS_RIGHT, 680, 492, 50, 28, 2024);
        add(L"BUTTON", L"√", BS_OWNERDRAW, 680, 542, 50, 42, 2025);
    }
    ShowWindow(g_effectsWindow, SW_SHOW);
    SetWindowPos(g_effectsWindow, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
    SetForegroundWindow(g_effectsWindow);
}

void ShowEffectsWindowFor(const std::wstring& mode) {
    if (!g_effectsWindow) {
        WNDCLASSEXW wc{};
        wc.cbSize = sizeof(wc);
        wc.lpfnWndProc = EffectsWndProc;
        wc.hInstance = g_instance;
        wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        wc.hbrBackground = CreateSolidBrush(RGB(235, 222, 247));
        wc.lpszClassName = L"TwilightEffectsWindow";
        RegisterClassExW(&wc);
    }
    if (g_effectsWindow) {
        DestroyWindow(g_effectsWindow);
        g_effectsWindow = nullptr;
    }
    static bool sliderClassRegistered = false;
    if (!sliderClassRegistered) {
        WNDCLASSEXW sliderClass{};
        sliderClass.cbSize = sizeof(sliderClass);
        sliderClass.lpfnWndProc = TwilightSliderProc;
        sliderClass.hInstance = g_instance;
        sliderClass.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        sliderClass.lpszClassName = L"TwilightSlider";
        RegisterClassExW(&sliderClass);
        sliderClassRegistered = true;
    }
    g_effAgingVhs = g_effAgingDig = nullptr;
    g_effAgingSlider = g_effSqueezeSlider = g_effFrameSlider = nullptr;
    g_effAgingLabel = g_effSqueezeLabel = g_effFrameLabel = nullptr;
    g_effectsMode = mode;
    g_effectsWindow = CreateWindowExW(WS_EX_TOOLWINDOW, L"TwilightEffectsWindow", Ui(UiKey::Effects),
                                      WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
                                      CW_USEDEFAULT, CW_USEDEFAULT, 620, 300,
                                      g_mainHwnd, nullptr, g_instance, nullptr);
    if (!g_effectsTitleFont) {
        g_effectsTitleFont = CreateFontW(-30, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                         DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                         CLEARTYPE_QUALITY, DEFAULT_PITCH, UiFontFamily(true).c_str());
    }
    auto add = [&](const wchar_t* cls, const wchar_t* text, DWORD style, int x, int y, int w, int h, int id, bool title = false) {
        HWND control = CreateWindowExW(0, cls, text, WS_CHILD | WS_VISIBLE | style,
                                       x, y, w, h, g_effectsWindow, reinterpret_cast<HMENU>(id), g_instance, nullptr);
        if (title && g_effectsTitleFont) {
            SendMessageW(control, WM_SETFONT, reinterpret_cast<WPARAM>(g_effectsTitleFont), TRUE);
        } else if (g_uiFont) {
            SendMessageW(control, WM_SETFONT, reinterpret_cast<WPARAM>(g_uiFont), TRUE);
        }
        return control;
    };
    if (mode == L"aging") {
        add(L"STATIC", Ui(UiKey::Aging), SS_LEFT, 32, 22, 120, 40, 2000, true);
        g_effAgingVhs = add(L"BUTTON", L"VHS", BS_AUTORADIOBUTTON | WS_GROUP, 36, 78, 90, 28, 2001);
        g_effAgingDig = add(L"BUTTON", L"DIG", BS_AUTORADIOBUTTON, 136, 78, 82, 28, 2002);
        SendMessageW(g_effAgingVhs, BM_SETCHECK, BST_CHECKED, 0);
        g_effAgingSlider = add(L"TwilightSlider", L"", 0, 230, 70, 290, 42, 2003);
        SliderSetRange(g_effAgingSlider, 0, 100);
        SliderSetPos(g_effAgingSlider, 5);
        g_effAgingLabel = add(L"STATIC", L"5%", SS_RIGHT, 520, 78, 70, 28, 2004);
        if (g_versionFont) SendMessageW(g_effAgingLabel, WM_SETFONT, reinterpret_cast<WPARAM>(g_versionFont), TRUE);
        add(L"BUTTON", L"√", BS_OWNERDRAW, 550, 130, 50, 42, 2005);
        SetWindowTheme(g_effAgingSlider, L"", L"");
    } else if (mode == L"squeeze") {
        add(L"STATIC", Ui(UiKey::Squeeze), SS_LEFT, 32, 22, 120, 40, 2010, true);
        g_effSqueezeSlider = add(L"TwilightSlider", L"", 0, 36, 98, 480, 42, 2013);
        SliderSetRange(g_effSqueezeSlider, 0, 100);
        SliderSetPos(g_effSqueezeSlider, 12);
        g_effSqueezeLabel = add(L"STATIC", L"12%", SS_RIGHT, 520, 106, 70, 28, 2014);
        if (g_versionFont) SendMessageW(g_effSqueezeLabel, WM_SETFONT, reinterpret_cast<WPARAM>(g_versionFont), TRUE);
        add(L"BUTTON", L"√", BS_OWNERDRAW, 550, 158, 50, 42, 2005);
        SetWindowTheme(g_effSqueezeSlider, L"", L"");
    } else {
        add(L"STATIC", Ui(UiKey::Frame), SS_LEFT, 32, 22, 120, 40, 2020, true);
        g_effFrameSlider = add(L"TwilightSlider", L"", 0, 36, 98, 480, 42, 2023);
        SliderSetRange(g_effFrameSlider, 2, 30);
        SliderSetPos(g_effFrameSlider, 10);
        g_effFrameLabel = add(L"STATIC", L"10×", SS_RIGHT, 520, 106, 70, 28, 2024);
        if (g_versionFont) SendMessageW(g_effFrameLabel, WM_SETFONT, reinterpret_cast<WPARAM>(g_versionFont), TRUE);
        add(L"BUTTON", L"√", BS_OWNERDRAW, 550, 158, 50, 42, 2005);
        SetWindowTheme(g_effFrameSlider, L"", L"");
    }
    ShowWindow(g_effectsWindow, SW_SHOW);
    SetWindowPos(g_effectsWindow, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
    SetForegroundWindow(g_effectsWindow);
}

void SendChat(HWND hwnd) {
    CommitComposition();
    const int length = GetWindowTextLengthW(g_input);
    if (length <= 0) return;
    std::wstring text(length + 1, L'\0');
    GetWindowTextW(g_input, text.data(), length + 1);
    text.resize(length);
    {
        HIMC context = ImmGetContext(g_input);
        if (context) {
            ImmSetConversionStatus(context, IME_CMODE_ALPHANUMERIC, IME_SMODE_NONE);
            ImmNotifyIME(context, NI_COMPOSITIONSTR, CPS_CANCEL, 0);
            ImmReleaseContext(g_input, context);
        }
    }
    std::wstring trimmed = text;
    while (!trimmed.empty() && iswspace(trimmed.front())) trimmed.erase(trimmed.begin());
    while (!trimmed.empty() && iswspace(trimmed.back())) trimmed.pop_back();
    if (_wcsicmp(trimmed.c_str(), L"/frame") == 0 ||
        _wcsicmp(trimmed.c_str(), L"/frame-upscale") == 0 ||
        _wcsicmp(trimmed.c_str(), L"/frame-interpolation") == 0) {
        ShowEffectsWindowFor(L"frame");
        return;
        g_framePanel = true;
        g_paramPanel = false;
        ShowWindow(g_paramLabel, SW_HIDE);
        ShowWindow(g_paramSlider, SW_HIDE);
        ShowWindow(g_paramApply, SW_HIDE);
        ShowWindow(g_frameLabel, SW_SHOW);
        ShowWindow(g_frameSlider, SW_SHOW);
        ShowWindow(g_frameApply, SW_SHOW);
        Layout(hwnd);
        return;
    }
    const std::wstring lower = [&]() { std::wstring value = trimmed; for (auto& ch : value) ch = static_cast<wchar_t>(towlower(ch)); return value; }();
    const bool explicitEffect = lower == L"/vhs" || lower == L"/vhs化" || lower == L"/compress" ||
        lower == L"/压片" || lower == L"/aging" || lower == L"/包浆";
    if (!g_attachmentPath.empty() && !explicitEffect &&
        (lower.find(L"压片") != std::wstring::npos || lower.find(L"压个片") != std::wstring::npos)) {
        ShowEffectsWindowFor(L"squeeze");
        return;
    }
    if (!g_attachmentPath.empty() && !explicitEffect &&
        (lower == L"/做旧" || lower.find(L"做旧") != std::wstring::npos)) {
        ShowEffectsWindowFor(L"aging");
        return;
        g_choicePanel = true;
        g_paramPanel = true;
        g_framePanel = false;
        g_paramCommand = L"/vhs";
        SendMessageW(g_choiceVhs, BM_SETCHECK, BST_CHECKED, 0);
        SendMessageW(g_choiceDig, BM_SETCHECK, BST_UNCHECKED, 0);
        SendMessageW(g_paramSlider, TBM_SETPOS, TRUE, 5);
        SetWindowTextW(g_paramLabel, (std::wstring(Ui(UiKey::Aging)) + L" 5%").c_str());
        ShowWindow(g_frameLabel, SW_HIDE);
        ShowWindow(g_frameSlider, SW_HIDE);
        ShowWindow(g_frameApply, SW_HIDE);
        ShowWindow(g_paramLabel, SW_SHOW);
        ShowWindow(g_paramSlider, SW_SHOW);
        ShowWindow(g_paramApply, SW_SHOW);
        ShowWindow(g_choiceVhs, SW_SHOW);
        ShowWindow(g_choiceDig, SW_SHOW);
        Layout(hwnd);
        return;
    }
    if ((lower == L"/vhs" || lower == L"/vhs化" || lower == L"/compress" ||
         lower == L"/压片" || lower == L"/aging" || lower == L"/包浆") &&
        trimmed.find(L' ') == std::wstring::npos) {
        ShowEffectsWindowFor(lower == L"/压片" ? L"squeeze" : L"aging");
        return;
        if (lower == L"/vhs" || lower == L"/vhs化") g_paramCommand = L"/vhs";
        else if (lower == L"/compress" || lower == L"/压片") g_paramCommand = L"/compress";
        else g_paramCommand = L"/aging";
        g_paramTitle = std::wstring(g_paramCommand == L"/vhs" ? Ui(UiKey::Vhs) : (g_paramCommand == L"/compress" ? Ui(UiKey::Squeeze) : Ui(UiKey::Aging))) + L" · 0.70";
        SetWindowTextW(g_paramLabel, g_paramTitle.c_str());
        SendMessageW(g_paramSlider, TBM_SETPOS, TRUE, 70);
        g_paramPanel = true;
        g_choicePanel = false;
        ShowWindow(g_choiceVhs, SW_HIDE);
        ShowWindow(g_choiceDig, SW_HIDE);
        g_framePanel = false;
        ShowWindow(g_frameLabel, SW_HIDE);
        ShowWindow(g_frameSlider, SW_HIDE);
        ShowWindow(g_frameApply, SW_HIDE);
        ShowWindow(g_paramLabel, SW_SHOW);
        ShowWindow(g_paramSlider, SW_SHOW);
        ShowWindow(g_paramApply, SW_SHOW);
        Layout(hwnd);
        return;
    }
    if (!g_detachedImc) {
        g_detachedImc = ImmAssociateContext(g_input, nullptr);
    }
    SetFocus(g_assistantLog);
    SetWindowTextW(g_input, L"");
    PostMessageW(hwnd, WM_APP_RECREATE_INPUT, 0, 0);
    g_reasoningMarkdown.clear();
    g_lastToolLabel.clear();
    g_lastUserPrompt = text;
    RenderReasoning();
    Layout(hwnd);
    if (g_firstTurn) {
        g_assistantMarkdown.clear();
        g_firstTurn = false;
    }
    g_pendingUserPrompt = text;
    g_pendingAttachmentLabel = g_chipName;
    g_streamingAnswer.clear();
    g_spokenOffset = 0;
    RenderAssistant();

    std::string userContent = g_attachmentText.empty() ? ToUtf8(text)
                                                       : g_attachmentText + "\n" + ToUtf8(text);
    const std::string attachmentImage = g_attachmentImageUri;
    const std::vector<std::string> attachmentImages = g_attachmentImageUris;
    g_attachmentImageUri.clear();
    g_attachmentImageUris.clear();
    g_attachmentText.clear();
    g_chipName.clear();
    ShowWindow(g_chip, SW_HIDE);
    g_messages.emplace_back("user", userContent);
    std::string body = "{\"messages\":[";
    for (size_t i = 0; i < g_messages.size(); ++i) {
        if (i) body += ",";
        body += "{\"role\":\"" + JsonEscape(g_messages[i].first) + "\",\"content\":\"" +
                JsonEscape(g_messages[i].second) + "\"}";
    }
    body += "]";
    if (!attachmentImages.empty()) {
        body += ",\"images\":[";
        for (size_t index = 0; index < attachmentImages.size(); ++index) {
            if (index) body += ",";
            body += "\"" + JsonEscape(attachmentImages[index]) + "\"";
        }
        body += "]";
    } else if (!attachmentImage.empty()) {
        body += ",\"images\":[\"" + JsonEscape(attachmentImage) + "\"]";
    }
    if (!g_attachmentPath.empty()) {
        body += ",\"attachment_path\":\"" + JsonEscape(ToUtf8(g_attachmentPath)) + "\"";
    }
    body += "}";
    EnableWindow(g_send, FALSE);
    g_twilightImage = g_imgBlue;
    ShowWindow(g_imageView, SW_HIDE);
    Layout(hwnd);
    std::thread([hwnd, body]() {
        std::string answer;
        std::string error;
        bool ok = g_sidecar.PostStream("/chat", body, [&](const std::string& line) {
            if (line.rfind("data: ", 0) != 0) return;
            const std::string payload = line.substr(6);
            const std::string event = JsonField(payload, "event");
            if (event == "reasoning") {
                const std::string chunk = JsonField(payload, "text");
                if (!chunk.empty()) {
                    PostMessageW(hwnd, WM_APP_REASONING, 0,
                                 reinterpret_cast<LPARAM>(new std::wstring(CleanTextUtf8(chunk))));
                }
            } else if (event == "content") {
                const std::string chunk = JsonField(payload, "text");
                if (!chunk.empty()) {
                    answer += chunk;
                    PostMessageW(hwnd, WM_APP_CHUNK, 0,
                                 reinterpret_cast<LPARAM>(new std::wstring(CleanTextUtf8(chunk))));
                }
            } else if (event == "done") {
                const std::string finalContent = JsonField(payload, "content");
                if (!finalContent.empty()) answer = finalContent;
            } else if (event == "tools") {
                const std::wstring summary = ToolSummary(payload);
                if (!summary.empty()) {
                    PostMessageW(hwnd, WM_APP_TOOL, 0,
                                 reinterpret_cast<LPARAM>(new std::wstring(summary)));
                }
            } else if (event == "progress") {
                auto* update = new ProgressUpdate();
                update->id = ToWide(JsonField(payload, "id"));
                update->label = ToWide(JsonField(payload, "label"));
                update->detail = ToWide(JsonField(payload, "detail"));
                update->name = ToWide(JsonField(payload, "name"));
                update->bytesDone = atoll(JsonField(payload, "done").c_str());
                update->bytesTotal = atoll(JsonField(payload, "total").c_str());
                update->batchDone = atoi(JsonField(payload, "batch_done").c_str());
                update->batchTotal = atoi(JsonField(payload, "batch_total").c_str());
                update->fraction = static_cast<float>(atof(JsonField(payload, "fraction").c_str()));
                update->done = JsonField(payload, "state") == "done";
                if (!update->label.empty()) {
                    PostMessageW(hwnd, WM_APP_PROGRESS, 0, reinterpret_cast<LPARAM>(update));
                } else {
                    delete update;
                }
            } else if (event == "error") {
                error = JsonField(payload, "error");
            }
        }, error);
        if (!ok || !error.empty()) {
            PostMessageW(hwnd, WM_APP_ERROR, 0,
                         reinterpret_cast<LPARAM>(new std::wstring(ToWide(error.empty() ? "请求失败" : error))));
        }
        PostMessageW(hwnd, WM_APP_DONE, 0,
                     reinterpret_cast<LPARAM>(new std::wstring(CleanTextUtf8(answer))));
    }).detach();
}

LRESULT CALLBACK LowLevelKeyboardProc(int code, WPARAM wparam, LPARAM lparam) {
    if (code == HC_ACTION && (wparam == WM_KEYDOWN || wparam == WM_SYSKEYDOWN)) {
        const auto* info = reinterpret_cast<KBDLLHOOKSTRUCT*>(lparam);
        if (info && info->vkCode == VK_RETURN) {
            const bool control = (GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0;
            const bool alt = (GetAsyncKeyState(VK_MENU) & 0x8000) != 0;
            if (g_mainHwnd && (control || alt)) {
                const int id = control ? 1013 : 1014;
                PostMessageW(g_mainHwnd, WM_COMMAND, MAKEWPARAM(id, BN_CLICKED), 0);
                return 1;
            }
        }
    }
    return CallNextHookEx(g_keyboardHook, code, wparam, lparam);
}

LRESULT CALLBACK ProgressWndProc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
    switch (msg) {
    case WM_ERASEBKGND:
        return 1;
    case WM_PAINT: {
        PAINTSTRUCT ps{};
        HDC dc = BeginPaint(hwnd, &ps);
        RECT rc{};
        GetClientRect(hwnd, &rc);
        HBRUSH background = CreateSolidBrush(kBg);
        FillRect(dc, &rc, background);
        DeleteObject(background);
        SetBkMode(dc, TRANSPARENT);
        SetTextColor(dc, kInk);
        HGDIOBJ old = SelectObject(dc, g_titleFont);
        RECT title{24, 16, rc.right - 24, 54};
        DrawTextW(dc, Ui(UiKey::Progress), -1, &title, DT_LEFT | DT_SINGLELINE | DT_VCENTER);
        SelectObject(dc, g_uiFont);
        int y = 68;
        const int barWidth = rc.right - 48;
        for (const auto& task : g_progressTasks) {
            SetTextColor(dc, kInk);
            RECT label{24, y, rc.right - 110, y + 26};
            DrawTextW(dc, task.label.c_str(), -1, &label, DT_LEFT | DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS);
            wchar_t percent[64]{};
            if (task.batchTotal > 0) {
                swprintf_s(percent, L"%d / %d", task.batchDone, task.batchTotal);
            } else {
                swprintf_s(percent, L"%d %%", static_cast<int>(task.fraction * 100));
            }
            SetTextColor(dc, RGB(80, 60, 96));
            RECT value{rc.right - 150, y, rc.right - 24, y + 26};
            DrawTextW(dc, percent, -1, &value, DT_RIGHT | DT_SINGLELINE | DT_VCENTER);
            RECT bar{24, y + 30, rc.right - 24, y + 50};
            HBRUSH empty = CreateSolidBrush(RGB(255, 255, 255));
            FillRect(dc, &bar, empty);
            DeleteObject(empty);
            FrameRect(dc, &bar, static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH)));
            RECT filled = bar;
            filled.right = bar.left + static_cast<int>((bar.right - bar.left) * std::clamp(task.fraction, 0.0f, 1.0f));
            HBRUSH fill = CreateSolidBrush(kPurple);
            FillRect(dc, &filled, fill);
            DeleteObject(fill);
            if (!task.detail.empty()) {
                SetTextColor(dc, RGB(80, 60, 96));
                RECT detail{24, y + 54, rc.right - 24, y + 76};
                std::wstring detailText = task.detail;
                if (!task.name.empty()) {
                    wchar_t bytes[160]{};
                    if (task.bytesTotal > 0) {
                        swprintf_s(bytes, L"%s    %.1f KiB / %.1f KiB", task.name.c_str(),
                                   task.bytesDone / 1024.0, task.bytesTotal / 1024.0);
                    } else {
                        swprintf_s(bytes, L"%s", task.name.c_str());
                    }
                    detailText = bytes;
                }
                DrawTextW(dc, detailText.c_str(), -1, &detail, DT_LEFT | DT_SINGLELINE | DT_END_ELLIPSIS);
                y += 84;
            } else {
                y += 62;
            }
            if (y > rc.bottom - 70) break;
        }
        SelectObject(dc, old);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_TIMER:
        if (wparam == 9) {
            KillTimer(hwnd, 9);
            ShowWindow(hwnd, SW_HIDE);
            g_progressTasks.clear();
        }
        return 0;
    case WM_CLOSE:
        ShowWindow(hwnd, SW_HIDE);
        return 0;
    case WM_DESTROY:
        g_progressWindow = nullptr;
        return 0;
    default:
        return DefWindowProcW(hwnd, msg, wparam, lparam);
    }
}

void ShowProgressWindow(HWND owner) {
    if (g_progressWindow) {
        ShowWindow(g_progressWindow, SW_SHOW);
        SetWindowPos(g_progressWindow, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
        return;
    }
    static bool registered = false;
    if (!registered) {
        WNDCLASSEXW wc{};
        wc.cbSize = sizeof(wc);
        wc.lpfnWndProc = ProgressWndProc;
        wc.hInstance = g_instance;
        wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        wc.lpszClassName = L"TwilightProgressWindow";
        wc.hbrBackground = nullptr;
        RegisterClassExW(&wc);
        registered = true;
    }
    g_progressWindow = CreateWindowExW(WS_EX_TOOLWINDOW, L"TwilightProgressWindow", Ui(UiKey::Progress),
                                       WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
                                       560, 240, 620, 460, owner, nullptr, g_instance, nullptr);
    ShowWindow(g_progressWindow, SW_SHOW);
    UpdateWindow(g_progressWindow);
}

void ApplyProgressUpdate(const ProgressUpdate& update) {
    std::wstring key = update.id.empty() ? update.label : update.id;
    const bool download = key.rfind(L"download-", 0) == 0;
    if (download) key = L"download-batch";
    if (download) {
        g_progressTasks.erase(
            std::remove_if(g_progressTasks.begin(), g_progressTasks.end(),
                           [](const ProgressTask& task) {
                               return task.id == L"derpi-search" || task.id == L"tool-derpi";
                           }),
            g_progressTasks.end());
    }
    bool found = false;
    for (auto& task : g_progressTasks) {
        if (task.id == key) {
            if (!update.label.empty() && !download) task.label = update.label;
            if (download && task.label.empty()) task.label = Ui(UiKey::DerpiDownload);
            task.detail = update.detail;
            task.name = update.name;
            task.bytesDone = update.bytesDone;
            task.bytesTotal = update.bytesTotal;
            task.batchDone = update.batchDone;
            task.batchTotal = update.batchTotal;
            bool done = update.done || (task.batchTotal > 0 && task.batchDone >= task.batchTotal);
            task.fraction = update.fraction;
            task.done = done;
            found = true;
            break;
        }
    }
    if (!found) {
        std::wstring label = download ? Ui(UiKey::DerpiDownload) : update.label;
        bool done = update.done || (update.batchTotal > 0 && update.batchDone >= update.batchTotal);
        g_progressTasks.push_back({key, label, update.detail, update.name, update.bytesDone, update.bytesTotal,
                                   update.batchDone, update.batchTotal, update.fraction, done});
    }
    ShowProgressWindow(g_mainHwnd);
    if (g_progressWindow) InvalidateRect(g_progressWindow, nullptr, TRUE);
    bool allDone = !g_progressTasks.empty();
    for (const auto& task : g_progressTasks) {
        if (!task.done && task.fraction < 0.999f) allDone = false;
    }
    if (allDone && g_progressWindow) SetTimer(g_progressWindow, 9, 900, nullptr);
}

LRESULT CALLBACK WindowProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_GETMINMAXINFO: {
        auto* info = reinterpret_cast<MINMAXINFO*>(lParam);
        info->ptMinTrackSize.x = 960;
        info->ptMinTrackSize.y = 640;
        return 0;
    }
    case WM_CREATE:
        CreateControls(hwnd);
        SetTimer(hwnd, 7, 50, nullptr);
        StartSidecarAsync();
        return 0;
    case WM_SIZE:
        Layout(hwnd);
        InvalidateRect(hwnd, nullptr, TRUE);
        return 0;
    case WM_ERASEBKGND:
        return 1;
    case WM_PAINT:
        Paint(hwnd);
        return 0;
    case WM_CTLCOLOREDIT: {
        HDC dc = reinterpret_cast<HDC>(wParam);
        const HWND control = reinterpret_cast<HWND>(lParam);
        const bool input = control == g_input || control == g_key;
        SetBkColor(dc, input ? RGB(255, 255, 255) : kBg);
        wchar_t keyText[512]{};
        if (control == g_key) GetWindowTextW(g_key, keyText, 512);
        SetTextColor(dc, control == g_key && wcscmp(keyText, KeyHintText().c_str()) == 0
                          ? RGB(160, 150, 170) : kInk);
        static HBRUSH white = CreateSolidBrush(RGB(255, 255, 255));
        static HBRUSH lavender = CreateSolidBrush(kBg);
        return reinterpret_cast<LRESULT>(input ? white : lavender);
    }
    case WM_CTLCOLORSTATIC: {
        HDC dc = reinterpret_cast<HDC>(wParam);
        SetBkColor(dc, kRightBg);
        SetTextColor(dc, kInk);
        static HBRUSH brush = CreateSolidBrush(kRightBg);
        return reinterpret_cast<LRESULT>(brush);
    }
    case WM_DRAWITEM:
        if (wParam == 1001) {
            DrawArrow(reinterpret_cast<DRAWITEMSTRUCT*>(lParam));
            return TRUE;
        }
        if (wParam == 1003) {
            auto* item = reinterpret_cast<DRAWITEMSTRUCT*>(lParam);
            HBRUSH background = CreateSolidBrush(kRightBg);
            FillRect(item->hDC, &item->rcItem, background);
            DeleteObject(background);
            RECT box = item->rcItem;
            DrawRoundedFill(item->hDC, box, 12, RGB(255, 255, 255));
            DrawRoundedStroke(item->hDC, box, 12, RGB(210, 190, 228), 1);
            SetBkMode(item->hDC, TRANSPARENT);
            SetTextColor(item->hDC, kInk);
            HGDIOBJ old = SelectObject(item->hDC, g_uiFont);
            RECT text = box;
            text.left += 14;
            text.right -= 34;
            DrawTextW(item->hDC, g_language.c_str(), -1, &text,
                      DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
            RECT chevron = box;
            chevron.left = chevron.right - 30;
            SetTextColor(item->hDC, kPurple);
            DrawTextW(item->hDC, L"v", -1, &chevron, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
            SelectObject(item->hDC, old);
            return TRUE;
        }
        if (wParam == 1013 || wParam == 1014) {
            auto* item = reinterpret_cast<DRAWITEMSTRUCT*>(lParam);
            HBRUSH background = CreateSolidBrush(RGB(255, 255, 255));
            FillRect(item->hDC, &item->rcItem, background);
            DeleteObject(background);
            const bool send = wParam == 1013;
            RECT circle = item->rcItem;
            const int size = min(circle.right - circle.left, circle.bottom - circle.top);
            circle.left = (circle.left + circle.right - size) / 2;
            circle.top = (circle.top + circle.bottom - size) / 2;
            circle.right = circle.left + size;
            circle.bottom = circle.top + size;
            DrawRoundedFill(item->hDC, circle, size / 2, kPurple);
            if (send) {
                SetBkMode(item->hDC, TRANSPARENT);
                SetTextColor(item->hDC, RGB(255, 255, 255));
                HGDIOBJ old = SelectObject(item->hDC, g_uiFont);
                DrawTextW(item->hDC, L"\u27A4", -1, &circle,
                          DT_CENTER | DT_VCENTER | DT_SINGLELINE);
                SelectObject(item->hDC, old);
            } else {
                Graphics graphics(item->hDC);
                graphics.SetSmoothingMode(SmoothingModeAntiAlias);
                Pen pen(Color(255, 255, 255, 255), 1.6f);
                const REAL cx = (circle.left + circle.right) / 2.0f;
                const REAL cy = (circle.top + circle.bottom) / 2.0f;
                const REAL left = cx - 8.0f;
                const REAL top = cy - 11.0f;
                const REAL right = cx + 8.0f;
                const REAL bottom = cy + 11.0f;
                PointF points[5] = {
                    PointF(left, top),
                    PointF(cx + 2.0f, top),
                    PointF(right, top + 6.0f),
                    PointF(right, bottom),
                    PointF(left, bottom)
                };
                graphics.DrawPolygon(&pen, points, 5);
                graphics.DrawLine(&pen, PointF(cx + 2.0f, top), PointF(cx + 2.0f, top + 6.0f));
                graphics.DrawLine(&pen, PointF(cx + 2.0f, top + 6.0f), PointF(right, top + 6.0f));
            }
            return TRUE;
        }
        if (wParam == 1015) {
            auto* item = reinterpret_cast<DRAWITEMSTRUCT*>(lParam);
            HBRUSH background = CreateSolidBrush(RGB(255, 255, 255));
            FillRect(item->hDC, &item->rcItem, background);
            DeleteObject(background);
            SetBkMode(item->hDC, TRANSPARENT);
            HGDIOBJ old = SelectObject(item->hDC, g_uiFont);
            RECT close = item->rcItem;
            close.right = close.left + 26;
            SetTextColor(item->hDC, kPurple);
            DrawTextW(item->hDC, L"\u00D7", -1, &close, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
            std::wstring label = g_chipName;
            RECT text = item->rcItem;
            text.left += 30;
            SetTextColor(item->hDC, kInk);
            DrawTextW(item->hDC, label.c_str(), -1, &text,
                      DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
            SelectObject(item->hDC, old);
            return TRUE;
        }
        break;
    case WM_APP_RECREATE_INPUT: {
        HWND old = g_input;
        CreateChatInput(hwnd);
        Layout(hwnd);
        ShowWindow(g_input, SW_SHOW);
        DestroyWindow(old);
        SetFocus(g_assistantLog);
        return 0;
    }
    case WM_APP_TEST_SEND:
        SendChat(hwnd);
        return 0;
    case WM_HOTKEY:
        if (wParam == 101) {
            SendChat(hwnd);
        } else if (wParam == 102) {
            PostMessageW(hwnd, WM_COMMAND, MAKEWPARAM(1014, BN_CLICKED), 0);
        }
        return 0;
    case WM_TIMER:
        if (wParam == 7) {
            const bool control = (GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0;
            const bool alt = (GetAsyncKeyState(VK_MENU) & 0x8000) != 0;
            const bool enter = (GetAsyncKeyState(VK_RETURN) & 0x8000) != 0;
            const HWND foreground = GetForegroundWindow();
            DWORD foregroundPid = 0;
            if (foreground) GetWindowThreadProcessId(foreground, &foregroundPid);
            const bool ours = foreground == g_mainHwnd || IsChild(g_mainHwnd, foreground) ||
                              foregroundPid == GetCurrentProcessId();
            if (g_mainHwnd && ours && enter && (control || alt)) {
                if (!g_hotkeyDown) {
                    g_hotkeyDown = true;
                    if (control) {
                        SendChat(hwnd);
                    } else {
                        PostMessageW(hwnd, WM_COMMAND, MAKEWPARAM(1014, BN_CLICKED), 0);
                    }
                }
            } else if (!enter) {
                g_hotkeyDown = false;
            }
        }
        return 0;
    case WM_HSCROLL:
        if (reinterpret_cast<HWND>(lParam) == g_frameSlider) {
            const int value = static_cast<int>(SendMessageW(g_frameSlider, TBM_GETPOS, 0, 0));
            SetWindowTextW(g_frameLabel, (std::wstring(Ui(UiKey::FrameMultiplier)) + std::to_wstring(value) + L"×").c_str());
            return 0;
        }
        if (reinterpret_cast<HWND>(lParam) == g_paramSlider) {
            const int value = static_cast<int>(SendMessageW(g_paramSlider, TBM_GETPOS, 0, 0));
            if (g_choicePanel) {
                SetWindowTextW(g_paramLabel, (std::wstring(Ui(UiKey::Aging)) + L" " + std::to_wstring(value) + L"%").c_str());
                return 0;
            }
            const std::wstring title = g_paramCommand == L"/vhs" ? L"q · vhs化：" : (g_paramCommand == L"/compress" ? L"crush · 压片：" : L"age · 包浆：");
            wchar_t number[16]{};
            swprintf_s(number, L"%.2f", value / 100.0);
            SetWindowTextW(g_paramLabel, (title + number).c_str());
            return 0;
        }
        break;
    case WM_COMMAND:
        if (LOWORD(wParam) >= 3000 && LOWORD(wParam) < 3000 + static_cast<int>(std::size(kLanguages))) {
            const std::wstring oldHint = KeyHintText();
            g_language = kLanguages[LOWORD(wParam) - 3000];
            ApplyLanguage(hwnd, oldHint);
            return 0;
        }
        if (LOWORD(wParam) == 1001 && HIWORD(wParam) == BN_CLICKED) {
            wchar_t key[512]{};
            GetWindowTextW(g_key, key, 512);
            if (wcscmp(key, KeyHintText().c_str()) == 0 || key[0] == 0) {
                MessageBoxW(hwnd, Ui(UiKey::NeedKey), kTitle, MB_ICONWARNING);
                return 0;
            }
            const std::string body = "{\"key\":\"" + JsonEscape(ToUtf8(std::wstring(key))) +
                                     "\",\"language\":\"" + JsonEscape(ToUtf8(g_language)) + "\"}";
            g_loadingMode = true;
            g_loadingStep = 1;
            ShowWindow(g_key, SW_HIDE);
            ShowWindow(g_langButton, SW_HIDE);
            ShowWindow(g_langLabel, SW_HIDE);
            ShowWindow(g_arrow, SW_HIDE);
            InvalidateRect(hwnd, nullptr, TRUE);
            std::thread([hwnd, body]() {
                for (int i = 0; i < 1800 && !g_sidecar.ready(); ++i) Sleep(100);
                if (!g_sidecar.ready()) {
                    PostMessageW(hwnd, WM_APP_BOOT_ERR, 0,
                                 reinterpret_cast<LPARAM>(new std::wstring(Ui(UiKey::SidecarTimeout))));
                    return;
                }
                g_loadingStep = 2;
                InvalidateRect(hwnd, nullptr, TRUE);
                std::string response;
                std::string error;
                if (!g_sidecar.Post("/bootstrap", body, response, error)) {
                    PostMessageW(hwnd, WM_APP_BOOT_ERR, 0,
                                 reinterpret_cast<LPARAM>(new std::wstring(ToWide(error))));
                    return;
                }
                const std::string content = JsonField(response, "content");
                const std::string problem = JsonField(response, "error");
                if (content.empty()) {
                    PostMessageW(hwnd, WM_APP_BOOT_ERR, 0,
                                 reinterpret_cast<LPARAM>(new std::wstring(ToWide(problem))));
                    return;
                }
                g_loadingStep = 3;
                InvalidateRect(hwnd, nullptr, TRUE);
                PostMessageW(hwnd, WM_APP_BOOT_OK, 0,
                             reinterpret_cast<LPARAM>(new std::wstring(ToWide(content))));
            }).detach();
            return 0;
        }
        if (LOWORD(wParam) == 1014) {
            std::vector<wchar_t> buffer(64 * 1024, 0);
            OPENFILENAMEW dialog{};
            dialog.lStructSize = sizeof(dialog);
            dialog.hwndOwner = hwnd;
            dialog.lpstrFile = buffer.data();
            dialog.nMaxFile = static_cast<DWORD>(buffer.size());
            dialog.lpstrFilter = L"All Files*.*";
            dialog.lpstrTitle = Ui(UiKey::AddAttachment);
            dialog.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_ALLOWMULTISELECT | OFN_EXPLORER;
            if (GetOpenFileNameW(&dialog)) {
                std::vector<std::wstring> selected;
                std::wstring first(buffer.data());
                const wchar_t* cursor = buffer.data() + first.size() + 1;
                if (*cursor) {
                    while (*cursor) {
                        std::wstring name(cursor);
                        if (!name.empty()) selected.push_back(first + L"\\" + name);
                        cursor += name.size() + 1;
                    }
                } else {
                    selected.push_back(first);
                }
                if (selected.size() > 32) selected.resize(32);
                std::string body = "{\"paths\":[";
                for (size_t index = 0; index < selected.size(); ++index) {
                    if (index) body += ",";
                    body += "\"" + JsonEscape(ToUtf8(selected[index])) + "\"";
                }
                body += "]}";
                std::string response;
                std::string error;
                if (g_sidecar.Post("/attachments", body, response, error)) {
                    g_attachmentText = JsonField(response, "text");
                    g_attachmentImageUri = JsonField(response, "image_uri");
                    g_attachmentMeta = ToWide(JsonField(response, "meta"));
                    g_attachmentImageUris = JsonStringArray(response, "image_uris");
                    if (g_attachmentImageUris.empty() && !g_attachmentImageUri.empty()) {
                        g_attachmentImageUris.push_back(g_attachmentImageUri);
                    }
                    g_attachmentPaths = selected;
                    g_attachmentPath = selected.empty() ? L"" : selected.back();
                    g_chipName = selected.empty() ? L"" : FileLabel(selected.front());
                    if (selected.size() > 1) g_chipName += L"  +" + std::to_wstring(selected.size() - 1);
                    SetWindowTextW(g_chip, (L"+" + g_chipName).c_str());
                    ShowWindow(g_chip, SW_SHOW);
                    Layout(hwnd);
                } else {
                    MessageBoxW(hwnd, ToWide(error).c_str(), kTitle, MB_ICONERROR);
                }
            }
            return 0;
        }
        if (LOWORD(wParam) == 1003 && HIWORD(wParam) == BN_CLICKED) {
            HMENU menu = CreatePopupMenu();
            for (int i = 0; i < static_cast<int>(std::size(kLanguages)); ++i) {
                AppendMenuW(menu, MF_STRING, 3000 + i, kLanguages[i]);
            }
            RECT rect{};
            GetWindowRect(g_langButton, &rect);
            const int command = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_LEFTALIGN,
                                               rect.left, rect.bottom, 0, hwnd, nullptr);
            DestroyMenu(menu);
            if (command >= 3000) {
                const std::wstring oldHint = KeyHintText();
                g_language = kLanguages[command - 3000];
                ApplyLanguage(hwnd, oldHint);
            }
            return 0;
        }
        if (LOWORD(wParam) == 1012 && HIWORD(wParam) == EN_SETFOCUS) {
            if (g_detachedImc) {
                ImmAssociateContext(g_input, g_detachedImc);
                g_detachedImc = nullptr;
            }
            return 0;
        }
        if (LOWORD(wParam) == 1002) {
            const WORD code = HIWORD(wParam);
            if (code == EN_SETFOCUS) {
                wchar_t current[512]{};
                GetWindowTextW(g_key, current, 512);
                if (wcscmp(current, KeyHintText().c_str()) == 0) SetWindowTextW(g_key, L"");
            } else if (code == EN_KILLFOCUS) {
                wchar_t current[512]{};
                GetWindowTextW(g_key, current, 512);
                if (current[0] == 0) SetWindowTextW(g_key, KeyHintText().c_str());
            }
            return 0;
        }
        if (LOWORD(wParam) == 1016 && HIWORD(wParam) == STN_CLICKED) {
            if (!g_imagePath.empty()) {
                ShellExecuteW(hwnd, L"open", g_imagePath.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
            }
            return 0;
        }
        if (LOWORD(wParam) == 1015 && HIWORD(wParam) == BN_CLICKED) {
            g_attachmentText.clear();
            g_attachmentPath.clear();
            g_attachmentPaths.clear();
            g_chipName.clear();
            ShowWindow(g_chip, SW_HIDE);
            Layout(hwnd);
            return 0;
        }
        if (LOWORD(wParam) == 1013) {
            SendChat(hwnd);
            return 0;
        }
        if (LOWORD(wParam) == 1021) {
            const int value = static_cast<int>(SendMessageW(g_paramSlider, TBM_GETPOS, 0, 0));
            if (g_choicePanel) {
                const bool vhs = SendMessageW(g_choiceVhs, BM_GETCHECK, 0, 0) == BST_CHECKED;
                g_paramCommand = vhs ? L"/vhs化" : L"/压片";
            }
            wchar_t command[64]{};
            swprintf_s(command, L"%s %.2f", g_paramCommand.c_str(), value / 100.0);
            SetWindowTextW(g_input, command);
            g_paramPanel = false;
            g_choicePanel = false;
            ShowWindow(g_choiceVhs, SW_HIDE);
            ShowWindow(g_choiceDig, SW_HIDE);
            ShowWindow(g_paramLabel, SW_HIDE);
            ShowWindow(g_paramSlider, SW_HIDE);
            ShowWindow(g_paramApply, SW_HIDE);
            SendChat(hwnd);
            return 0;
        }
        if (LOWORD(wParam) == 1019) {
            const int value = static_cast<int>(SendMessageW(g_frameSlider, TBM_GETPOS, 0, 0));
            SetWindowTextW(g_input, (L"/frame " + std::to_wstring(value)).c_str());
            g_framePanel = false;
            ShowWindow(g_frameLabel, SW_HIDE);
            ShowWindow(g_frameSlider, SW_HIDE);
            ShowWindow(g_frameApply, SW_HIDE);
            SendChat(hwnd);
            return 0;
        }
        break;
    case WM_APP_CHAT_TEST:
        EnterChat(hwnd, "# Markdown Test\n\n**Bold** and *italic* and `code`.\n\n---\n\n| Name | Value |\n| --- | --- |\n| one | 1 |\n| two | 2 |\n\n- item one\n- item two\n\n![avatar](H:/Github/twilight-harness-for-deepseek-api/native/assets/avatar_twilight.png)");
        g_reasoningMarkdown = "# Think\n\n- 检查上下文\n- 对比表格\n\n| 步骤 | 状态 |\n| --- | --- |\n| 读取 | 完成 |\n| 校验 | 进行中 |\n\n---\n\n![think](H:/Github/twilight-harness-for-deepseek-api/native/assets/avatar_twilight.png)";
        RenderReasoning();
        RenderAssistant();
        g_lastUserPrompt = L"用户让我展示 ABCDEFG 内容。";
        g_attachmentText = "这是附件文本预览 ABCDEFG";
        g_chipName = L"sample-attachment.png";
        ShowWindow(g_chip, SW_SHOW);
        Layout(hwnd);
        return 0;
    case WM_APP_BOOT_OK: {
        auto* content = reinterpret_cast<std::wstring*>(lParam);
        if (content) {
            g_loadingMode = false;
            EnterChat(hwnd, ToUtf8(*content));
            delete content;
        }
        return 0;
    }
    case WM_APP_BOOT_ERR: {
        auto* error = reinterpret_cast<std::wstring*>(lParam);
        g_loadingMode = false;
        ShowWindow(g_key, SW_SHOW);
        ShowWindow(g_langButton, SW_SHOW);
        ShowWindow(g_langLabel, SW_SHOW);
        ShowWindow(g_arrow, SW_SHOW);
        InvalidateRect(hwnd, nullptr, TRUE);
        if (error) {
            MessageBoxW(hwnd, error->c_str(), kTitle, MB_ICONERROR);
            delete error;
        }
        return 0;
    }
    case WM_APP_REASONING: {
        auto* chunk = reinterpret_cast<std::wstring*>(lParam);
        if (chunk) {
            g_reasoningMarkdown += ToUtf8(*chunk);
            delete chunk;
            RenderReasoning();
        }
        return 0;
    }
    case WM_APP_CHUNK: {
        auto* chunk = reinterpret_cast<std::wstring*>(lParam);
        if (!g_pendingUserPrompt.empty()) {
            std::string userBlock = ToUtf8(g_pendingUserPrompt);
            if (!g_pendingAttachmentLabel.empty()) {
                { const std::wstring metaText = g_attachmentMeta.empty() ? (std::wstring(Ui(UiKey::Attachment)) + g_pendingAttachmentLabel) : g_attachmentMeta;
                  userBlock = "[[META:" + ToUtf8(metaText) + "]]\n" + userBlock;
                  g_attachmentMeta.clear(); }
            }
                if (!g_attachmentImageUri.empty()) {
                    userBlock += "\n\n[[GALLERY:" + g_attachmentImageUri + "]]";
                    g_attachmentImageUri.clear();
                }
            g_assistantMarkdown += "\n\n" + AvatarMarker(L"user", L"avatar_user.png") + "\n" + userBlock + "\n\n";
            g_pendingUserPrompt.clear();
            g_pendingAttachmentLabel.clear();
            RenderAssistant();
        }
        if (chunk) {
            g_streamingAnswer += ToUtf8(*chunk);
            SpeakStreamingSentences(false);
            std::string visible = g_streamingAnswer;
            const auto command = visible.find("[[LOCAL_TOOL");
            if (command != std::string::npos) visible.erase(command);
            const auto partial = visible.rfind("[[");
            if (partial != std::string::npos && visible.size() - partial < 12) visible.erase(partial);
            const std::string preview = g_assistantMarkdown + "\n\n" +
                                        AvatarMarker(L"assistant", L"avatar_green.png") + "\n" +
                                        visible;
            SetChatViewMarkdown(g_assistantLog, preview);
        }
        delete chunk;
        return 0;
    }
    case WM_APP_DONE: {
        if (!g_pendingUserPrompt.empty()) {
            std::string userBlock = ToUtf8(g_pendingUserPrompt);
            if (!g_pendingAttachmentLabel.empty()) {
                { const std::wstring metaText = g_attachmentMeta.empty() ? (std::wstring(Ui(UiKey::Attachment)) + g_pendingAttachmentLabel) : g_attachmentMeta;
                  userBlock = "[[META:" + ToUtf8(metaText) + "]]\n" + userBlock;
                  g_attachmentMeta.clear(); }
            }
                if (!g_attachmentImageUri.empty()) {
                    userBlock += "\n\n[[GALLERY:" + g_attachmentImageUri + "]]";
                    g_attachmentImageUri.clear();
                }
            g_assistantMarkdown += "\n\n" + AvatarMarker(L"user", L"avatar_user.png") + "\n" + userBlock + "\n\n";
            g_pendingUserPrompt.clear();
            g_pendingAttachmentLabel.clear();
        }
        auto* answer = reinterpret_cast<std::wstring*>(lParam);
        if (answer) {
            const std::string answerUtf8 = ToUtf8(*answer);
            g_messages.emplace_back("assistant", answerUtf8);
            SpeakStreamingSentences(true);
            g_assistantMarkdown += "\n\n" + AvatarMarker(L"assistant", L"avatar_green.png") + "\n" + answerUtf8;
            delete answer;
            RenderAssistant();
        }
        EnableWindow(g_send, TRUE);
        g_twilightImage = g_imgGreen; InvalidateRect(hwnd, nullptr, FALSE);
        if (g_progressWindow) {
            KillTimer(g_progressWindow, 9);
            ShowWindow(g_progressWindow, SW_HIDE);
            g_progressTasks.clear();
        }
        return 0;
    }
    case WM_APP_PROGRESS: {
        auto* update = reinterpret_cast<ProgressUpdate*>(lParam);
        if (update) {
            if (update->label != g_lastToolLabel) {
                g_lastToolLabel = update->label;
                g_reasoningMarkdown += "\n- 工具调用：" + ToUtf8(update->label);
                RenderReasoning();
            }
            ApplyProgressUpdate(*update);
            delete update;
        }
        return 0;
    }
    case WM_APP_TOOL: {
        auto* summary = reinterpret_cast<std::wstring*>(lParam);
        delete summary;
        return 0;
    }
    case WM_APP_ERROR: {
        auto* error = reinterpret_cast<std::wstring*>(lParam);
        if (error) {
            AppendText(g_assistantLog, std::wstring(Ui(UiKey::Error)) + *error);
            delete error;
        }
        EnableWindow(g_send, TRUE);
        g_twilightImage = g_imgGray; InvalidateRect(hwnd, nullptr, FALSE);
        if (g_progressWindow) {
            KillTimer(g_progressWindow, 9);
            ShowWindow(g_progressWindow, SW_HIDE);
            g_progressTasks.clear();
        }
        return 0;
    }
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    default:
        return DefWindowProcW(hwnd, msg, wParam, lParam);
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

} // namespace

HWND g_benchButton = nullptr;
std::vector<std::wstring> g_benchLines;
bool g_benchRunning = false;
constexpr UINT WM_APP_BENCH_RESULT = WM_APP + 21;

void SplitBenchmarkReport(const std::wstring& text) {
    g_benchLines.clear();
    std::wstring line;
    for (wchar_t ch : text) {
        if (ch == L'\n') {
            if (!line.empty() && line.back() == L'\r') line.pop_back();
            g_benchLines.push_back(line);
            line.clear();
        } else {
            line.push_back(ch);
        }
    }
    if (!line.empty()) g_benchLines.push_back(line);
    if (g_benchLines.empty()) g_benchLines.push_back(L"Benchmark returned no output.");
}

void DrawRoundRect(Graphics& graphics, RectF area, REAL radius, Brush* fill, Pen* border = nullptr) {
    GraphicsPath path;
    path.AddArc(area.X, area.Y, radius * 2, radius * 2, 180, 90);
    path.AddArc(area.GetRight() - radius * 2, area.Y, radius * 2, radius * 2, 270, 90);
    path.AddArc(area.GetRight() - radius * 2, area.GetBottom() - radius * 2, radius * 2, radius * 2, 0, 90);
    path.AddArc(area.X, area.GetBottom() - radius * 2, radius * 2, radius * 2, 90, 90);
    path.CloseFigure();
    if (fill) graphics.FillPath(fill, &path);
    if (border) graphics.DrawPath(border, &path);
}

void DrawBenchmark(HDC dc, RECT client) {
    DrawHero(dc, client);
    Graphics graphics(dc);
    graphics.SetSmoothingMode(SmoothingModeAntiAlias);
    graphics.SetTextRenderingHint(TextRenderingHintClearTypeGridFit);
    SolidBrush rightBrush(Color(255, 244, 231, 255));
    graphics.FillRectangle(&rightBrush, RectF(static_cast<REAL>(kLeftWidth), 0,
                                               static_cast<REAL>(client.right - kLeftWidth),
                                               static_cast<REAL>(client.bottom)));
    SolidBrush white(Color(255, 250, 247, 255));
    SolidBrush ink(Color(255, 35, 22, 48));
    Font title(L"Source Han Sans", 25.0f, FontStyleBold);
    Font version(L"Consolas", 13.0f);
    Font mono(L"Consolas", 14.0f);
    graphics.DrawString(L"Twilight Harness", -1, &title, PointF(24, 18), &white);
    graphics.DrawString(L"rc-261106-benchmark", -1, &version, PointF(24, 58), &white);

    const int x = kLeftWidth + 26;
    const int top = 96;
    const int width = client.right - x - 24;
    const int height = client.bottom - top - 24;
    SolidBrush panelFill(Color(255, 255, 255, 255));
    Pen panelBorder(Color(255, 210, 190, 228), 1.0f);
    DrawRoundRect(graphics, RectF(static_cast<REAL>(x), static_cast<REAL>(top),
                                  static_cast<REAL>(width), static_cast<REAL>(height)), 10.0f,
                  &panelFill, &panelBorder);

    int y = top + 14;
    const int lineHeight = 23;
    const int maxY = top + height - 12;
    for (const auto& line : g_benchLines) {
        if (y + lineHeight > maxY) break;
        RectF lineRect(static_cast<REAL>(x + 16), static_cast<REAL>(y),
                       static_cast<REAL>(width - 32), static_cast<REAL>(lineHeight));
        graphics.DrawString(line.c_str(), -1, &mono, lineRect, nullptr, &ink);
        y += lineHeight;
    }
    if (g_benchRunning) {
        SolidBrush muted(Color(255, 130, 110, 150));
        Font smallFont(L"Source Han Sans", 12.0f);
        graphics.DrawString(L"Running...", -1, &smallFont,
                            PointF(static_cast<REAL>(x + 16), static_cast<REAL>(top + height - 26)), &muted);
    }
}

LRESULT CALLBACK BenchmarkProc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
    switch (message) {
    case WM_CREATE: {
        g_benchLines = {
            L"Devices detected:",
            L"CPU: detecting...",
            L"GPU: detecting...",
            L"RAM: detecting...",
            L"",
            L"Click Run to start the local benchmark.",
            L"",
            L"Qwen-Image-2.1-Turbo INT4",
            L"Real-ESRGAN / NTSC-QT / ffmpeg",
        };
        g_benchButton = CreateWindowExW(0, L"BUTTON", L"",
                                        WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                                        kLeftWidth + 26, 18, 300, 52, hwnd,
                                        reinterpret_cast<HMENU>(3002), g_instance, nullptr);
        return 0;
    }
    case WM_SIZE: {
        RECT rc{}; GetClientRect(hwnd, &rc);
        if (g_benchButton) MoveWindow(g_benchButton, kLeftWidth + 26, 18,
                                      rc.right - kLeftWidth - 50, 52, TRUE);
        InvalidateRect(hwnd, nullptr, TRUE);
        return 0;
    }
    case WM_DRAWITEM: {
        auto* item = reinterpret_cast<DRAWITEMSTRUCT*>(lparam);
        if (item && item->CtlID == 3002) {
            Graphics graphics(item->hDC);
            graphics.SetSmoothingMode(SmoothingModeAntiAlias);
            SolidBrush purple(Color(255, 123, 47, 190));
            SolidBrush white(Color(255, 255, 255, 255));
            const RECT& r = item->rcItem;
            DrawRoundRect(graphics, RectF(static_cast<REAL>(r.left), static_cast<REAL>(r.top),
                                          static_cast<REAL>(r.right - r.left), static_cast<REAL>(r.bottom - r.top)),
                          8.0f, &purple);
            Font font(L"Source Han Sans", 18.0f, FontStyleRegular);
            StringFormat format;
            format.SetAlignment(StringAlignmentCenter);
            format.SetLineAlignment(StringAlignmentCenter);
            RectF area(static_cast<REAL>(r.left), static_cast<REAL>(r.top),
                       static_cast<REAL>(r.right - r.left), static_cast<REAL>(r.bottom - r.top));
            graphics.DrawString(g_benchRunning ? L"Running..." : L"Run", -1, &font, area, &format, &white);
            return TRUE;
        }
        break;
    }
    case WM_PAINT: {
        PAINTSTRUCT ps{};
        HDC dc = BeginPaint(hwnd, &ps);
        RECT rc{}; GetClientRect(hwnd, &rc);
        DrawBenchmark(dc, rc);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wparam) == 3002 && !g_benchRunning) {
            g_benchRunning = true;
            SetWindowTextW(g_benchButton, L"Running...");
            InvalidateRect(g_benchButton, nullptr, TRUE);
            g_benchLines = {
                L"Devices detected:",
                L"CPU: reading hardware index...",
                L"GPU: reading hardware index...",
                L"RAM: reading hardware index...",
                L"",
                L"Testing Qwen-Image-2.1-Turbo INT4...",
                L"Testing Real-ESRGAN...",
                L"Testing NTSC-QT/VHS...",
                L"",
                L"Please wait; first Qwen load can take up to a minute.",
            };
            InvalidateRect(hwnd, nullptr, TRUE);
            StartSidecarAsync();
            std::thread([]() {
                for (int i = 0; i < 240 && !g_sidecar.ready(); ++i) Sleep(250);
                std::string response, error;
                if (!g_sidecar.ready()) {
                    error = "Sidecar not ready";
                } else if (!g_sidecar.Post("/benchmark.json", "{}", response, error)) {
                    if (error.empty()) error = "benchmark request failed";
                }
                std::wstring text = error.empty() ? ToWide(JsonField(response, "report"))
                                                  : (L"Benchmark error:\n" + ToWide(error));
                if (text.empty()) text = L"Benchmark returned no output.";
                PostMessageW(g_mainHwnd, WM_APP_BENCH_RESULT, 0,
                             reinterpret_cast<LPARAM>(new std::wstring(text)));
            }).detach();
            return 0;
        }
        break;
    case WM_APP_BENCH_RESULT: {
        auto* text = reinterpret_cast<std::wstring*>(lparam);
        if (text) {
            SplitBenchmarkReport(*text);
            delete text;
        }
        g_benchRunning = false;
        SetWindowTextW(g_benchButton, L"Run");
        InvalidateRect(hwnd, nullptr, TRUE);
        InvalidateRect(g_benchButton, nullptr, TRUE);
        return 0;
    }
    case WM_ERASEBKGND:
        return 1;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, message, wparam, lparam);
}

int RunBenchmarkClient(HINSTANCE instance, int show) {
    const wchar_t* cls = L"TwilightBenchmarkWindow";
    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.style = CS_HREDRAW | CS_VREDRAW;
    wc.lpfnWndProc = BenchmarkProc;
    wc.hInstance = instance;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hIcon = LoadIconW(instance, MAKEINTRESOURCEW(101));
    wc.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
    wc.lpszClassName = cls;
    RegisterClassExW(&wc);
    HWND hwnd = CreateWindowExW(0, cls, L"Twilight Harness Benchmark", WS_OVERLAPPEDWINDOW,
                                60, 40, 1180, 840, nullptr, nullptr, instance, nullptr);
    if (!hwnd) return 1;
    g_mainHwnd = hwnd;
    ShowWindow(hwnd, show);
    UpdateWindow(hwnd);
    MSG msg{};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    return 0;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int show) {
    g_instance = instance;
    g_variant = ReadVariant();
    SetEnvironmentVariableW(L"TWILIGHT_VARIANT", ToWide(g_variant).c_str());
    if (g_variant == "debug") {
        AllocConsole();
        SetConsoleTitleW(L"Twilight Harness Debug Console");
    }
    std::wstring startupKey;
    bool startupAuto = false;
    if (wcsstr(GetCommandLineW(), L"--debug-backend")) {
        SetEnvironmentVariableW(L"TWILIGHT_DEBUG_BACKEND", L"1");
    }
    int argumentCount = 0;
    LPWSTR* arguments = CommandLineToArgvW(GetCommandLineW(), &argumentCount);
    if (arguments) {
        for (int i = 1; i < argumentCount; ++i) {
            const std::wstring arg = arguments[i];
            if (arg.rfind(L"--api-key=", 0) == 0) startupKey = arg.substr(10);
            if (arg == L"--auto-start") startupAuto = true;
        }
        LocalFree(arguments);
    }
    if (wcsstr(GetCommandLineW(), L"--sidecar-test")) {
        std::wstring root = ExeDir();
        wchar_t env[1024]{};
        std::wstring python = root + L"\\python\\Scripts\\python.exe";
        std::wstring script = root + L"\\sidecar\\server.py";
        if (GetEnvironmentVariableW(L"TWILIGHT_SIDECAR_PYTHON", env, 1024) > 0) python = env;
        if (GetEnvironmentVariableW(L"TWILIGHT_SIDECAR_SCRIPT", env, 1024) > 0) script = env;
        bool ok = g_sidecar.Start(python, script, root, root + L"\\sidecar\\handshake.json");
        std::string response, error;
        Sleep(1200);
        if (ok) g_sidecar.Get("/health", response, error);
        std::ofstream out("sidecar-test.txt");
        out << "start=" << ok << " port=" << g_sidecar.port() << "\n" << response << "\n" << error << "\n";
        g_sidecar.Stop();
        return ok ? 0 : 2;
    }
    if (auto user32 = GetModuleHandleW(L"user32.dll")) {
        using SetDpiContext = BOOL(WINAPI*)(HANDLE);
        auto setContext = reinterpret_cast<SetDpiContext>(GetProcAddress(user32, "SetProcessDpiAwarenessContext"));
        if (setContext) {
            setContext(reinterpret_cast<HANDLE>(-4)); // PER_MONITOR_AWARE_V2
        } else {
            SetProcessDPIAware();
        }
    }
    INITCOMMONCONTROLSEX commonControls{sizeof(INITCOMMONCONTROLSEX), ICC_BAR_CLASSES | ICC_STANDARD_CLASSES};
    InitCommonControlsEx(&commonControls);
    GdiplusStartupInput gdiplusInput;
    GdiplusStartup(&g_gdiplus, &gdiplusInput, nullptr);
    RegisterBundledFonts();
    LoadHero();
    if (g_variant == "benchmark") {
        return RunBenchmarkClient(instance, show);
    }

    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.style = CS_HREDRAW | CS_VREDRAW;
    wc.lpfnWndProc = WindowProc;
    wc.hInstance = instance;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hIcon = LoadIconW(instance, MAKEINTRESOURCEW(101));
    wc.hIconSm = static_cast<HICON>(LoadImageW(instance, MAKEINTRESOURCEW(101), IMAGE_ICON, 16, 16, LR_DEFAULTCOLOR));
    wc.hbrBackground = nullptr;
    wc.lpszClassName = kClass;
    RegisterClassExW(&wc);

    HWND hwnd = CreateWindowExW(0, kClass, kTitle, WS_OVERLAPPEDWINDOW,
                                CW_USEDEFAULT, CW_USEDEFAULT, 1120, 760,
                                nullptr, nullptr, instance, nullptr);
    if (!hwnd) return 1;
    g_mainHwnd = hwnd;
    if (wcsstr(GetCommandLineW(), L"--effects-test")) ShowEffectsWindowFor(L"aging");
    if (!startupKey.empty() && g_key) SetWindowTextW(g_key, startupKey.c_str());
    ShowWindow(hwnd, show);
    UpdateWindow(hwnd);
    if (startupAuto) PostMessageW(hwnd, WM_COMMAND, MAKEWPARAM(1001, BN_CLICKED), 0);
    if (wcsstr(GetCommandLineW(), L"--chat-test")) {
        PostMessageW(hwnd, WM_APP_CHAT_TEST, 0, 0);
    }

    ACCEL accelerators[] = {
        {FVIRTKEY | FCONTROL, VK_RETURN, 1013},
        {FVIRTKEY | FALT, VK_RETURN, 1014},
    };
    HACCEL accelTable = CreateAcceleratorTableW(accelerators, 2);

    g_speechThread = std::thread(SpeechWorker);

    MSG msg{};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        if (!accelTable || !TranslateAcceleratorW(hwnd, accelTable, &msg)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
    if (accelTable) DestroyAcceleratorTable(accelTable);
    if (g_keyboardHook) UnhookWindowsHookEx(g_keyboardHook);
    UnregisterHotKey(hwnd, 101);
    UnregisterHotKey(hwnd, 102);
    {
        std::lock_guard<std::mutex> lock(g_speechMutex);
        g_speechStop = true;
    }
    g_speechCv.notify_all();
    if (g_speechThread.joinable()) g_speechThread.join();
    if (g_hero) delete g_hero;
    if (g_panel) delete g_panel;
    if (g_titleFont) DeleteObject(g_titleFont);
    if (g_versionFont) DeleteObject(g_versionFont);
    if (g_uiFont) DeleteObject(g_uiFont);
    if (g_langLabelFont) DeleteObject(g_langLabelFont);
    if (g_privateFonts > 0) {
        RemoveFontResourceExW(AssetPath(L"SourceHanSerifSC-Regular.otf").c_str(), FR_PRIVATE, nullptr);
    }
    GdiplusShutdown(g_gdiplus);
    return static_cast<int>(msg.wParam);
}
