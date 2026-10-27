#include <algorithm>
#include <array>
#include <atomic>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>
#include "audio_codec.h"
#include "button_clicks.h"
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
static std::atomic<int64_t> capture_time{0};
static std::atomic<int> page{0}, pages{0}, transcript_pages{0}, config_version{0};
static std::atomic<bool> page_request{false};
static bool configured=false;
static constexpr size_t MAX_AUDIO=16000*2*60;
static constexpr gpio_num_t OK=GPIO_NUM_0, UP=GPIO_NUM_39, DOWN=GPIO_NUM_18;
static std::array<uint8_t,15000> last_frame;
static std::atomic<const uint8_t*> last_status{nullptr};
static SemaphoreHandle_t cache_mutex;
struct CachedPage {std::string turn; bool answer; int index; uint8_t* data; uint32_t used;};
static std::vector<CachedPage> page_cache;
static uint32_t cache_clock=0;
static constexpr size_t CACHE_PAGES=64, FRAME_BYTES=15000;
static bool wifi_fast=false; // Network task owns this setting.
static void wifi_transfer_mode(bool fast) {
    if(wifi_fast!=fast){esp_wifi_set_ps(fast?WIFI_PS_NONE:WIFI_PS_MIN_MODEM);wifi_fast=fast;}
}

static std::string turn_id() {xSemaphoreTake(context_mutex,portMAX_DELAY);auto out=current_turn;xSemaphoreGive(context_mutex);return out;}
static void set_turn(const std::string& id) {xSemaphoreTake(context_mutex,portMAX_DELAY);current_turn=id;xSemaphoreGive(context_mutex);}


