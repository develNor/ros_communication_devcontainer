std_msgs/Header header
uint64 seq

# The same fixed-rate message is both a fresh probe and the response to the
# newest peer probe. On receipt, t4 is the local receive time.
bool echo_valid
uint64 echo_seq
builtin_interfaces/Time echo_t1
builtin_interfaces/Time echo_t2
builtin_interfaces/Time echo_t3
