# Makefile for libsai-stat-shim.so (fault masker only)
CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -g -fPIC

OBJS = shim.o compat.o

libsai-stat-shim.so: $(OBJS)
	$(CC) -shared -o $@ $(OBJS) -ldl

%.o: %.c shim.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o *.so
