// Read only war3map.w3i via the same native StormLib used by GHost++.
#include <cstdio>
#include <vector>
#define __STORMLIB_SELF__
#include <stormlib/StormLib.h>
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    HANDLE archive = NULL, file = NULL;
    if (!SFileOpenArchive(argv[1], 0, MPQ_OPEN_FORCE_MPQ_V1, &archive)) return 3;
    if (!SFileOpenFileEx(archive, "war3map.w3i", 0, &file)) {
        SFileCloseArchive(archive); return 4;
    }
    DWORD size = SFileGetFileSize(file, NULL), read = 0;
    int status = 5;
    if (size > 0 && size <= 4 * 1024 * 1024) {
        std::vector<unsigned char> data(size);
        if (SFileReadFile(file, data.data(), size, &read, NULL) && read == size)
            status = std::fwrite(data.data(), 1, size, stdout) == size ? 0 : 6;
    }
    SFileCloseFile(file); SFileCloseArchive(archive);
    return status;
}
