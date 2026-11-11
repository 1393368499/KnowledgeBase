#include "ble_relay.h"
#include <algorithm>
#include <cstring>
#include <sys/time.h>
#include "cJSON.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/ble_uuid.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

extern "C" void ble_store_config_init(void);
// UUIDs: b04e0001/2/3/4-7a31-4aa4-9b8f-278ba019f604 (service, RX, TX, info).
static const ble_uuid128_t svc=BLE_UUID128_INIT(0x04,0xf6,0x19,0xa0,0x8b,0x27,0x8f,0x9b,0xa4,0x4a,0x31,0x7a,0x01,0x00,0x4e,0xb0);
static const ble_uuid128_t rx=BLE_UUID128_INIT(0x04,0xf6,0x19,0xa0,0x8b,0x27,0x8f,0x9b,0xa4,0x4a,0x31,0x7a,0x02,0x00,0x4e,0xb0);
static const ble_uuid128_t tx=BLE_UUID128_INIT(0x04,0xf6,0x19,0xa0,0x8b,0x27,0x8f,0x9b,0xa4,0x4a,0x31,0x7a,0x03,0x00,0x4e,0xb0);
static const ble_uuid128_t inf=BLE_UUID128_INIT(0x04,0xf6,0x19,0xa0,0x8b,0x27,0x8f,0x9b,0xa4,0x4a,0x31,0x7a,0x04,0x00,0x4e,0xb0);
static std::atomic<uint16_t> connection{BLE_HS_CONN_HANDLE_NONE};
static std::atomic<bool> ready{false}, authenticated{false}, subscribed{false};
static std::atomic<uint32_t> route_version{0};
static std::atomic<int> task_id{0};
uint32_t ble_relay_route_version(){return route_version;}
int ble_relay_task_id(){return task_id;}
void ble_relay_set_route(uint32_t version,int conversation){if(version>=route_version){task_id=conversation;route_version=version;}}
static uint16_t tx_handle;
static uint8_t address_type;
static std::string information;
static void (*show_pairing)(uint32_t);
static SemaphoreHandle_t send_mutex, state_mutex;
struct Slot {SemaphoreHandle_t done,tx_ack; uint16_t id=0; size_t expected=0,ack_offset=0,tx_size=0;
    uint8_t tx_type=0; bool begun=false,failed=false; Response response;};
static Slot slots[2];
static uint16_t next_id=0;
static uint32_t u32(const uint8_t* p){return uint32_t(p[0])|(uint32_t(p[1])<<8)|(uint32_t(p[2])<<16)|(uint32_t(p[3])<<24);}
static void put32(uint8_t* p,uint32_t n){for(int i=0;i<4;i++)p[i]=uint8_t(n>>(i*8));}
static std::string str(cJSON* j,const char* key){auto* p=cJSON_GetObjectItem(j,key);return cJSON_IsString(p)?p->valuestring:"";}
static int num(cJSON* j,const char* key){auto* p=cJSON_GetObjectItem(j,key);return cJSON_IsNumber(p)?p->valueint:0;}

