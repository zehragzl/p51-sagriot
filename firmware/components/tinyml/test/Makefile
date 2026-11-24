CC      = gcc
CFLAGS  = -std=c11 -O2 -Wall -Wextra -I../include -I.. -I../../baseline/include
LDLIBS  = -lm
SRCS    = test_tinyml.c ../tinyml.c ../tinyml_weights.c ../../baseline/baseline.c

test_tinyml: $(SRCS) ../include/tinyml.h ../tinyml_weights.h
	$(CC) $(CFLAGS) -o $@ $(SRCS) $(LDLIBS)

clean:
	rm -f test_tinyml
