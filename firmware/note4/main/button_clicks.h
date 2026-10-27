#pragma once
#include <cstdint>

enum class ClickAction { None, Single, Cancel };

// A recording release is never the first click of a double click.
class ButtonClicks {
public:
    static constexpr int64_t window_us=350000;
    constexpr ClickAction release(int64_t now,bool short_press) {
        if(!short_press){pending_=false;return ClickAction::None;}
        if(pending_&&now>=released_&&now-released_<=window_us){pending_=false;return ClickAction::Cancel;}
        pending_=true;released_=now;return ClickAction::None;
    }
    constexpr ClickAction poll(int64_t now,bool pressed) {
        if(pending_&&!pressed&&now-released_>window_us){pending_=false;return ClickAction::Single;}
        return ClickAction::None;
    }
    constexpr bool pending()const{return pending_;}
private:
    bool pending_=false;
    int64_t released_=0;
};
