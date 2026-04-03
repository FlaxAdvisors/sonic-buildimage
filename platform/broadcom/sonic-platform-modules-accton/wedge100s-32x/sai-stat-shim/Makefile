# Makefile for libsai-stat-shim.so
CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -g -fPIC
LDFLAGS = -lpthread

OBJS = shim.o stat_map.o compat.o

libsai-stat-shim.so: $(OBJS)
	$(CC) -shared -o $@ $(OBJS) -ldl $(LDFLAGS)

%.o: %.c shim.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o *.so
