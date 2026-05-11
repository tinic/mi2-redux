/* adfreplace — replace a file inside an ADF image with a host file.
 *
 * Usage: adfreplace <disk.adf> <name-on-disk> <host-file>
 *
 * Builds against ADFlib (already cloned + built at ~/mi2-redux/tools/adflib).
 * Build:
 *   gcc-15 -O2 -Wall -Wextra -std=c11 \
 *     -I~/mi2-redux/tools/adflib/src \
 *     -L~/mi2-redux/tools/adflib/build/src \
 *     adfreplace.c -ladf -o adfreplace
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <adflib.h>

static int usage(void) {
    fprintf(stderr,
        "Usage: adfreplace <disk.adf> <name-on-disk> <host-file>\n"
        "  Replaces an existing file in the AmigaDOS root directory of <disk.adf>\n"
        "  with the contents of <host-file>. Same name must already exist.\n");
    return 1;
}

int main(int argc, char **argv) {
    if (argc != 4) return usage();
    const char *adf = argv[1];
    const char *target = argv[2];
    const char *src = argv[3];

    /* Read source file into memory */
    FILE *f = fopen(src, "rb");
    if (!f) { perror(src); return 2; }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    void *buf = malloc((size_t)n);
    if (!buf) { fprintf(stderr, "oom\n"); return 2; }
    if (fread(buf, 1, (size_t)n, f) != (size_t)n) { perror("fread"); return 2; }
    fclose(f);

    if (adfLibInit() != ADF_RC_OK) { fprintf(stderr, "adfLibInit failed\n"); return 3; }

    struct AdfDevice *dev = adfDevOpen(adf, ADF_ACCESS_MODE_READWRITE);
    if (!dev) { fprintf(stderr, "open %s failed\n", adf); return 3; }
    if (adfDevMount(dev) != ADF_RC_OK) { fprintf(stderr, "mount failed\n"); return 3; }
    struct AdfVolume *vol = adfVolMount(dev, 0, ADF_ACCESS_MODE_READWRITE);
    if (!vol) { fprintf(stderr, "vol mount failed\n"); return 3; }

    /* Delete existing entry, recreate, write */
    if (adfRemoveEntry(vol, vol->curDirPtr, target) != ADF_RC_OK) {
        fprintf(stderr, "delete %s failed (does it exist?)\n", target);
        return 4;
    }
    struct AdfFile *out = adfFileOpen(vol, target, ADF_FILE_MODE_WRITE);
    if (!out) { fprintf(stderr, "create %s failed\n", target); return 4; }
    uint32_t written = adfFileWrite(out, (uint32_t)n, buf);
    if (written != (uint32_t)n) {
        fprintf(stderr, "write short: %u/%ld\n", written, n);
        adfFileClose(out);
        return 5;
    }
    adfFileClose(out);

    adfVolUnMount(vol);
    adfDevUnMount(dev);
    adfDevClose(dev);
    adfLibCleanUp();

    printf("Replaced %s in %s with %ld bytes from %s\n", target, adf, n, src);
    free(buf);
    return 0;
}
