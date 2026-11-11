#include <algorithm>
#include <array>
#include <atomic>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>
#include "audio_codec.h"
#include "button_clicks.h"
#include "ble_relay.h"
#include "cJSON.h"
#include "driver/gpio.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_sntp.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "psa/crypto.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "zectrix_board.h"
#include "zectrix_epd.h"

#define ASSET(n) extern const uint8_t asset_##n[] asm("_binary_" #n "_bin_start")
ASSET(ready); ASSET(recording); ASSET(uploading); ASSET(transcribing);
ASSET(thinking); ASSET(offline); ASSET(error); ASSET(setup); ASSET(canceled);
ASSET(hotspot); ASSET(clock); ASSET(server); ASSET(canceling); ASSET(cancel_late);
ASSET(pairing); ASSET(standby);
ASSET(submitted); ASSET(task_full);
ASSET(task_selected);
ASSET(menu_wait); ASSET(menu_offline); ASSET(menu_error);
ASSET(upload_error); ASSET(upload_rejected);
ASSET(restarting); ASSET(restart_busy);
static ZectrixBoard board;
static AudioCodec* codec;
static std::atomic<bool> connected{false}, busy{false}, capturing{false};
static QueueHandle_t display_queue;
static TaskHandle_t network_task, cancel_task;
enum SendPhase {Idle,Waiting,Sending,LocalCanceled};
static std::atomic<int> send_phase{Idle};
static std::atomic<bool> cancel_requested{false},cancel_confirmed{false},gesture_pending{false};
static std::atomic<int64_t> send_after{0};
static std::string previous_turn;
static int previous_pages=0,previous_transcript_pages=0,previous_page=0;
static bool previous_answer=true;
static uint8_t* audio;
static std::atomic<size_t> audio_bytes{0};
static std::string server, token, current_turn, latest_answer;
static SemaphoreHandle_t context_mutex;
static std::atomic<bool> answer_view{true};
static std::atomic<int> capture_version{0};
static std::atomic<uint32_t> capture_route{0};
static std::atomic<int64_t> capture_time{0};
static std::atomic<int> page{0}, pages{0}, transcript_pages{0}, config_version{0};
static std::atomic<bool> page_request{false};
static bool configured=false;
static constexpr size_t MAX_AUDIO=16000*2*60;
static constexpr gpio_num_t OK=GPIO_NUM_0, UP=GPIO_NUM_39, DOWN=GPIO_NUM_18;
static std::array<uint8_t,15000> last_frame;
static std::atomic<const uint8_t*> last_status{nullptr};
static std::atomic<const uint8_t*> foreground_status{asset_ready};
static SemaphoreHandle_t cache_mutex;
struct CachedPage {std::string turn; bool answer; int index; uint8_t* data; uint32_t used;};
static std::vector<CachedPage> page_cache;
static uint32_t cache_clock=0;
static constexpr size_t CACHE_PAGES=64, FRAME_BYTES=15000;
static bool wifi_fast=false; // Network task owns this setting.
static bool bluetooth_mode=true;
static std::atomic<bool> standby_visible{false};
static std::atomic<int64_t> last_activity{0}, pairing_until{0};
static std::array<uint8_t,15000> standby_frame;
static bool standby_cached=false;
static std::atomic<int> power_percent{-1},power_mode{0}; // unknown / battery / charging / full / no battery / fault
static std::atomic<bool> standby_dirty{true};
static std::atomic<bool> parallel_mode{false}, latest_task_active{false};
static std::vector<std::string> watched_turns;
static std::string latest_submitted, pending_result;
static std::atomic<bool> menu_visible{false},menu_dirty{false},menu_confirm{false};
static std::atomic<bool> menu_selecting{false};
static std::atomic<int64_t> menu_notice_until{0};
static std::atomic<bool> menu_refresh{true};
static std::atomic<int> menu_index{0},menu_count{1},menu_rendered{-1},menu_conversation{0};
struct CachedMenu {int index,conversation,count;uint8_t* data;};
static std::vector<CachedMenu> menu_cache;
static std::atomic<int64_t> menu_cached_at{0};
static std::array<uint8_t,15000> task_home;
static std::atomic<bool> home_cached{false};
static void watch_turn(const std::string& id) {
    if(!id.empty()&&watched_turns.size()<16&&std::find(watched_turns.begin(),watched_turns.end(),id)==watched_turns.end())watched_turns.push_back(id);
}
static void activity(){last_activity=esp_timer_get_time();standby_visible=false;}
static void wifi_transfer_mode(bool fast) {
    if(bluetooth_mode)return;
    if(wifi_fast!=fast){esp_wifi_set_ps(fast?WIFI_PS_NONE:WIFI_PS_MIN_MODEM);wifi_fast=fast;}
}

static std::string turn_id() {xSemaphoreTake(context_mutex,portMAX_DELAY);auto out=current_turn;xSemaphoreGive(context_mutex);return out;}
static void set_turn(const std::string& id) {xSemaphoreTake(context_mutex,portMAX_DELAY);current_turn=id;xSemaphoreGive(context_mutex);}


