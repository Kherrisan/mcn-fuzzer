/*
 * Minimal one-line TCP server used to verify the mcn-fuzzer ASAN path.
 *
 * The ordinary build does nothing special for the payload "CRASH": it reads
 * one byte past an eight-byte heap allocation, which is likely benign without a
 * sanitizer. The ASAN build reports that read and, with abort_on_error=1,
 * terminates with SIGABRT so the fuzzer can classify it as a crash.
 *
 * The server intentionally handles one connection per forked case. It binds
 * 127.0.0.1 on the port supplied by the campaign's @PORT@ placeholder.
 */

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static void serve_one(int listener) {
    int client = accept(listener, NULL, NULL);
    if (client < 0) {
        return;
    }

    char buffer[256];
    ssize_t count = recv(client, buffer, sizeof(buffer) - 1, 0);
    if (count > 0) {
        buffer[count] = '\0';
        if (strstr(buffer, "CRASH") != NULL) {
            char *heap = malloc(8);
            if (heap != NULL) {
                /* One-byte out-of-bounds read: ASAN reports it, plain builds usually do not crash. */
                volatile char value = heap[8];
                (void)value;
                free(heap);
            }
            send(client, "ASAN\n", 5, 0);
        } else {
            send(client, "OK\n", 3, 0);
        }
    }
    close(client);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s PORT\n", argv[0]);
        return 2;
    }

    int port = atoi(argv[1]);
    if (port <= 0 || port > 65535) {
        fprintf(stderr, "invalid port: %s\n", argv[1]);
        return 2;
    }

    int listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) {
        perror("socket");
        return 2;
    }
    int reuse = 1;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    struct sockaddr_in address;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons((unsigned short)port);
    if (bind(listener, (struct sockaddr *)&address, sizeof(address)) < 0) {
        perror("bind");
        close(listener);
        return 2;
    }
    if (listen(listener, 8) < 0) {
        perror("listen");
        close(listener);
        return 2;
    }

    serve_one(listener);
    close(listener);
    return 0;
}
