#pragma once
/*
 * net - Wi-Fi station + MQTT client for the SAgrIoT node.
 *
 * Settings (SSID, password, broker URI, node id) come from Kconfig, set in
 * firmware/sdkconfig.local. Topics follow docs/message_contract.md:
 * sagriot/<node>/<kind>.
 */
#include <stdbool.h>

/* Starts Wi-Fi; MQTT connects as soon as the node has an IP.
 * online_status is published (retained) on .../status at every connect;
 * a retained "offline" message is left as last will. */
void net_start(const char *online_status);

/* Waits up to timeout_ms for the MQTT connection. */
bool net_wait_ready(int timeout_ms);
bool net_is_ready(void);

/* Publishes json on sagriot/<node>/<kind>.
 * Returns the MQTT message id (>= 0), or -1 if not connected. */
int net_publish(const char *kind, const char *json, int qos, bool retain);

/* Messages from the gateway/cloud. The node subscribes to
 * sagriot/<node>/cloud_decision and sagriot/<node>/config; each message is
 * passed to the handler with its kind (last topic part). data is NOT
 * null-terminated. Runs in the MQTT task: keep it short. */
typedef void (*net_rx_handler_t)(const char *kind, const char *data, int len);
void net_set_rx_handler(net_rx_handler_t handler);

/* True once SNTP has set the clock. */
bool net_time_synced(void);
