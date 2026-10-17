CC      = gcc
CFLAGS  = -std=c11 -O2 -Wall -Wextra -I../include
LDLIBS  = -lm

test_baseline: test_baseline.c ../baseline.c ../include/baseline.h
	$(CC) $(CFLAGS) -o $@ test_baseline.c ../baseline.c $(LDLIBS)

clean:
	rm -f test_baseline
