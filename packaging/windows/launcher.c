#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR commandLine, int show) {
    (void)instance;
    (void)previous;
    (void)commandLine;
    (void)show;

    wchar_t directory[32768];
    DWORD length = GetModuleFileNameW(NULL, directory, 32768);
    if (length == 0 || length >= 32768) {
        MessageBoxW(NULL, L"The application folder could not be found.", L"Bilingual Manga", MB_OK | MB_ICONERROR);
        return 1;
    }
    while (length > 0 && directory[length - 1] != L'\\' && directory[length - 1] != L'/') {
        directory[--length] = L'\0';
    }
    if (length == 0) {
        MessageBoxW(NULL, L"The application folder is invalid.", L"Bilingual Manga", MB_OK | MB_ICONERROR);
        return 1;
    }
    directory[length - 1] = L'\0';

    wchar_t python[32768];
    wchar_t command[32768];
    if (swprintf(python, 32768, L"%ls\\pythonw.exe", directory) < 0 ||
        swprintf(command, 32768, L"\"%ls\" -m app.launcher", python) < 0) {
        MessageBoxW(NULL, L"The launch command was too long.", L"Bilingual Manga", MB_OK | MB_ICONERROR);
        return 1;
    }

    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.cb = sizeof(startup);

    if (!CreateProcessW(python, command, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, directory, &startup, &process)) {
        MessageBoxW(
            NULL,
            L"The bundled reader runtime could not start. Re-extract the complete ZIP, then try again.",
            L"Bilingual Manga",
            MB_OK | MB_ICONERROR
        );
        return 1;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}
