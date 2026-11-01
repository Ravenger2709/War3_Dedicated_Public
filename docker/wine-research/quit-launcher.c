/* Request normal shutdown of our Launcher only. No login input, vendor calls,
 * process-memory access or forced termination is performed. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <wchar.h>

static DWORD wanted_pid, gui_thread;
static HWND main_window;
static BOOL CALLBACK find_window(HWND hwnd, LPARAM ignored) {
    DWORD pid=0, tid;
    wchar_t title[256];
    (void)ignored;
    tid=GetWindowThreadProcessId(hwnd,&pid);
    if (pid!=wanted_pid || !IsWindowVisible(hwnd)) return TRUE;
    if (!GetWindowTextW(hwnd,title,256) || wcsncmp(title,L"iCCup",5)) return TRUE;
    main_window=hwnd;gui_thread=tid;
    return FALSE;
}
int main(void) {
    PROCESSENTRY32 p={0};
    HANDLE snap=CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS,0),process;
    DWORD exit_code=STILL_ACTIVE;
    const char *method="WM_CLOSE";
    if (snap==INVALID_HANDLE_VALUE) return 64;
    p.dwSize=sizeof(p);
    if (Process32First(snap,&p)) do {
        if (!_stricmp(p.szExeFile,"Launcher.exe")) {wanted_pid=p.th32ProcessID;break;}
    } while (Process32Next(snap,&p));
    CloseHandle(snap);
    if (!wanted_pid) return 65;
    EnumWindows(find_window,0);
    if (!main_window || !gui_thread) return 66;
    process=OpenProcess(SYNCHRONIZE|PROCESS_QUERY_LIMITED_INFORMATION,FALSE,wanted_pid);
    if (!process) return 67;
    if (!PostMessageW(main_window,WM_CLOSE,0,0)) {CloseHandle(process);return 68;}
    if (WaitForSingleObject(process,2000)==WAIT_TIMEOUT) {
        method="WM_QUIT";
        if (!PostThreadMessageW(gui_thread,WM_QUIT,0,0)) {CloseHandle(process);return 69;}
        if (WaitForSingleObject(process,8000)==WAIT_TIMEOUT) {CloseHandle(process);return 70;}
    }
    if (!GetExitCodeProcess(process,&exit_code)) {CloseHandle(process);return 71;}
    CloseHandle(process);
    printf("LAUNCHER_GRACEFUL_EXIT method=%s exit_code=%lu forced_termination=0\n",method,exit_code);
    return exit_code==0 ? 0 : 72;
}
