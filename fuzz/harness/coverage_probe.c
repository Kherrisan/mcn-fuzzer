/* Controlled PCGUARD target. The mode file selects a branch.
 *
 * 'a' and 'b' take different paths on the main thread.
 * 'm' keeps the main thread small and the worker tiny.
 * 't' keeps the main thread small and runs a distinct loop on a second thread.
 */
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

/* Each thread writes only its own object. pthread_join happens-before the merge. */
static volatile unsigned main_sink;
static volatile unsigned worker_sink;

static void branch_a(void) {
    for (unsigned i = 0; i < 80; i++) {
        main_sink = main_sink * 3u + i;
    }
}

static void branch_b(void) {
    for (unsigned i = 0; i < 80; i++) {
        main_sink ^= (i * 5u) + 1u;
        if ((i & 7u) == 0u) {
            main_sink += 9u;
        }
    }
}

static void tiny(void) { main_sink += 1u; }

static void *worker(void *arg) {
    int mode = (int)(intptr_t)arg;
    if (mode == 't') {
        for (unsigned i = 0; i < 120; i++) {
            worker_sink += (i ^ 0x5au);
            if ((i & 3u) == 0u) {
                worker_sink ^= 0x11u;
            }
        }
    } else {
        worker_sink += 1u;
    }
    return NULL;
}

int main(int argc, char **argv) {
    int mode = 'a';
    if (argc > 1) {
        FILE *file = fopen(argv[1], "r");
        if (file != NULL) {
            int byte = fgetc(file);
            if (byte == 'a' || byte == 'b' || byte == 'm' || byte == 't') {
                mode = byte;
            }
            fclose(file);
        }
    }
    pthread_t thread;
    if (pthread_create(&thread, NULL, worker, (void *)(intptr_t)mode) != 0) {
        return 2;
    }
    if (mode == 'a') {
        branch_a();
    } else if (mode == 'b') {
        branch_b();
    } else {
        tiny();
    }
    if (pthread_join(thread, NULL) != 0) {
        return 2;
    }
    unsigned merged = main_sink + worker_sink;
    return merged == 0xffffffffu ? 1 : 0;
}
