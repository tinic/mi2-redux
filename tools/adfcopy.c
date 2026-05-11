/* adfcopy — recursively copy a host directory tree into an HDF/ADF.
 *
 * Usage: adfcopy <disk.adf> <host-dir>
 *
 * Walks <host-dir> and reproduces the directory tree at the root of the
 * volume in <disk.adf>. Existing entries with the same name are deleted
 * first (so this is destructive on conflicts).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dirent.h>
#include <sys/stat.h>
#include <sys/types.h>

#include <adflib.h>

static int copy_file(struct AdfVolume *vol, const char *amiga_name, const char *host_path) {
    FILE *f = fopen(host_path, "rb");
    if (!f) { perror(host_path); return -1; }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    void *buf = malloc((size_t)n);
    if (!buf) { fprintf(stderr, "oom\n"); fclose(f); return -1; }
    if (fread(buf, 1, (size_t)n, f) != (size_t)n) { perror("fread"); fclose(f); free(buf); return -1; }
    fclose(f);

    /* Delete existing entry if any */
    adfRemoveEntry(vol, vol->curDirPtr, (char *)amiga_name);

    struct AdfFile *out = adfFileOpen(vol, (char *)amiga_name, ADF_FILE_MODE_WRITE);
    if (!out) {
        fprintf(stderr, "  [fail] adfFileOpen %s\n", amiga_name);
        free(buf);
        return -1;
    }
    uint32_t written = adfFileWrite(out, (uint32_t)n, buf);
    adfFileClose(out);
    free(buf);
    if (written != (uint32_t)n) {
        fprintf(stderr, "  [fail] write short %s: %u/%ld\n", amiga_name, written, n);
        return -1;
    }
    printf("  [ok]  %s (%ld B)\n", amiga_name, n);
    return 0;
}

static int recurse(struct AdfVolume *vol, const char *host_dir, const char *prefix) {
    DIR *d = opendir(host_dir);
    if (!d) { perror(host_dir); return -1; }
    /* Snapshot parent dir cursor by remembering the dir block to come back to */
    ADF_SECTNUM saved = vol->curDirPtr;

    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (e->d_name[0] == '.') continue;  /* skip ., .., dotfiles */
        char host_path[4096];
        snprintf(host_path, sizeof(host_path), "%s/%s", host_dir, e->d_name);
        struct stat st;
        if (lstat(host_path, &st) != 0) continue;

        if (S_ISDIR(st.st_mode)) {
            /* Create dir, descend */
            adfRemoveEntry(vol, vol->curDirPtr, e->d_name);
            ADF_RETCODE rc = adfCreateDir(vol, vol->curDirPtr, e->d_name);
            if (rc != ADF_RC_OK) {
                fprintf(stderr, "  [fail] mkdir %s/%s\n", prefix, e->d_name);
                continue;
            }
            printf("  [dir] %s%s/\n", prefix, e->d_name);
            if (adfChangeDir(vol, e->d_name) != ADF_RC_OK) {
                fprintf(stderr, "  [fail] chdir %s/%s\n", prefix, e->d_name);
                continue;
            }
            char child_prefix[4096];
            snprintf(child_prefix, sizeof(child_prefix), "%s%s/", prefix, e->d_name);
            recurse(vol, host_path, child_prefix);
            adfParentDir(vol);
        } else if (S_ISREG(st.st_mode)) {
            char prefixed[4096];
            snprintf(prefixed, sizeof(prefixed), "%s%s", prefix, e->d_name);
            (void)prefixed;
            copy_file(vol, e->d_name, host_path);
        }
    }
    closedir(d);
    /* adfParentDir calls should have us back; if not, can't easily recover */
    (void)saved;
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "Usage: %s <disk.adf> <host-dir>\n", argv[0]);
        return 1;
    }
    const char *adf = argv[1];
    const char *hostdir = argv[2];

    if (adfLibInit() != ADF_RC_OK) { fprintf(stderr, "adfLibInit failed\n"); return 2; }
    struct AdfDevice *dev = adfDevOpen(adf, ADF_ACCESS_MODE_READWRITE);
    if (!dev) { fprintf(stderr, "open %s failed\n", adf); return 2; }
    if (adfDevMount(dev) != ADF_RC_OK) { fprintf(stderr, "mount failed\n"); return 2; }
    struct AdfVolume *vol = adfVolMount(dev, 0, ADF_ACCESS_MODE_READWRITE);
    if (!vol) { fprintf(stderr, "vol mount failed\n"); return 2; }

    printf("Copying %s -> %s:\n", hostdir, adf);
    int r = recurse(vol, hostdir, "");

    adfVolUnMount(vol);
    adfDevUnMount(dev);
    adfDevClose(dev);
    adfLibCleanUp();
    return r;
}