static void show(const uint8_t* data) {
    last_status=nullptr;
    if (display_queue) xQueueOverwrite(display_queue, data);
}
static void status(const uint8_t* data) {
    foreground_status=data;
    if(standby_visible||menu_visible||esp_timer_get_time()<pairing_until)return;
    if(last_status.exchange(data)!=data && display_queue) xQueueOverwrite(display_queue,data);
}
// Local battery rendering works without a phone or server. Keep the date/time
// and cached page bitmaps intact; only the reserved gap in the home header is used.
static void show_standby_locked() {
    static std::array<uint8_t,15000> image;
    memcpy(image.data(),standby_cached?standby_frame.data():asset_standby,FRAME_BYTES);
    auto pixel=[&](int x,int y,bool black){int bit=y*400+x;auto mask=uint8_t(0x80>>(bit%8));if(black)image[bit/8]&=~mask;else image[bit/8]|=mask;};
    auto rect=[&](int x,int y,int w,int h,bool black){for(int j=y;j<y+h;j++)for(int i=x;i<x+w;i++)pixel(i,j,black);};
    rect(215,0,82,36,false);
    static const char* glyphs[]={"111101101101111","010110010010111","111001111100111","111001111001111","101101111001001","111100111001111","111100111101111","111001001001001","111101111101111","111101111001111"};
    const int pct=power_percent.load(),mode=power_mode.load();
    char label[12];if(pct>=0&&mode!=4)snprintf(label,sizeof(label),"%d",pct);else strcpy(label,"--");
    int x=216;for(char c:std::string(label)){if(c=='-')rect(x,15,6,2,true);else for(int y=0;y<5;y++)for(int col=0;col<3;col++)if(glyphs[c-'0'][y*3+col]=='1')rect(x+col*2,11+y*2,2,2,true);x+=8;}
    // Percent sign and a 5-level battery outline; + marks actual charging.
    rect(x,11,2,2,true);rect(x+5,19,2,2,true);for(int i=0;i<5;i++)rect(x+5-i,13+i,1,1,true);
    rect(258,10,28,13,true);rect(260,12,24,9,false);rect(286,14,3,5,true);
    if(pct>=0&&mode!=4)rect(261,13,22*pct/100,7,true);
    if(mode==2){rect(291,12,2,8,true);rect(288,15,8,2,true);}
    if(mode==4||mode==5){for(int i=0;i<7;i++){pixel(268+i,13+i,true);pixel(274-i,13+i,true);}}
    show(image.data());
}
static bool cached_menu(int index,bool render=false) {
    bool found=false;xSemaphoreTake(cache_mutex,portMAX_DELAY);
    for(const auto& entry:menu_cache)if(entry.index==index) {
        found=true;
        if(render){menu_count=entry.count;menu_conversation=entry.conversation;menu_rendered=index;show(entry.data);}
        break;
    }
    xSemaphoreGive(cache_mutex);
    if(found&&render)ESP_LOGI("voice","Local task selector %d queued",index);
    return found;
}
static bool selected_menu_home(int index,int conversation) {
    bool found=false;xSemaphoreTake(cache_mutex,portMAX_DELAY);
    for(const auto& entry:menu_cache)if(entry.index==index&&entry.conversation==conversation) {
        memcpy(task_home.data(),asset_task_selected,FRAME_BYTES);
        const int start=std::max(0,std::min(index-2,entry.count-6));
        const int source_y=43+(index-start)*34;
        // Reuse the already-verified CJK row bitmap. Invert its focused row to
        // normal black-on-white text; no network or font rasterizer is needed.
        for(int y=0;y<31;y++)for(int x=0;x<374;x++) {
            const int from=(source_y+y)*400+13+x,to=(80+y)*400+13+x;
            if(entry.data[from/8]&(0x80>>(from%8)))task_home[to/8]&=~(0x80>>(to%8));
            else task_home[to/8]|=0x80>>(to%8);
        }
        home_cached=true;found=true;break;
    }
    xSemaphoreGive(cache_mutex);return found;
}
static void invalidate_menus() {
    xSemaphoreTake(cache_mutex,portMAX_DELAY);for(auto& entry:menu_cache)heap_caps_free(entry.data);menu_cache.clear();xSemaphoreGive(cache_mutex);
    menu_cached_at=0;
}
static bool cached_page(const std::string& id,bool answer,int index,bool render=false) {
    bool found=false;
    xSemaphoreTake(cache_mutex,portMAX_DELAY);
    for(auto& c:page_cache)if(c.turn==id&&c.answer==answer&&c.index==index) {
        c.used=++cache_clock;found=true;if(render)show(c.data);break;
    }
    xSemaphoreGive(cache_mutex);
    if(found&&render)ESP_LOGI("voice","Local %s page %d queued",answer?"answer":"transcript",index);
    return found;
}
static bool cache_page(const std::string& id,bool answer,int index,const uint8_t* data) {
    auto* copy=static_cast<uint8_t*>(heap_caps_malloc(FRAME_BYTES,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if(!copy)return false;
    memcpy(copy,data,FRAME_BYTES);
    xSemaphoreTake(cache_mutex,portMAX_DELAY);
    auto victim=page_cache.end();
    for(auto i=page_cache.begin();i!=page_cache.end();++i)if(i->turn==id&&i->answer==answer&&i->index==index){victim=i;break;}
    if(victim==page_cache.end()&&page_cache.size()>=CACHE_PAGES)
        victim=std::min_element(page_cache.begin(),page_cache.end(),[](const auto& a,const auto& b){return a.used<b.used;});
    if(victim!=page_cache.end()){heap_caps_free(victim->data);*victim={id,answer,index,copy,++cache_clock};}
    else page_cache.push_back({id,answer,index,copy,++cache_clock});
    xSemaphoreGive(cache_mutex);return true;
}
static void invalidate_answer(const std::string& id) {
    xSemaphoreTake(cache_mutex,portMAX_DELAY);
    for(auto i=page_cache.begin();i!=page_cache.end();){
        if(i->turn==id&&i->answer){heap_caps_free(i->data);i=page_cache.erase(i);}
        else ++i;
    }
    xSemaphoreGive(cache_mutex);
}
static bool show_current_cached() {
    return !capturing&&cached_page(turn_id(),answer_view.load(),page.load(),true);
}
static void display(void*) {
    zectrix_epd_config_t cfg; zectrix_epd_get_default_config(&cfg);
    zectrix_epd_handle_t epd=nullptr;
    if (zectrix_epd_new(&cfg, &epd)!=ESP_OK || zectrix_epd_power_on(epd)!=ESP_OK) { vTaskDelete(nullptr); return; }
    bool baseline=false;unsigned partials=0;
    static std::array<uint8_t,15000> previous;
    while (true) {
        if (xQueueReceive(display_queue,last_frame.data(),portMAX_DELAY)==pdTRUE) {
            if(baseline&&previous==last_frame)continue;
            const int64_t started=esp_timer_get_time();
            // Use the driver's supported monochrome transition refresh, with
            // periodic full refreshes to clear accumulated ghosting.
            bool partial=baseline&&partials<7;
            zectrix_epd_rect_t rect={0,0,400,300};
            esp_err_t err=partial?zectrix_epd_refresh_partial_1bpp(epd,&rect,last_frame.data(),last_frame.size()):
                zectrix_epd_refresh_full_1bpp(epd,last_frame.data(),last_frame.size());
            if(partial&&err!=ESP_OK){partial=false;err=zectrix_epd_refresh_full_1bpp(epd,last_frame.data(),last_frame.size());}
            if(err==ESP_OK){previous=last_frame;baseline=true;partials=partial?partials+1:0;}else baseline=false;
            if (err!=ESP_OK) ESP_LOGW("voice", "Display refresh failed: %s",esp_err_to_name(err));
            else ESP_LOGI("voice","Display refresh completed (%s, %lld ms)",partial?"partial":"full",(esp_timer_get_time()-started)/1000);
        }
    }
}
static std::string nvs_string(const char* key) {
    nvs_handle_t handle; size_t size=0;
    if(nvs_open("kbvoice",NVS_READONLY,&handle)!=ESP_OK) return "";
    nvs_get_str(handle,key,nullptr,&size);
    std::string out(size,'\0');
    if(size) {nvs_get_str(handle,key,out.data(),&size); out.resize(size-1);}
    nvs_close(handle); return out;
}
static void save_nvs(const char* key,const std::string& id) {
    nvs_handle_t h;
    if(nvs_open("kbvoice",NVS_READWRITE,&h)==ESP_OK) {nvs_set_str(h,key,id.c_str());nvs_commit(h);nvs_close(h);}
}
static void save_pending(const std::string& id){save_nvs("pending",id);}
static void save_cancel(const std::string& id){save_nvs("cancel",id);}
static std::string uuid() {
    uint8_t b[16]; esp_fill_random(b,16); b[6]=(b[6]&15)|64; b[8]=(b[8]&63)|128;
    char out[37]; snprintf(out,sizeof(out),"%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",b[0],b[1],b[2],b[3],b[4],b[5],b[6],b[7],b[8],b[9],b[10],b[11],b[12],b[13],b[14],b[15]); return out;
}
static std::string sha(const uint8_t* data,size_t length) {
    uint8_t digest[32]; size_t output=0;
    if(psa_hash_compute(PSA_ALG_SHA_256,data,length,digest,sizeof(digest),&output)!=PSA_SUCCESS || output!=32)return "";
    char out[65]; for(int i=0;i<32;i++) snprintf(out+i*2,3,"%02x",digest[i]); return out;
}
static void wifi_event(void*,esp_event_base_t base,int32_t id,void* data) {
    if(base==IP_EVENT && id==IP_EVENT_STA_GOT_IP) {connected=true;status(asset_clock);ESP_LOGI("voice","WiFi connected; synchronizing clock");}
    if(base==WIFI_EVENT && id==WIFI_EVENT_STA_DISCONNECTED) {
        connected=false;
        ESP_LOGW("voice","WiFi disconnected, reason=%u",static_cast<wifi_event_sta_disconnected_t*>(data)->reason);
        if(static_cast<wifi_event_sta_disconnected_t*>(data)->reason==201)status(asset_hotspot);
        esp_wifi_connect();
    }
    if(base==WIFI_EVENT && id==WIFI_EVENT_STA_START) esp_wifi_connect();
}
static esp_http_client_handle_t http_client=nullptr, cancel_http_client=nullptr; // Owned only by network().
static esp_err_t http_event(esp_http_client_event_t* e) {
    auto* r=static_cast<Response*>(e->user_data);
    if(!r)return ESP_OK;
    if(e->event_id==HTTP_EVENT_ON_HEADER && e->header_key && strcasecmp(e->header_key,"X-Content-SHA256")==0) r->checksum=e->header_value;
    if(e->event_id==HTTP_EVENT_ON_HEADER && e->header_key && strcasecmp(e->header_key,"X-Page-Count")==0) r->page_count=atoi(e->header_value);
    if(e->event_id==HTTP_EVENT_ON_HEADER && e->header_key && strcasecmp(e->header_key,"X-Frame-Count")==0) r->frame_count=atoi(e->header_value);
    return ESP_OK;
}
static bool write_all(esp_http_client_handle_t h,const uint8_t* p,size_t n) {
    while(n) {if(cancel_requested)return false;int k=esp_http_client_write(h,reinterpret_cast<const char*>(p),std::min(n,size_t(8192))); if(k<=0)return false; p+=k;n-=k;}
    return true;
}
static Response request(const std::string& path,const uint8_t* pcm=nullptr,size_t count=0,
                        bool post=false,esp_http_client_handle_t* control=nullptr,int selected_conversation=-1) {
    Response r;
    if(!connected) return r;
    if(bluetooth_mode) {
        const int64_t started=esp_timer_get_time();
        auto* meta=cJSON_CreateObject();
        cJSON_AddStringToObject(meta,"server",server.c_str());cJSON_AddStringToObject(meta,"token",token.c_str());
        cJSON_AddStringToObject(meta,"path",path.c_str());cJSON_AddStringToObject(meta,"method",(pcm||post)?"POST":"GET");
        cJSON_AddNumberToObject(meta,"length",count);
        if(selected_conversation>=0){if(selected_conversation)cJSON_AddNumberToObject(meta,"conversation_id",selected_conversation);else cJSON_AddNullToObject(meta,"conversation_id");}
        if(pcm){cJSON_AddStringToObject(meta,"turn",turn_id().c_str());cJSON_AddNumberToObject(meta,"version",capture_version.load());cJSON_AddNumberToObject(meta,"route_version",capture_route.load());cJSON_AddStringToObject(meta,"sha",sha(pcm,count).c_str());}
        char* json=cJSON_PrintUnformatted(meta);
        const uint32_t timeout=pcm?180000:path=="task-selection/"?12000:path.find("/batch/")!=std::string::npos?90000:20000;
        r=ble_relay_request(json,pcm,count,control!=nullptr,pcm?&cancel_requested:nullptr,timeout);
        cJSON_free(json);cJSON_Delete(meta);
        if(r.code&&sha(reinterpret_cast<const uint8_t*>(r.body.data()),r.body.size())!=r.checksum)r.code=0;
        ESP_LOGI("voice","BLE relay %s: %d, %u bytes, %lld ms",pcm?"upload":path.c_str(),r.code,unsigned(r.body.size()),(esp_timer_get_time()-started)/1000);
        return r;
    }
    if(control)esp_wifi_set_ps(WIFI_PS_NONE);else wifi_transfer_mode(true);
    auto& channel=control?*control:http_client;
    const int64_t started=esp_timer_get_time();
    std::string url=server+"/api/v1/voice/device/"+path;
    if(!channel) {
        esp_http_client_config_t cfg={}; cfg.url=url.c_str(); cfg.timeout_ms=12000;
        cfg.crt_bundle_attach=esp_crt_bundle_attach; cfg.event_handler=http_event; cfg.disable_auto_redirect=true;
        cfg.keep_alive_enable=true;cfg.buffer_size=4096;
        channel=esp_http_client_init(&cfg);
    }
    auto h=channel;if(!h)return r;
    esp_http_client_set_user_data(h,&r);esp_http_client_set_url(h,url.c_str());
    esp_http_client_set_method(h,(pcm||post)?HTTP_METHOD_POST:HTTP_METHOD_GET);
    std::string auth="Bearer "+token; esp_http_client_set_header(h,"Authorization",auth.c_str());
    bool ok=true;
    if(pcm) {
        std::string head="--kbvoice-boundary\r\nContent-Disposition: form-data; name=\"turn_id\"\r\n\r\n"+turn_id()+
            "\r\n--kbvoice-boundary\r\nContent-Disposition: form-data; name=\"config_version\"\r\n\r\n"+std::to_string(capture_version.load())+
            "\r\n--kbvoice-boundary\r\nContent-Disposition: form-data; name=\"audio_sha256\"\r\n\r\n"+sha(pcm,count)+
            "\r\n--kbvoice-boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"recording.wav\"\r\nContent-Type: audio/wav\r\n\r\n";
        std::string tail="\r\n--kbvoice-boundary--\r\n";
        esp_http_client_set_method(h,HTTP_METHOD_POST);
        esp_http_client_set_header(h,"Content-Type","multipart/form-data; boundary=kbvoice-boundary");
        ok=esp_http_client_open(h,head.size()+count+tail.size())==ESP_OK;
        if(ok)ok=write_all(h,reinterpret_cast<const uint8_t*>(head.data()),head.size())&&write_all(h,pcm,count)&&write_all(h,reinterpret_cast<const uint8_t*>(tail.data()),tail.size());
    } else ok=esp_http_client_open(h,0)==ESP_OK;
    if(ok) {
        if(esp_http_client_fetch_headers(h)>=0) {
            r.code=esp_http_client_get_status_code(h);
            auto length=esp_http_client_get_content_length(h);
            if(length>0&&length<=120000)r.body.reserve(length);
            char buf[2048]; int got;
            while((got=esp_http_client_read(h,buf,sizeof(buf)))>0) {
                if(r.body.size()+got>120000) {r.code=0;break;}
                r.body.append(buf,got);
            }
            if(got<0||!esp_http_client_is_complete_data_received(h))r.code=0;
        }
    }
    if(!r.code||r.code==400){esp_http_client_cleanup(h);channel=nullptr;}
    else esp_http_client_set_user_data(h,nullptr);
    ESP_LOGI("voice","HTTP %s %d, %u bytes, %lld ms",control?"cancel":pcm?"upload":path.find("pages/")!=std::string::npos?"pages":path=="config/"?"config":"state",r.code,unsigned(r.body.size()),(esp_timer_get_time()-started)/1000);
    if(!control&&!capturing&&!busy)wifi_transfer_mode(false);
    return r;
}
static std::string field(cJSON* root,const char* key) {
    auto* p=cJSON_GetObjectItem(root,key); return cJSON_IsString(p)?p->valuestring:"";
}
static int number(cJSON* root,const char* key) {auto* p=cJSON_GetObjectItem(root,key);return cJSON_IsNumber(p)?p->valueint:0;}
static void refresh_standby() {
    static int64_t last=0;
    if(!connected||capturing||(!standby_dirty&&esp_timer_get_time()-last<30000000))return;
    standby_dirty=false;
    last=esp_timer_get_time();auto r=request("standby/");
    if(r.code!=200||r.body.size()!=FRAME_BYTES||sha(reinterpret_cast<const uint8_t*>(r.body.data()),r.body.size())!=r.checksum)return;
    xSemaphoreTake(cache_mutex,portMAX_DELAY);
    memcpy(standby_frame.data(),r.body.data(),FRAME_BYTES);standby_cached=true;
    if(standby_visible)show_standby_locked();
    xSemaphoreGive(cache_mutex);
}
static bool refresh_config() {
    static std::string previous_latest,previous_revision;
    auto r=request("config/"); if(r.code!=200)return false;
    auto* json=cJSON_Parse(r.body.c_str());if(!json)return false;
    config_version=number(json,"config_version");latest_answer=field(json,"latest_turn_id");
    const auto revision=field(json,"latest_answer_revision");
    if(!previous_revision.empty()&&!revision.empty()&&(previous_latest!=latest_answer||previous_revision!=revision)&&
       number(json,"latest_conversation_id")==number(json,"active_conversation_id")&&number(json,"active_conversation_id")>0){
        invalidate_answer(latest_answer);pending_result=latest_answer;
        ESP_LOGI("voice","Updated answer content invalidated old page cache");
    }
    previous_latest=latest_answer;previous_revision=revision;
    parallel_mode=field(json,"conversation_mode")=="per_turn"||field(json,"conversation_mode")=="tasks";
    ble_relay_set_route(number(json,"route_version"),number(json,"active_conversation_id"));
    if(parallel_mode){auto* ids=cJSON_GetObjectItem(json,"active_turn_ids");cJSON* item=nullptr;cJSON_ArrayForEach(item,ids)if(cJSON_IsString(item))watch_turn(item->valuestring);}
    cJSON_Delete(json);return config_version>0;
}
static bool download_pages(const std::string& id,bool answer,int index) {
    auto r=request("turns/"+id+"/pages/"+(answer?"answer":"transcript")+"/batch/"+std::to_string(index)+"/");
    if(r.code==200&&r.frame_count>0&&r.frame_count<=8&&r.page_count>=index+r.frame_count&&
       r.body.size()==size_t(r.frame_count)*FRAME_BYTES&&!r.checksum.empty()&&
       sha(reinterpret_cast<const uint8_t*>(r.body.data()),r.body.size())==r.checksum) {
        for(int i=0;i<r.frame_count;i++)if(!cache_page(id,answer,index+i,reinterpret_cast<const uint8_t*>(r.body.data())+i*FRAME_BYTES))return false;
        if(id==turn_id()){if(answer)pages=r.page_count;else transcript_pages=r.page_count;}
        ESP_LOGI("voice","Cached %d verified %s pages from %d",r.frame_count,answer?"answer":"transcript",index);
        return true;
    }
    ESP_LOGW("voice","Page download not verified (HTTP %d, %u bytes)",r.code,unsigned(r.body.size()));
    return false;
}
static bool download_menu(int index,bool render) {
    const int64_t started=esp_timer_get_time();auto r=request("task-menu/"+std::to_string(index)+"/");
    if(r.code==404&&menu_visible){menu_index=0;menu_dirty=true;return false;}
    if(r.code!=200||r.body.size()!=FRAME_BYTES||r.menu_count<1||r.menu_count>21||sha(reinterpret_cast<const uint8_t*>(r.body.data()),r.body.size())!=r.checksum)return false;
    auto* data=static_cast<uint8_t*>(heap_caps_malloc(FRAME_BYTES,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));if(!data)return false;
    memcpy(data,r.body.data(),FRAME_BYTES);
    xSemaphoreTake(cache_mutex,portMAX_DELAY);
    auto entry=std::find_if(menu_cache.begin(),menu_cache.end(),[index](const auto& item){return item.index==index;});
    if(entry==menu_cache.end())menu_cache.push_back({index,r.menu_conversation,r.menu_count,data});
    else {heap_caps_free(entry->data);*entry={index,r.menu_conversation,r.menu_count,data};}
    menu_count=r.menu_count;menu_cached_at=esp_timer_get_time();
    xSemaphoreGive(cache_mutex);
    if(render&&menu_visible&&menu_index==index)cached_menu(index,true);
    ESP_LOGI("voice","Task selector fetch %d completed in %lld ms",index,(esp_timer_get_time()-started)/1000);
    return true;
}
static void warm_menu() {
    if(!parallel_mode||!connected||busy||capturing||gesture_pending||menu_visible)return;
    if(menu_refresh.exchange(false))invalidate_menus();
    // At most one bounded frame per idle pass. The first eight recent choices
    // cover the new-task entry and the four concurrently active tasks.
    for(int index=0;index<std::min(menu_count.load(),8);index++)if(!cached_menu(index)){download_menu(index,false);return;}
}
static bool fetch_page() {
    const auto id=turn_id();if(id.empty())return false;
    const bool answer=answer_view.load();const int index=page.load();
    if(!cached_page(id,answer,index)&&!download_pages(id,answer,index))return false;
    // A button may have changed the desired page while the network was busy.
    if(id==turn_id()&&answer==answer_view.load()&&index==page.load()&&!capturing&&!menu_visible)
        return cached_page(id,answer,index,true);
    return false;
}
static bool prefetch_pages() {
    const auto id=turn_id();if(id.empty()||capturing||busy||menu_visible)return false;
    const bool answer=answer_view.load();const int index=page.load();
    const int limit=answer?pages.load():transcript_pages.load();
    // Prefetch one nearby batch at a time; cap RAM and never sweep a long reply
    // indefinitely, evicting pages that the user is currently reading.
    for(int p=std::max(0,index-1);p<std::min(limit,index+16);p++)
        if(!cached_page(id,answer,p))return download_pages(id,answer,p);
    const bool other=!answer;
    if((other?pages.load():transcript_pages.load())>0&&!cached_page(id,other,0))return download_pages(id,other,0);
    return false;
}
static void background_tasks() {
    static int64_t last_poll=0;static size_t cursor=0;
    if(!parallel_mode||busy||capturing||!connected)return;
    if(!watched_turns.empty()&&esp_timer_get_time()-last_poll>1000000) {
        last_poll=esp_timer_get_time();cursor%=watched_turns.size();const auto id=watched_turns[cursor];
        auto r=request("turns/"+id+"/");auto* json=cJSON_Parse(r.body.c_str());
        if(r.code==200&&json) {
            auto state=field(json,"state");
            if(state=="answer_ready"||state=="failed"||state=="uncertain"||state=="canceled") {
                if(state!="canceled"&&number(json,"conversation_id")==ble_relay_task_id()&&ble_relay_task_id()!=0){invalidate_answer(id);pending_result=id;}
                xSemaphoreTake(context_mutex,portMAX_DELAY);if(id==latest_submitted)latest_task_active=false;xSemaphoreGive(context_mutex);
                watched_turns.erase(watched_turns.begin()+cursor);standby_dirty=true;
                menu_refresh=true;
            } else cursor++;
        } else if(r.code==404) {
            watched_turns.erase(watched_turns.begin()+cursor);
            if(id==turn_id()){set_turn("");pages=0;transcript_pages=0;}
        }
        if(json)cJSON_Delete(json);
    }
    if(!pending_result.empty()&&!busy&&!capturing&&!gesture_pending&&!menu_visible) {
        set_turn(pending_result);answer_view=true;page=0;
        if(fetch_page()){activity();pending_result.clear();}
    }
}
static void task_controls() {
    static uint32_t shown_route=0;static int shown_task=-1;
    if(!parallel_mode||busy||capturing)return;
    if(!connected){
        if(menu_confirm.exchange(false)){
            menu_selecting=false;show(asset_menu_offline);menu_notice_until=esp_timer_get_time()+2500000;
        }
        return;
    }
    bool selection_done=false,local_home=false;uint32_t prepared_route=0;int prepared_task=0;
    if(menu_visible&&menu_confirm.exchange(false)) {
        const int chosen_index=menu_index.load(),chosen_conversation=menu_conversation.load();
        auto r=request("task-selection/",nullptr,0,true,nullptr,chosen_conversation);
        auto* j=cJSON_Parse(r.body.c_str());
        if(r.code==200&&j){prepared_route=number(j,"route_version");prepared_task=number(j,"active_conversation_id");ble_relay_set_route(prepared_route,prepared_task);local_home=selected_menu_home(chosen_index,chosen_conversation);selection_done=true;menu_visible=false;activity();}
        else {menu_dirty=true;show(r.code==0&&!ble_relay_ready()&&bluetooth_mode?asset_menu_offline:asset_menu_error);menu_notice_until=esp_timer_get_time()+2500000;}
        menu_selecting=false;
        if(j)cJSON_Delete(j);
    }
    if(menu_visible&&esp_timer_get_time()>=menu_notice_until&&menu_dirty.exchange(false))download_menu(menu_index.load(),true);
    if(!menu_visible&&(selection_done||shown_route!=ble_relay_route_version()||shown_task!=ble_relay_task_id())) {
        const uint32_t version=ble_relay_route_version();const int selected=ble_relay_task_id();
        const bool changed_task=selected!=shown_task;
        const bool assigned_after_upload=version==shown_route&&shown_task==0&&selected>0;
        const bool display_home=selection_done||(changed_task&&!assigned_after_upload);
        Response r;
        if(local_home&&prepared_route==version&&prepared_task==selected){r.code=200;r.body.assign(reinterpret_cast<const char*>(task_home.data()),FRAME_BYTES);r.checksum=sha(task_home.data(),FRAME_BYTES);}
        else r=request("task-home/");
        if(r.code==200&&r.body.size()==FRAME_BYTES&&sha(reinterpret_cast<const uint8_t*>(r.body.data()),r.body.size())==r.checksum) {
            if(changed_task){xSemaphoreTake(cache_mutex,portMAX_DELAY);for(auto& cached:page_cache)heap_caps_free(cached.data);page_cache.clear();xSemaphoreGive(cache_mutex);set_turn("");pages=0;transcript_pages=0;pending_result.clear();}
            menu_refresh=true;
            xSemaphoreTake(cache_mutex,portMAX_DELAY);memcpy(task_home.data(),r.body.data(),FRAME_BYTES);home_cached=true;xSemaphoreGive(cache_mutex);shown_route=version;shown_task=selected;standby_dirty=true;
            if(display_home&&!busy&&!capturing&&!gesture_pending){activity();show(task_home.data());}
            if(display_home&&selected>0) {
                auto detail=request("tasks/"+std::to_string(selected)+"/");auto* j=cJSON_Parse(detail.body.c_str());
                if(detail.code==200&&j&&shown_route==ble_relay_route_version()&&shown_task==ble_relay_task_id()&&!busy&&!capturing&&!gesture_pending) {
                    auto* turns=cJSON_GetObjectItem(j,"turns");cJSON* item=nullptr;
                    cJSON_ArrayForEach(item,turns)if(number(item,"answer_page_count")>0){set_turn(field(item,"turn_id"));pages=number(item,"answer_page_count");transcript_pages=number(item,"transcript_page_count");page=0;answer_view=true;}
                    if(!turn_id().empty())page_request=true;
                }
                if(j)cJSON_Delete(j);
            }
        }
    }
}
static void ask_cancel() {
    if(!busy&&parallel_mode&&latest_task_active) {
        previous_turn=turn_id();previous_pages=pages.load();previous_transcript_pages=transcript_pages.load();previous_page=page.load();previous_answer=answer_view.load();
        xSemaphoreTake(context_mutex,portMAX_DELAY);current_turn=latest_submitted;xSemaphoreGive(context_mutex);
        save_pending(turn_id());send_phase=Sending;busy=true;
    }
    if(!busy){ESP_LOGI("voice","Double click: no pending send");return;}
    int expected=Waiting;
    if(send_phase.compare_exchange_strong(expected,LocalCanceled)) {
        // Winning this CAS means the upload task cannot start a request.
        save_pending("");status(asset_canceled);
        ESP_LOGI("voice","Double click: canceled before upload");
        return;
    }
    if(expected!=Sending||cancel_requested.exchange(true))return;
    save_cancel(turn_id());status(asset_canceling);
    ESP_LOGI("voice","Double click: requesting server cancellation");
    xTaskNotifyGive(cancel_task);
}
static void cancellation(void*) {
    while(true) {
        if(bluetooth_mode)connected=ble_relay_ready();
        ulTaskNotifyTake(pdTRUE,portMAX_DELAY);
        const auto id=turn_id();
        while(busy&&id==turn_id()&&cancel_requested&&!cancel_confirmed) {
            if(!connected){vTaskDelay(pdMS_TO_TICKS(1000));continue;}
            auto r=request("turns/"+id+"/cancel/",nullptr,0,true,&cancel_http_client);
            if(!busy||id!=turn_id()||!cancel_requested)break;
            auto* json=cJSON_Parse(r.body.c_str());
            bool confirmed=r.code==200&&json&&field(json,"state")=="canceled";
            if(json)cJSON_Delete(json);
            if(confirmed){cancel_confirmed=true;status(asset_canceled);ESP_LOGI("voice","Server confirmed cancellation");break;}
            if(r.code==409){save_cancel("");cancel_requested=false;status(asset_cancel_late);ESP_LOGW("voice","Cancellation too late: AI processing already started");break;}
            status(asset_canceling);vTaskDelay(pdMS_TO_TICKS(1500));
        }
    }
}
static void finish_canceled() {
    if(parallel_mode){xSemaphoreTake(context_mutex,portMAX_DELAY);if(current_turn==latest_submitted)latest_task_active=false;xSemaphoreGive(context_mutex);}
    save_pending("");save_cancel("");audio_bytes=0;
    set_turn(previous_turn);pages=previous_pages;transcript_pages=previous_transcript_pages;
    page=previous_page;answer_view=previous_answer;page_request=false;
    cancel_requested=false;cancel_confirmed=false;send_phase=Idle;
    status(asset_canceled);busy=false;
    standby_dirty=true;
    ESP_LOGI("voice","Canceled send finished; ready for a new recording");
}
static void network(void*) {
    bool initialized=false;
    std::string pending=nvs_string("pending");
    const auto pending_cancel=nvs_string("cancel");
    if(pending.empty()&&!pending_cancel.empty())pending=pending_cancel;
    int64_t last_heartbeat=0,last_prefetch=0;
    while(true) {
        if(bluetooth_mode)connected=ble_relay_ready();
        if(connected&&!initialized) {
            time_t now; time(&now);
            if(now<1700000000) {vTaskDelay(pdMS_TO_TICKS(500));continue;}
            status(asset_server);initialized=refresh_config();
            if(initialized){
                pairing_until=0;activity();refresh_standby();
                ESP_LOGI("voice","Device authenticated and ready");status(asset_ready);
                if(!pending.empty()){
                    set_turn(pending);previous_turn=latest_answer;send_phase=Sending;busy=true;
                    if(pending_cancel==pending){cancel_requested=true;xTaskNotifyGive(cancel_task);}
                    pending.clear();
                } else if(!latest_answer.empty()){
                    set_turn(latest_answer);
                    if(parallel_mode){answer_view=true;page=0;page_request=true;}
                    else {send_phase=Sending;busy=true;}
                }
            }
        }
        if(busy&&(send_phase==LocalCanceled||cancel_confirmed)){finish_canceled();continue;}
        if(initialized&&!busy&&!capturing&&menu_visible){task_controls();vTaskDelay(pdMS_TO_TICKS(50));continue;}
        if(busy&&cancel_requested){status(asset_canceling);vTaskDelay(pdMS_TO_TICKS(50));continue;}
        if(busy&&send_phase==Waiting) {
            if(esp_timer_get_time()<send_after||gesture_pending){vTaskDelay(pdMS_TO_TICKS(20));continue;}
            int expected=Waiting;
            if(!send_phase.compare_exchange_strong(expected,Sending))continue;
        }
        if(busy&&initialized) {
            status(asset_uploading);
            const int64_t deadline=esp_timer_get_time()+120000000;
            const auto id=turn_id();
            // The UUID upload is idempotent. Submit immediately rather than
            // spending a round trip proving that a brand-new UUID is absent.
            auto r=audio_bytes?request("turns/",audio,audio_bytes+44):request("turns/"+id+"/");
            while(!cancel_requested&&(r.code==404||r.code==0||r.code>=500)&&audio_bytes&&esp_timer_get_time()<capture_time.load()+120000000) {
                if(!connected){status(asset_offline);vTaskDelay(pdMS_TO_TICKS(1500));continue;}
                if(r.code==404){r=request("turns/",audio,audio_bytes+44);ESP_LOGI("voice","Audio upload HTTP %d",r.code);}
                else {vTaskDelay(pdMS_TO_TICKS(1500));r=request("turns/"+id+"/");}
            }
            if(parallel_mode&&!cancel_requested) {
                if(r.code==429){status(asset_task_full);vTaskDelay(pdMS_TO_TICKS(1500));continue;}
                auto* accepted=cJSON_Parse(r.body.c_str());
                const bool known=(r.code==200||r.code==202)&&accepted&&field(accepted,"turn_id")==id;
                const bool canceled=known&&field(accepted,"state")=="canceled";
                const int accepted_conversation=accepted?number(accepted,"conversation_id"):0;
                if(accepted)cJSON_Delete(accepted);
                if(canceled){cancel_confirmed=true;continue;}
                if(known) {
                    if(capture_route==ble_relay_route_version())ble_relay_set_route(capture_route,accepted_conversation);
                    watch_turn(id);xSemaphoreTake(context_mutex,portMAX_DELAY);latest_submitted=id;latest_task_active=true;xSemaphoreGive(context_mutex);
                    save_pending("");audio_bytes=0;send_phase=Idle;busy=false;standby_dirty=true;activity();status(asset_submitted);
                    menu_refresh=true;
                    ESP_LOGI("voice","Task accepted; microphone is available for the next task");continue;
                }
            }
            bool final=false;
            std::string last_state;
            while(!cancel_requested&&!final&&esp_timer_get_time()<deadline) {
                if(r.code==200||r.code==202) {
                    auto* json=cJSON_Parse(r.body.c_str());
                    if(json) {
                        std::string state=field(json,"state");
                        if(state!=last_state)ESP_LOGI("voice","Turn state: %s",state.c_str());
                        if(state=="answer_ready") {
                            activity();
                            pages=number(json,"answer_page_count");transcript_pages=number(json,"transcript_page_count");
                            answer_view=true;page=0;final=fetch_page();
                        } else if(state=="canceled") {cancel_confirmed=true;final=true;}
                        else if(state=="failed"||state=="uncertain") {
                            activity();pages=number(json,"answer_page_count");answer_view=true;page=0;
                            if(pages<=0||!fetch_page())status(asset_error);
                            final=true;
                        }
                        else if(state!=last_state) {
                            if(state=="answering"||state=="rendering"||state=="transcribed") {
                                if(!field(json,"transcript").empty()){answer_view=false;page=0;fetch_page();}
                                else status(asset_thinking);
                            } else status(asset_transcribing);
                            last_state=state;
                            standby_dirty=true;
                        }
                        cJSON_Delete(json);
                    }
                } else if(r.code>=400&&r.code<500) {status(audio_bytes?(r.code==404?asset_upload_error:asset_upload_rejected):asset_error);final=true;}
                if(!final){if(standby_visible)refresh_standby();vTaskDelay(pdMS_TO_TICKS(500));r=request("turns/"+id+"/");}
            }
            if(cancel_requested||cancel_confirmed)continue;
            if(final){save_pending("");save_cancel("");audio_bytes=0;send_phase=Idle;busy=false;standby_dirty=true;}
            else {
                // Keep durable identity and resume polling; never generate a new
                // request after an accepted upload or uncertain network result.
                status(asset_offline); vTaskDelay(pdMS_TO_TICKS(3000));
            }
        } else if(page_request.exchange(false))fetch_page();
        else if(initialized&&!capturing&&esp_timer_get_time()-last_heartbeat>30000000) {
            if(!refresh_config())status(asset_offline);
            last_heartbeat=esp_timer_get_time();
        } else if(initialized&&!capturing&&esp_timer_get_time()-last_prefetch>1500000) {
            if(!prefetch_pages())warm_menu();
            last_prefetch=esp_timer_get_time();
        }
        if(initialized&&!capturing){task_controls();background_tasks();}
        if(initialized&&!capturing&&standby_visible)refresh_standby();
        if(!capturing&&!busy)wifi_transfer_mode(false);
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
static void wav_header(size_t length) {
    uint8_t header[44]={ 'R','I','F','F',0,0,0,0,'W','A','V','E','f','m','t',' ',16,0,0,0,1,0,1,0,0x80,0x3e,0,0,0,0x7d,0,0,2,0,16,0,'d','a','t','a',0,0,0,0};
    uint32_t riff=length+36, pcm=length; memcpy(header+4,&riff,4);memcpy(header+40,&pcm,4);memcpy(audio,header,44);
}
static void pairing_code(uint32_t code) {
    static std::array<uint8_t,15000> image;
    static const char* glyphs[]={"111101101101111","010110010010111","111001111100111","111001111001111","101101111001001","111100111001111","111100111101111","111001001001001","111101111101111","111101111001111"};
    memcpy(image.data(),asset_pairing,FRAME_BYTES);char digits[7];snprintf(digits,sizeof(digits),"%06lu",static_cast<unsigned long>(code));
    for(int i=0;i<6;i++)for(int y=0;y<5;y++)for(int x=0;x<3;x++)if(glyphs[digits[i]-'0'][y*3+x]=='1')
        for(int dy=0;dy<8;dy++)for(int dx=0;dx<8;dx++){
            int bit=(212+y*8+dy)*400+44+i*52+x*8+dx;image[bit/8]&=~(0x80>>(bit%8));
        }
    activity();pairing_until=esp_timer_get_time()+90000000;show(image.data());
}
extern "C" void app_main() {
    psa_crypto_init();
    esp_log_level_set("wifi",ESP_LOG_WARN);
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(board.Init());
    context_mutex=xSemaphoreCreateMutex();cache_mutex=xSemaphoreCreateMutex();
    page_cache.reserve(CACHE_PAGES);
    display_queue=xQueueCreate(1,15000);
    xTaskCreate(display,"display",6144,nullptr,3,nullptr);
    auto ssid=nvs_string("ssid"), password=nvs_string("password");
    server=nvs_string("server");token=nvs_string("token");
    while(!server.empty()&&server.back()=='/')server.pop_back();
    bluetooth_mode=nvs_string("transport")!="wifi";
    configured=(bluetooth_mode||!ssid.empty())&&server.rfind("https://",0)==0&&!token.empty();
    status(configured?asset_offline:asset_setup);
    if(!configured)return;
    audio=static_cast<uint8_t*>(heap_caps_malloc(MAX_AUDIO+44,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if(!audio){status(asset_error);return;}
    codec=board.PrepareAudio();
    if(!codec||!codec->valid()){status(asset_error);return;}
    codec->EnableOutput(false);codec->SetInputGain(24);
    if(bluetooth_mode) {
        std::string info="{\"model\":\"ZECTRIX Note 4\",\"firmware\":\"2.5-power-restart\",\"screen\":\"400 x 300\",\"audio\":\"16 kHz PCM16\",\"protocol\":1,\"upload_ack\":true}";
        if(!ble_relay_start(info,pairing_code)){status(asset_error);return;}
        ESP_LOGI("voice","Bluetooth relay mode; Wi-Fi is not started");
    } else {
    ESP_ERROR_CHECK(esp_netif_init());ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init=WIFI_INIT_CONFIG_DEFAULT();ESP_ERROR_CHECK(esp_wifi_init(&init));
    esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,wifi_event,nullptr);
    esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,wifi_event,nullptr);
    wifi_config_t wifi={};
    if(ssid.size()>sizeof(wifi.sta.ssid)||password.size()>=sizeof(wifi.sta.password)){status(asset_setup);return;}
    memcpy(wifi.sta.ssid,ssid.data(),ssid.size());memcpy(wifi.sta.password,password.data(),password.size());
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    // This deployment is in China. Include channels 12/13 when looking for
    // phone hotspots instead of relying on the SDK's default world channel set.
    ESP_ERROR_CHECK(esp_wifi_set_country_code("CN",true));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA,&wifi));ESP_ERROR_CHECK(esp_wifi_start());
    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);esp_sntp_setservername(0,"ntp.aliyun.com");esp_sntp_setservername(1,"pool.ntp.org");esp_sntp_init();
    }
    if(xTaskCreate(cancellation,"cancel",12288,nullptr,4,&cancel_task)!=pdPASS){status(asset_error);return;}
    xTaskCreate(network,"network",12288,nullptr,4,&network_task);
    bool recording=false, canceled=false, last_up=true, last_down=true;
    int64_t last_side_press=0,down_pressed=0,up_pressed=0,power_read_at=0;bool down_long=false,down_woke=false,up_long=false;
    activity();ButtonClicks clicks;bool click_started_busy=false,woke_front=false;
    auto on_click=[&](ClickAction action){
        if(action==ClickAction::Cancel)ask_cancel();
        else if(action==ClickAction::Single&&!woke_front&&!click_started_busy&&!busy&&!capturing){
            if(!turn_id().empty()){answer_view=!answer_view.load();page=0;show_current_cached();page_request=true;}
            else status(asset_ready);
        }
    };
    int stable=1,last=1;int64_t changed=0,pressed=0;
    std::vector<int16_t> chunk(320);
    while(true) {
        if(bluetooth_mode)connected=ble_relay_ready();
        int down=gpio_get_level(OK);int64_t now=esp_timer_get_time();
        if(!recording&&now-power_read_at>=5000000){
            power_read_at=now;const auto power=board.ReadPowerSnapshot();
            const int pct=power.battery_valid?power.battery_percent:-1;
            const int mode=power.charge.fault?5:power.charge.no_battery?4:power.charge.full?3:power.charge.charging?2:1;
            const int old_pct=power_percent.exchange(pct),old_mode=power_mode.exchange(mode);
            ESP_LOGI("power","Battery %d%%, %u mV, mode %d",pct,power.battery_mv,mode);
            if(standby_visible&&(old_pct!=pct||old_mode!=mode)){xSemaphoreTake(cache_mutex,portMAX_DELAY);show_standby_locked();xSemaphoreGive(cache_mutex);}
        }
        if(menu_visible&&!menu_selecting&&menu_notice_until>0&&now>=menu_notice_until){
            menu_notice_until=0;menu_dirty=!cached_menu(menu_index,true);
        }
        if(!capturing&&!menu_selecting&&!standby_visible&&now-last_activity>=10000000&&now>=pairing_until) {
            menu_visible=false;standby_visible=true;xSemaphoreTake(cache_mutex,portMAX_DELAY);
            show_standby_locked();xSemaphoreGive(cache_mutex);
        }
        if(down!=last){last=down;changed=now;}
        if(down!=stable&&now-changed>=30000) {
            stable=down;
            if(!stable) {
                board.SetPowerLed(true);
                woke_front=standby_visible.load();activity();if(woke_front&&!show_current_cached())status(foreground_status.load());
                pressed=now;
                if(!busy&&!menu_visible&&config_version>0) {
                    recording=true;canceled=false;audio_bytes=0;
                    capture_version=config_version.load();capture_route=ble_relay_route_version();capturing=true;
                    board.SetPowerLed(true);ESP_LOGI("voice","Recording started");
                }
            } else {
                board.SetPowerLed(false);
                activity();
                const bool short_press=now-pressed<200000;
                if(menu_visible&&!recording){
                    clicks.release(now,false);
                    if(!menu_selecting&&menu_rendered==menu_index){
                        if(!connected){show(asset_menu_offline);menu_notice_until=now+2500000;}
                        else {menu_notice_until=0;menu_selecting=true;show(asset_menu_wait);menu_confirm=true;}
                    }
                    continue;
                }
                if(recording) {
                    recording=false;capturing=false;board.SetPowerLed(false);
                    if(canceled){audio_bytes=0;clicks.release(now,false);status(asset_canceled);}
                    else if(short_press||audio_bytes<6400) {
                        audio_bytes=0;
                        if(!clicks.pending())click_started_busy=false;
                        if(short_press&&!busy&&!latest_task_active&&!turn_id().empty()){clicks.release(now,false);on_click(ClickAction::Single);}
                        else on_click(clicks.release(now,short_press));
                    } else {
                        clicks.release(now,false);
                        previous_turn=turn_id();previous_pages=pages.load();previous_transcript_pages=transcript_pages.load();
                        previous_page=page.load();previous_answer=answer_view.load();
                        cancel_requested=false;cancel_confirmed=false;
                        ESP_LOGI("voice","Recording captured: %u PCM bytes",unsigned(audio_bytes.load()));
                        wav_header(audio_bytes);const auto id=uuid();set_turn(id);save_cancel("");save_pending(id);
                        if(parallel_mode){pages=0;transcript_pages=0;page=0;answer_view=true;}
                        capture_time=now;send_after=now+800000;send_phase=Waiting;busy=true;
                        standby_dirty=true;
                    }
                } else {
                    if(!clicks.pending())click_started_busy=busy.load();
                    on_click(clicks.release(now,short_press));
                }
            }
        }
        on_click(clicks.poll(now,stable==0));
        gesture_pending=down==0||clicks.pending();
        if(recording&&!canceled) {
            if(now-pressed>=200000)status(asset_recording);
            if(gpio_get_level(UP)==0){canceled=true;capturing=false;status(asset_canceled);board.SetPowerLed(false);}
            else if(audio_bytes<MAX_AUDIO&&codec->InputData(chunk)) {
                size_t n=std::min(chunk.size()*sizeof(int16_t),MAX_AUDIO-audio_bytes);
                memcpy(audio+44+audio_bytes,chunk.data(),n);audio_bytes+=n;
            } else vTaskDelay(pdMS_TO_TICKS(10));
        } else {
            bool up=gpio_get_level(UP),dn=gpio_get_level(DOWN);
            bool side_edge=(!up&&last_up)||(!dn&&last_down),woke_side=false;
            if(up!=last_up||dn!=last_down)board.SetPowerLed(!up||!dn);
            if(side_edge){woke_side=standby_visible.load();activity();if(woke_side&&!show_current_cached())status(foreground_status.load());}
            if(!up&&last_up){up_pressed=now;up_long=false;}
            if(!up&&!up_long&&up_pressed&&now-up_pressed>=5000000){
                up_long=true;activity();
                if(busy||capturing||audio_bytes>0||menu_selecting){show(asset_restart_busy);ESP_LOGI("power","Restart blocked: local recording or selection pending");}
                else {menu_visible=false;show(asset_restarting);board.SetPowerLed(true);ESP_LOGI("power","User requested restart");vTaskDelay(pdMS_TO_TICKS(1500));esp_restart();}
            }
            if(!dn&&last_down){down_pressed=now;down_long=false;down_woke=woke_side;}
            if(!dn&&!down_long&&!busy&&!menu_selecting&&parallel_mode&&down_pressed&&now-down_pressed>=800000) {
                down_long=true;activity();menu_visible=!menu_visible.load();menu_index=0;menu_rendered=-1;
                if(menu_visible){const bool local=cached_menu(0,true);menu_dirty=!local||menu_refresh||esp_timer_get_time()-menu_cached_at>30000000;}
                ESP_LOGI("voice","Task selector %s",menu_visible?"opened":"closed");
                if(!menu_visible){if(!show_current_cached()){xSemaphoreTake(cache_mutex,portMAX_DELAY);show(home_cached?task_home.data():asset_ready);xSemaphoreGive(cache_mutex);}}
            }
            const bool short_down=dn&&!last_down&&!down_long;
            if(menu_visible&&!busy&&!menu_selecting) {
                if(!up&&last_up&&menu_index>0){menu_index--;menu_rendered=-1;menu_dirty=!cached_menu(menu_index,true);}
                if(short_down&&menu_index+1<menu_count){menu_index++;menu_rendered=-1;menu_dirty=!cached_menu(menu_index,true);}
            } else if(!menu_visible&&!woke_side&&!recording&&!busy&&!turn_id().empty()) {
                int limit=answer_view?pages.load():transcript_pages.load();
                if(now-last_side_press>=100000&&((!up&&last_up&&page>0)||(short_down&&!down_woke&&page+1<limit))) {
                    if(!up&&last_up)page--;else page++;
                    last_side_press=now;ESP_LOGI("voice","Page button: %d",page.load());
                    show_current_cached();page_request=true;
                }
            }
            last_up=up;last_down=dn;vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
}
