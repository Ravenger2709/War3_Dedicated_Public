/* Diagnostic only: DllMain runs on LoadLibrary; no resolved export is called. */
#include <windows.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    const char *names[] = {"Initialize", "ApplyPatch", "ApplyPatchSuspended"};
    unsigned int i;
    if (argc != 2 || (strcmp(argv[1], "iccwc3.icc") && strcmp(argv[1], "Reconnect.dll") && strcmp(argv[1], "iccscbn.icc"))) return 64;
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    printf("LOAD_ONLY target=%s pointer_bits=%u\n", argv[1], (unsigned int)(8*sizeof(void*)));
    fflush(stdout);
    HMODULE h = LoadLibraryA(argv[1]);
    if (!h) { printf("LOAD_FAILED win32_error=%lu\n", GetLastError()); fflush(stdout); return 1; }
    printf("LOAD_OK\n");
    for (i=0; i<sizeof(names)/sizeof(names[0]); i++) printf("EXPORT name=%s present=%d invoked=0\n", names[i], GetProcAddress(h,names[i]) != NULL);
    fflush(stdout);
    Sleep(3000);
    /* Process exit handles unload; no module-specific teardown ABI is assumed. */
    return 0;
}