static void show(const uint8_t* data) {
    last_status=nullptr;
    if (display_queue) xQueueOverwrite(display_queue, data);
}
static void status(const uint8_t* data) {
    if(last_status.exchange(data)!=data && display_queue) xQueueOverwrite(display_queue,data);
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
struct Response { int code=0, page_count=0, frame_count=0; std::string body, checksum; };
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
                        bool post=false,esp_http_client_handle_t* control=nullptr) {
    Response r;
    if(!connected) return r;
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
static bool refresh_config() {
    auto r=request("config/"); if(r.code!=200)return false;
    auto* json=cJSON_Parse(r.body.c_str());if(!json)return false;
    config_version=number(json,"config_version");latest_answer=field(json,"latest_turn_id");
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
static bool fetch_page() {
    const auto id=turn_id();if(id.empty())return false;
    const bool answer=answer_view.load();const int index=page.load();
    if(!cached_page(id,answer,index)&&!download_pages(id,answer,index))return false;
    // A button may have changed the desired page while the network was busy.
    if(id==turn_id()&&answer==answer_view.load()&&index==page.load()&&!capturing)
        return cached_page(id,answer,index,true);
    return false;
}
static bool prefetch_pages() {
    const auto id=turn_id();if(id.empty()||capturing||busy)return false;
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
static void ask_cancel() {
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
    save_pending("");save_cancel("");audio_bytes=0;
    set_turn(previous_turn);pages=previous_pages;transcript_pages=previous_transcript_pages;
    page=previous_page;answer_view=previous_answer;page_request=false;
    cancel_requested=false;cancel_confirmed=false;send_phase=Idle;
    status(asset_canceled);busy=false;
    ESP_LOGI("voice","Canceled send finished; ready for a new recording");
}
static void network(void*) {
    bool initialized=false;
    std::string pending=nvs_string("pending");
    const auto pending_cancel=nvs_string("cancel");
    if(pending.empty()&&!pending_cancel.empty())pending=pending_cancel;
    int64_t last_heartbeat=0,last_prefetch=0;
    while(true) {
        if(connected&&!initialized) {
            time_t now; time(&now);
            if(now<1700000000) {vTaskDelay(pdMS_TO_TICKS(500));continue;}
            status(asset_server);initialized=refresh_config();
            if(initialized){
                ESP_LOGI("voice","Device authenticated and ready");status(asset_ready);
                if(!pending.empty()){
                    set_turn(pending);previous_turn=latest_answer;send_phase=Sending;busy=true;
                    if(pending_cancel==pending){cancel_requested=true;xTaskNotifyGive(cancel_task);}
                    pending.clear();
                } else if(!latest_answer.empty()){set_turn(latest_answer);send_phase=Sending;busy=true;}
            }
        }
        if(busy&&(send_phase==LocalCanceled||cancel_confirmed)){finish_canceled();continue;}
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
            bool final=false;
            std::string last_state;
            while(!cancel_requested&&!final&&esp_timer_get_time()<deadline) {
                if(r.code==200||r.code==202) {
                    auto* json=cJSON_Parse(r.body.c_str());
                    if(json) {
                        std::string state=field(json,"state");
                        if(state!=last_state)ESP_LOGI("voice","Turn state: %s",state.c_str());
                        if(state=="answer_ready") {
                            pages=number(json,"answer_page_count");transcript_pages=number(json,"transcript_page_count");
                            answer_view=true;page=0;final=fetch_page();
                        } else if(state=="canceled") {cancel_confirmed=true;final=true;}
                        else if(state=="failed"||state=="uncertain") {status(asset_error);final=true;}
                        else if(state!=last_state) {
                            if(state=="answering"||state=="rendering"||state=="transcribed") {
                                if(!field(json,"transcript").empty()){answer_view=false;page=0;fetch_page();}
                                else status(asset_thinking);
                            } else status(asset_transcribing);
                            last_state=state;
                        }
                        cJSON_Delete(json);
                    }
                } else if(r.code==401||r.code==403||r.code==409||r.code==404) {status(asset_error);final=true;}
                if(!final){vTaskDelay(pdMS_TO_TICKS(500));r=request("turns/"+id+"/");}
            }
            if(cancel_requested||cancel_confirmed)continue;
            if(final){save_pending("");save_cancel("");audio_bytes=0;send_phase=Idle;busy=false;}
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
            prefetch_pages();last_prefetch=esp_timer_get_time();
        }
        if(!capturing&&!busy)wifi_transfer_mode(false);
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
static void wav_header(size_t length) {
    uint8_t header[44]={ 'R','I','F','F',0,0,0,0,'W','A','V','E','f','m','t',' ',16,0,0,0,1,0,1,0,0x80,0x3e,0,0,0,0x7d,0,0,2,0,16,0,'d','a','t','a',0,0,0,0};
    uint32_t riff=length+36, pcm=length; memcpy(header+4,&riff,4);memcpy(header+40,&pcm,4);memcpy(audio,header,44);
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
    configured=!ssid.empty()&&server.rfind("https://",0)==0&&!token.empty();
    status(configured?asset_offline:asset_setup);
    if(!configured)return;
    audio=static_cast<uint8_t*>(heap_caps_malloc(MAX_AUDIO+44,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if(!audio){status(asset_error);return;}
    codec=board.PrepareAudio();
    if(!codec||!codec->valid()){status(asset_error);return;}
    codec->EnableOutput(false);codec->SetInputGain(24);
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
    if(xTaskCreate(cancellation,"cancel",12288,nullptr,4,&cancel_task)!=pdPASS){status(asset_error);return;}
    xTaskCreate(network,"network",12288,nullptr,4,&network_task);
    bool recording=false, canceled=false, last_up=true, last_down=true;
    int64_t last_side_press=0;
    ButtonClicks clicks;bool click_started_busy=false;
    auto on_click=[&](ClickAction action){
        if(action==ClickAction::Cancel)ask_cancel();
        else if(action==ClickAction::Single&&!click_started_busy&&!busy&&!capturing){
            if(!turn_id().empty()){answer_view=!answer_view.load();page=0;show_current_cached();page_request=true;}
            else status(asset_ready);
        }
    };
    int stable=1,last=1;int64_t changed=0,pressed=0;
    std::vector<int16_t> chunk(320);
    while(true) {
        int down=gpio_get_level(OK);int64_t now=esp_timer_get_time();
        if(down!=last){last=down;changed=now;}
        if(down!=stable&&now-changed>=30000) {
            stable=down;
            if(!stable) {
                pressed=now;
                if(!busy&&config_version>0) {
                    recording=true;canceled=false;audio_bytes=0;
                    capture_version=config_version.load();capturing=true;
                    board.SetPowerLed(true);ESP_LOGI("voice","Recording started");
                }
            } else {
                const bool short_press=now-pressed<200000;
                if(recording) {
                    recording=false;capturing=false;board.SetPowerLed(false);
                    if(canceled){audio_bytes=0;clicks.release(now,false);status(asset_canceled);}
                    else if(short_press||audio_bytes<6400) {
                        audio_bytes=0;
                        if(!clicks.pending())click_started_busy=false;
                        on_click(clicks.release(now,short_press));
                    } else {
                        clicks.release(now,false);
                        previous_turn=turn_id();previous_pages=pages.load();previous_transcript_pages=transcript_pages.load();
                        previous_page=page.load();previous_answer=answer_view.load();
                        cancel_requested=false;cancel_confirmed=false;
                        ESP_LOGI("voice","Recording captured: %u PCM bytes",unsigned(audio_bytes.load()));
                        wav_header(audio_bytes);const auto id=uuid();set_turn(id);save_cancel("");save_pending(id);
                        capture_time=now;send_after=now+800000;send_phase=Waiting;busy=true;
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
            if(!recording&&!busy&&!turn_id().empty()) {
                int limit=answer_view?pages.load():transcript_pages.load();
                if(now-last_side_press>=100000&&((!up&&last_up&&page>0)||(!dn&&last_down&&page+1<limit))) {
                    if(!up&&last_up)page--;else page++;
                    last_side_press=now;ESP_LOGI("voice","Page button: %d",page.load());
                    show_current_cached();page_request=true;
                }
            }
            last_up=up;last_down=dn;vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
}
