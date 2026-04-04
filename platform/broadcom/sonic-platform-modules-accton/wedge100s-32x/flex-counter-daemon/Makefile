# Makefile for wedge100s-flex-counter-daemon
CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -g
LDFLAGS = -Wl,-Bstatic -lhiredis -Wl,-Bdynamic -lpthread

OBJS = daemon.o bcmcmd_client.o stat_map.o compat.o

wedge100s-flex-counter-daemon: $(OBJS)
	$(CC) -o $@ $(OBJS) $(LDFLAGS)

daemon.o: daemon.c bcmcmd_client.h stat_map.h
	$(CC) $(CFLAGS) -c -o $@ $<

bcmcmd_client.o: bcmcmd_client.c bcmcmd_client.h stat_map.h
	$(CC) $(CFLAGS) -c -o $@ $<

stat_map.o: stat_map.c stat_map.h
	$(CC) $(CFLAGS) -c -o $@ $<

compat.o: compat.c
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o wedge100s-flex-counter-daemon
