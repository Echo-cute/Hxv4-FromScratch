// inject_launch.c -- 32-bit launcher: start game suspended, inject a DLL, resume.
// 用於 dynamic_dump.py：載入 KrkrDump.dll 後再讓遊戲初始化，避開 Detours loader
// 對某些遊戲 DRM 的干擾。
// Build (x86): cl /nologo /MT /O2 inject_launch.c /Fe:inject_launch.exe
#include <windows.h>
#include <stdio.h>

static void build_cmdline(wchar_t *out, int argc, wchar_t **argv, int start)
{
    out[0] = 0;
    for (int i = start; i < argc; i++) {
        if (i > start) wcscat(out, L" ");
        wcscat(out, L"\"");
        wcscat(out, argv[i]);
        wcscat(out, L"\"");
    }
}

int wmain(int argc, wchar_t **argv)
{
    if (argc < 3) {
        fwprintf(stderr,
                 L"usage: inject_launch.exe <dll> <game.exe> [args...]\n");
        return 1;
    }
    wchar_t dll[MAX_PATH * 4];
    wcsncpy(dll, argv[1], MAX_PATH * 4 - 1);
    dll[MAX_PATH * 4 - 1] = 0;
    wchar_t exe[MAX_PATH * 4];
    wcsncpy(exe, argv[2], MAX_PATH * 4 - 1);
    exe[MAX_PATH * 4 - 1] = 0;
    wchar_t dir[MAX_PATH * 4];
    wcsncpy(dir, exe, MAX_PATH * 4 - 1);
    dir[MAX_PATH * 4 - 1] = 0;
    wchar_t *p = wcsrchr(dir, L'\\');
    if (p) *p = 0;

    wchar_t cmd[32768];
    build_cmdline(cmd, argc, argv, 2);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    if (!CreateProcessW(exe, cmd, NULL, NULL, FALSE, CREATE_SUSPENDED,
                        NULL, dir, &si, &pi)) {
        fwprintf(stderr, L"CreateProcess failed: %lu\n", GetLastError());
        return 2;
    }

    SIZE_T bytes = (wcslen(dll) + 1) * sizeof(wchar_t);
    void *remote = VirtualAllocEx(pi.hProcess, NULL, bytes,
                                  MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (remote) {
        WriteProcessMemory(pi.hProcess, remote, dll, bytes, NULL);
        HMODULE k32 = GetModuleHandleW(L"kernel32.dll");
        void *loadlib = (void *)GetProcAddress(k32, "LoadLibraryW");
        HANDLE th = CreateRemoteThread(pi.hProcess, NULL, 0,
                                       (LPTHREAD_START_ROUTINE)loadlib,
                                       remote, 0, NULL);
        if (th) {
            WaitForSingleObject(th, 10000);
            CloseHandle(th);
        }
    }

    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
