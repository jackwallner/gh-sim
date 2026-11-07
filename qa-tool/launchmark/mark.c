// Inserted into an app on the simulator by qa-gate. Constructors of an inserted
// library run once dyld has loaded every dependency, so this time is the end of
// the simulator's pre-main cost, where the app's own launch work begins.
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

__attribute__((constructor)) static void qa_launch_mark(void) {
    const char *dir = getenv("TMPDIR");
    if (!dir) return;
    char path[1024];
    snprintf(path, sizeof path, "%s/qa-launch-mark", dir);
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    FILE *file = fopen(path, "w");
    if (!file) return;
    fprintf(file, "%ld.%09ld\n", (long)now.tv_sec, now.tv_nsec);
    fclose(file);
}
