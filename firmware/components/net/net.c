/*
 * net.c - Wi-Fi station + MQTT (esp-mqtt) + SNTP.
 *
 * Flow: net_start() -> Wi-Fi connects -> got IP -> MQTT client starts ->
 * connected -> "online" status published. If Wi-Fi or the broker drops,
 * both reconnect by themselves; net_publish() returns -1 meanwhile, so the
 * caller can count the lost cycles (they show up as seq gaps at the gateway).
 */
#include "net.h"

#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "esp_eap_client.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "mqtt_client.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

static const char *TAG = "net";

#define MQTT_UP BIT0

/* QoS 1 messages sent but not yet acknowledged by the broker (PUBACK).
 * net_flush() waits for this to reach 0 before the node sleeps. */
static atomic_int s_unacked;

static EventGroupHandle_t s_events;
static esp_mqtt_client_handle_t s_mqtt;
static bool s_mqtt_started;
static char s_status_topic[64];
static char s_online[160];
static char s_offline[96];
static net_rx_handler_t s_rx_handler;

static void subscribe(const char *kind)
{
    char topic[64];
    snprintf(topic, sizeof topic, "sagriot/%s/%s", CONFIG_SAGRIOT_NODE_ID, kind);
    esp_mqtt_client_subscribe(s_mqtt, topic, 1);
}

/* last part of "sagriot/<node>/<kind>" */
static void topic_kind(const char *topic, int len, char *kind, size_t n)
{
    int start = len;
    while (start > 0 && topic[start - 1] != '/') {
        start--;
    }
    int k = len - start;
    if (k > (int)n - 1) {
        k = (int)n - 1;
    }
    memcpy(kind, topic + start, k);
    kind[k] = '\0';
}

static void mqtt_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT connected to %s", CONFIG_SAGRIOT_MQTT_URI);
        atomic_fetch_add(&s_unacked, 1);   /* its PUBACK is counted below too */
        if (esp_mqtt_client_publish(s_mqtt, s_status_topic, s_online, 0, 1, 1) < 0) {
            atomic_fetch_sub(&s_unacked, 1);
        }
        subscribe("cloud_decision");
        subscribe("config");
        xEventGroupSetBits(s_events, MQTT_UP);
        break;
    case MQTT_EVENT_PUBLISHED:   /* PUBACK for a QoS 1 message */
        if (atomic_load(&s_unacked) > 0) {
            atomic_fetch_sub(&s_unacked, 1);
        }
        break;
    case MQTT_EVENT_DATA: {
        esp_mqtt_event_handle_t e = (esp_mqtt_event_handle_t)data;
        if (s_rx_handler) {
            char kind[24];
            topic_kind(e->topic, e->topic_len, kind, sizeof kind);
            s_rx_handler(kind, e->data, e->data_len);
        }
        break;
    }
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT disconnected, will retry");
        xEventGroupClearBits(s_events, MQTT_UP);
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGW(TAG, "MQTT error (broker unreachable? check the URI and the Mac firewall)");
        break;
    default:
        break;
    }
}

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "Wi-Fi disconnected, retrying");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "Wi-Fi up, IP " IPSTR, IP2STR(&e->ip_info.ip));
        if (!s_mqtt_started) {
            esp_mqtt_client_start(s_mqtt);   /* later reconnects are automatic */
            s_mqtt_started = true;
        }
    }
}

