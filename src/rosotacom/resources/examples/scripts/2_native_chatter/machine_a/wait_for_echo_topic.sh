#!/bin/bash

# Wait until the topic is available and has a known type
until topic_info="$(ros2 topic info /chatter 2>/dev/null)" && grep -q "Type:" <<<"$topic_info"; do
    echo "[INFO] Waiting for /chatter to become available..."
    sleep 1
done

# Now start echoing the topic
exec ros2 topic echo /chatter
