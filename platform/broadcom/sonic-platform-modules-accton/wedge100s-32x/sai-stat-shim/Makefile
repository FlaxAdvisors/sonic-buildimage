# Makefile for libsai-stat-shim.so and test_parser
CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -g -fPIC
LDFLAGS = -lpthread

OBJS = shim.o bcmcmd_client.o stat_map.o compat.o

libsai-stat-shim.so: $(OBJS)
	$(CC) -shared -o $@ $(OBJS) -ldl $(LDFLAGS)

# test_parser includes bcmcmd_client.c directly, so links only stat_map.o
test_parser: test_parser.o stat_map.o
	$(CC) -o $@ $^ $(LDFLAGS)

%.o: %.c shim.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o *.so test_parser