void net_start(const char *online_status)
{
    s_events = xEventGroupCreate();

    /* Wi-Fi keeps its calibration data in NVS */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    /* MQTT client: created now, started once the node has an IP */
    snprintf(s_status_topic, sizeof s_status_topic, "sagriot/%s/status", CONFIG_SAGRIOT_NODE_ID);
    snprintf(s_offline, sizeof s_offline,
             "{\"v\":1,\"node\":\"%s\",\"state\":\"offline\"}", CONFIG_SAGRIOT_NODE_ID);
    strlcpy(s_online, online_status, sizeof s_online);

    esp_mqtt_client_config_t mc = {
        .broker.address.uri = CONFIG_SAGRIOT_MQTT_URI,
        .credentials.client_id = CONFIG_SAGRIOT_NODE_ID,
        .session.last_will = {
            .topic = s_status_topic,
            .msg = s_offline,
            .qos = 1,
            .retain = 1,
        },
    };
    s_mqtt = esp_mqtt_client_init(&mc);
    esp_mqtt_client_register_event(s_mqtt, ESP_EVENT_ANY_ID, mqtt_event, NULL);

    /* clock for the "ts" field */
    esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    esp_netif_sntp_init(&sntp);

    /* Wi-Fi station */
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));

    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, CONFIG_SAGRIOT_WIFI_SSID, sizeof wc.sta.ssid);
    strlcpy((char *)wc.sta.password, CONFIG_SAGRIOT_WIFI_PASSWORD, sizeof wc.sta.password);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));

    /* WPA2-Enterprise (e.g. a campus network): identity set -> PEAP/TTLS
     * with that identity and the password above; the server certificate
     * is not checked. Empty identity: normal WPA2-Personal. */
    const char *eap_id = CONFIG_SAGRIOT_WIFI_EAP_IDENTITY;
    if (eap_id[0] != '\0') {
        const char *pw = CONFIG_SAGRIOT_WIFI_PASSWORD;
        ESP_ERROR_CHECK(esp_eap_client_set_identity((const unsigned char *)eap_id, strlen(eap_id)));
        ESP_ERROR_CHECK(esp_eap_client_set_username((const unsigned char *)eap_id, strlen(eap_id)));
        ESP_ERROR_CHECK(esp_eap_client_set_password((const unsigned char *)pw, strlen(pw)));
        ESP_ERROR_CHECK(esp_wifi_sta_enterprise_enable());
        ESP_LOGI(TAG, "WPA2-Enterprise as \"%s\"", eap_id);
    }

    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "connecting to Wi-Fi \"%s\"", CONFIG_SAGRIOT_WIFI_SSID);
}

bool net_is_ready(void)
{
    return (xEventGroupGetBits(s_events) & MQTT_UP) != 0;
}

bool net_wait_ready(int timeout_ms)
{
    EventBits_t bits = xEventGroupWaitBits(s_events, MQTT_UP, pdFALSE, pdTRUE,
                                           pdMS_TO_TICKS(timeout_ms));
    return (bits & MQTT_UP) != 0;
}

int net_publish(const char *kind, const char *json, int qos, bool retain)
{
    if (!net_is_ready()) {
        return -1;
    }
    char topic[64];
    snprintf(topic, sizeof topic, "sagriot/%s/%s", CONFIG_SAGRIOT_NODE_ID, kind);
    if (qos > 0) {
        atomic_fetch_add(&s_unacked, 1);   /* before publishing: the PUBACK can arrive first */
    }
    int id = esp_mqtt_client_publish(s_mqtt, topic, json, 0, qos, retain ? 1 : 0);
    if (id < 0 && qos > 0) {
        atomic_fetch_sub(&s_unacked, 1);
    }
    return id;
}

bool net_flush(int timeout_ms)
{
    int64_t end = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (atomic_load(&s_unacked) > 0 && esp_timer_get_time() < end) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return atomic_load(&s_unacked) <= 0;
}

void net_stop(void)
{
    if (s_mqtt_started) {
        /* clean DISCONNECT: the broker does not send the "offline" will,
         * so a sleeping node is not reported as dead every cycle */
        esp_mqtt_client_disconnect(s_mqtt);
        esp_mqtt_client_stop(s_mqtt);
    }
    esp_wifi_disconnect();
    esp_wifi_stop();
}

void net_set_rx_handler(net_rx_handler_t handler)
{
    s_rx_handler = handler;
}

bool net_wait_time_synced(int timeout_ms)
{
    if (net_time_synced()) {
        return true;
    }
    return esp_netif_sntp_sync_wait(pdMS_TO_TICKS(timeout_ms)) == ESP_OK;
}

bool net_time_synced(void)
{
    time_t now = time(NULL);
    struct tm t;
    gmtime_r(&now, &t);
    return t.tm_year + 1900 >= 2024;
}
