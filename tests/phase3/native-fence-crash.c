/* Test-only LD_PRELOAD: crash at exact durable-close boundaries. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int fsync(int fd)
{
    static int (*real_fsync)(int);
    static int saw_close_temp;
    static int start_count;
    static int returned_error;
    if (!real_fsync) real_fsync = dlsym(RTLD_NEXT, "fsync");
    const char *mode = getenv("FENCED_TEST_CRASH");
    const char *directory = getenv("FENCED_TEST_DIR");
    char linkpath[64], path[4096];
    snprintf(linkpath, sizeof linkpath, "/proc/self/fd/%d", fd);
    ssize_t n = readlink(linkpath, path, sizeof path - 1);
    if (n > 0) path[n] = '\0'; else path[0] = '\0';
    int is_directory = mode && directory && saw_close_temp && !strcmp(path, directory);
    if (mode && directory && start_count >= 2 && !returned_error &&
        !strcmp(mode, "start_dirsync_error") && !strcmp(path, directory))
    { returned_error = 1; errno = EIO; return -1; }
    if (is_directory && !strcmp(mode, "after_rename")) kill(getpid(), SIGKILL);
    if (is_directory && !returned_error && !strcmp(mode, "dirsync_error"))
    { returned_error = 1; errno = EIO; return -1; }
    int result = real_fsync(fd);
    if (result != 0 || !mode) return result;
    if (strstr(path, "/.start-") && strstr(path, ".tmp")) ++start_count;
    if (strstr(path, "/.close-") && strstr(path, ".tmp"))
    {
        saw_close_temp = 1;
        if (!strcmp(mode, "before_rename")) kill(getpid(), SIGKILL);
    }
    else if (is_directory && !strcmp(mode, "after_dirsync"))
        kill(getpid(), SIGKILL);
    return result;
}