static int access(uint16_t, uint16_t, ble_gatt_access_ctxt* ctx, void*) {
    if(ctx->op==BLE_GATT_ACCESS_OP_READ_CHR)
        return os_mbuf_append(ctx->om,information.data(),information.size())?BLE_ATT_ERR_INSUFFICIENT_RES:0;
    if(!authenticated)return BLE_ATT_ERR_INSUFFICIENT_AUTHEN;
    uint8_t packet[517];uint16_t length=0;
    if(ble_hs_mbuf_to_flat(ctx->om,packet,sizeof(packet),&length)||length<7)return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    const uint8_t type=packet[0];const uint16_t id=packet[1]|(packet[2]<<8);
    const uint32_t offset=u32(packet+3);const auto* data=packet+7;const size_t n=length-7;
    if(type==11){if(n!=8)return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;ble_relay_set_route(u32(data),u32(data+4));return 0;}
    if(type==8) {
        if(n!=8||!subscribed||ble_att_mtu(connection)<247)return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        uint64_t epoch=uint64_t(u32(data))|(uint64_t(u32(data+4))<<32);
        if(epoch<1700000000ULL||epoch>4102444800ULL)return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        timeval time={static_cast<time_t>(epoch),0};settimeofday(&time,nullptr);ready=true;
        ESP_LOGI("relay","Encrypted phone relay ready (MTU %u)",ble_att_mtu(connection));return 0;
    }
    xSemaphoreTake(state_mutex,portMAX_DELAY);
    for(auto& s:slots)if(s.id&&s.id==id) {
        if(type==12){if(n==1&&data[0]==s.tx_type&&offset<=s.tx_size){s.ack_offset=std::max(s.ack_offset,size_t(offset));xSemaphoreGive(s.tx_ack);}}
        else if(type==13){s.failed=true;s.response.code=0;xSemaphoreGive(s.tx_ack);xSemaphoreGive(s.done);}
        else if(type==5&&!s.begun) {
            std::string json(reinterpret_cast<const char*>(data),n);auto* j=cJSON_Parse(json.c_str());
            const int total=j?num(j,"length"):-1;
            if(j&&total>=0&&total<=120000) {
                s.expected=total;s.response.code=num(j,"code");s.response.checksum=str(j,"sha");
                s.response.page_count=num(j,"pages");s.response.frame_count=num(j,"frames");
                s.response.menu_count=num(j,"menus");s.response.menu_conversation=num(j,"conversation");
                s.response.body.reserve(total);s.begun=true;
            } else {s.response.code=0;xSemaphoreGive(s.done);}
            if(j)cJSON_Delete(j);
        } else if(type==6&&s.begun&&offset==s.response.body.size()&&n<=s.expected-s.response.body.size()) {
            s.response.body.append(reinterpret_cast<const char*>(data),n);
        } else if(type==7&&s.begun&&s.response.body.size()==s.expected) {
            xSemaphoreGive(s.done);
        } else {s.response.code=0;xSemaphoreGive(s.done);}
        break;
    }
    xSemaphoreGive(state_mutex);return 0;
}
static ble_gatt_chr_def characteristics[4];
static ble_gatt_svc_def services[2];
static int gap(ble_gap_event* event,void*);
static void advertise() {
    ble_hs_adv_fields fields={};fields.flags=BLE_HS_ADV_F_DISC_GEN|BLE_HS_ADV_F_BREDR_UNSUP;
    fields.uuids128=const_cast<ble_uuid128_t*>(&svc);fields.num_uuids128=1;fields.uuids128_is_complete=1;
    ble_gap_adv_set_fields(&fields);
    ble_hs_adv_fields response={};const char* name=ble_svc_gap_device_name();
    response.name=reinterpret_cast<const uint8_t*>(name);response.name_len=strlen(name);response.name_is_complete=1;
    ble_gap_adv_rsp_set_fields(&response);
    ble_gap_adv_params params={};params.conn_mode=BLE_GAP_CONN_MODE_UND;params.disc_mode=BLE_GAP_DISC_MODE_GEN;
    params.itvl_min=800;params.itvl_max=1000;
    ble_gap_adv_start(address_type,nullptr,BLE_HS_FOREVER,&params,gap,nullptr);
}
static int gap(ble_gap_event* e,void*) {
    switch(e->type) {
    case BLE_GAP_EVENT_CONNECT:
        if(e->connect.status){advertise();break;}
        connection=e->connect.conn_handle;
        {ble_gap_upd_params p={};p.itvl_min=12;p.itvl_max=24;p.supervision_timeout=600;ble_gap_update_params(connection,&p);}
        ble_gap_security_initiate(connection);break;
    case BLE_GAP_EVENT_DISCONNECT:
        ready=false;authenticated=false;subscribed=false;connection=BLE_HS_CONN_HANDLE_NONE;
        xSemaphoreTake(state_mutex,portMAX_DELAY);
        for(auto& s:slots)if(s.id){s.failed=true;s.response.code=0;xSemaphoreGive(s.tx_ack);xSemaphoreGive(s.done);}
        xSemaphoreGive(state_mutex);advertise();ESP_LOGI("relay","Phone disconnected");break;
    case BLE_GAP_EVENT_ENC_CHANGE:
        {ble_gap_conn_desc d={};if(!ble_gap_conn_find(e->enc_change.conn_handle,&d))
            authenticated=e->enc_change.status==0&&d.sec_state.encrypted&&d.sec_state.authenticated;}
        break;
    case BLE_GAP_EVENT_SUBSCRIBE:
        if(e->subscribe.attr_handle==tx_handle)subscribed=e->subscribe.cur_notify;
        if(!subscribed)ready=false;
        break;
    case BLE_GAP_EVENT_PASSKEY_ACTION:
        if(e->passkey.params.action==BLE_SM_IOACT_DISP) {
            ble_sm_io io={};io.action=BLE_SM_IOACT_DISP;io.passkey=esp_random()%1000000;
            if(show_pairing)show_pairing(io.passkey);
            ble_sm_inject_io(e->passkey.conn_handle,&io);
        }break;
    case BLE_GAP_EVENT_REPEAT_PAIRING:
        // Never silently replace a stored bond from a remote request.
        return BLE_GAP_REPEAT_PAIRING_IGNORE;
    default:break;
    }return 0;
}
static void on_sync(){if(!ble_hs_id_infer_auto(0,&address_type))advertise();}
static void host(void*){nimble_port_run();nimble_port_freertos_deinit();}
bool ble_relay_start(const std::string& info,void (*pairing)(uint32_t)) {
    information=info;show_pairing=pairing;send_mutex=xSemaphoreCreateMutex();state_mutex=xSemaphoreCreateMutex();
    for(auto& s:slots){s.done=xSemaphoreCreateBinary();s.tx_ack=xSemaphoreCreateBinary();}
    if(nimble_port_init()!=ESP_OK)return false;
    ble_hs_cfg.sync_cb=on_sync;ble_hs_cfg.store_status_cb=ble_store_util_status_rr;
    ble_hs_cfg.sm_io_cap=BLE_HS_IO_DISPLAY_ONLY;ble_hs_cfg.sm_bonding=1;ble_hs_cfg.sm_mitm=1;ble_hs_cfg.sm_sc=1;
    ble_hs_cfg.sm_our_key_dist=BLE_SM_PAIR_KEY_DIST_ENC|BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist=BLE_SM_PAIR_KEY_DIST_ENC|BLE_SM_PAIR_KEY_DIST_ID;
    ble_svc_gap_init();ble_svc_gatt_init();ble_svc_gap_device_name_set("KB Note4");ble_att_set_preferred_mtu(517);
    characteristics[0].uuid=&rx.u;characteristics[0].access_cb=access;
    characteristics[0].flags=BLE_GATT_CHR_F_WRITE|BLE_GATT_CHR_F_WRITE_ENC|BLE_GATT_CHR_F_WRITE_AUTHEN;
    characteristics[1].uuid=&tx.u;characteristics[1].access_cb=access;characteristics[1].val_handle=&tx_handle;
    characteristics[1].flags=BLE_GATT_CHR_F_NOTIFY;
    characteristics[2].uuid=&inf.u;characteristics[2].access_cb=access;
    characteristics[2].flags=BLE_GATT_CHR_F_READ|BLE_GATT_CHR_F_READ_ENC|BLE_GATT_CHR_F_READ_AUTHEN;
    services[0].type=BLE_GATT_SVC_TYPE_PRIMARY;services[0].uuid=&svc.u;services[0].characteristics=characteristics;
    if(ble_gatts_count_cfg(services)||ble_gatts_add_svcs(services))return false;
    ble_store_config_init();nimble_port_freertos_init(host);return true;
}
bool ble_relay_ready(){return ready&&authenticated&&subscribed;}
static bool send(uint8_t type,uint16_t id,uint32_t offset,const uint8_t* data,size_t n) {
    uint8_t packet[512];if(n>sizeof(packet)-7)return false;
    packet[0]=type;packet[1]=id;packet[2]=id>>8;put32(packet+3,offset);if(n)memcpy(packet+7,data,n);
    xSemaphoreTake(send_mutex,portMAX_DELAY);int result=-1;const int64_t deadline=esp_timer_get_time()+5000000;
    while(ble_relay_ready()&&esp_timer_get_time()<deadline) {
        auto* mb=ble_hs_mbuf_from_flat(packet,n+7);if(!mb)break;
        result=ble_gatts_notify_custom(connection,tx_handle,mb);
        if(!result)break;
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    xSemaphoreGive(send_mutex);return result==0;
}
static bool stream(Slot& slot,uint8_t type,uint16_t id,const uint8_t* data,size_t n,const std::atomic<bool>* cancel) {
    xSemaphoreTake(state_mutex,portMAX_DELAY);slot.tx_type=type;slot.tx_size=n;slot.ack_offset=0;xSemaphoreTake(slot.tx_ack,0);xSemaphoreGive(state_mutex);
    unsigned window=0;
    size_t offset=0;while(offset<n) {
        if(cancel&&*cancel)return false;
        const size_t size=std::min(n-offset,std::min(size_t(505),size_t(ble_att_mtu(connection)-10)));
        if(!send(type,id,offset,data+offset,size))return false;
        offset+=size;vTaskDelay(std::max(TickType_t(1),pdMS_TO_TICKS(2)));
        if(++window==8||offset==n){
            window=0;const int64_t deadline=esp_timer_get_time()+8000000;
            while(true){
                xSemaphoreTake(state_mutex,portMAX_DELAY);
                const bool failed=slot.failed,acknowledged=slot.ack_offset>=offset;
                xSemaphoreGive(state_mutex);
                if(failed||!ble_relay_ready()||(cancel&&*cancel))return false;
                if(acknowledged)break;
                if(esp_timer_get_time()>=deadline){ESP_LOGW("relay","Phone receipt timed out (type %u, offset %u)",type,unsigned(offset));return false;}
                xSemaphoreTake(slot.tx_ack,pdMS_TO_TICKS(100));
            }
        }
    }return true;
}
Response ble_relay_request(const std::string& meta,const uint8_t* body,size_t length,bool control,const std::atomic<bool>* cancel,uint32_t timeout_ms) {
    auto& slot=slots[control?1:0];Response response;if(!ble_relay_ready())return response;
    xSemaphoreTake(state_mutex,portMAX_DELAY);
    const uint16_t id=++next_id?next_id:++next_id;
    slot.id=id;slot.begun=false;slot.failed=false;slot.expected=0;slot.response=Response{};
    xSemaphoreTake(slot.done,0);xSemaphoreGive(state_mutex);
    bool ok=send(1,id,meta.size(),nullptr,0)&&stream(slot,2,id,reinterpret_cast<const uint8_t*>(meta.data()),meta.size(),nullptr)
        &&(!length||stream(slot,3,id,body,length,cancel))&&send(4,id,0,nullptr,0);
    if(!ok&&ble_relay_ready())send(10,id,0,nullptr,0);
    if(ok)ok=xSemaphoreTake(slot.done,pdMS_TO_TICKS(timeout_ms))==pdTRUE;
    xSemaphoreTake(state_mutex,portMAX_DELAY);
    if(ok)response=std::move(slot.response);
    slot.response=Response{};slot.id=0;
    xSemaphoreGive(state_mutex);return response;
}
