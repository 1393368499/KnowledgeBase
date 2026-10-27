#include "button_clicks.h"

constexpr bool click_contracts() {
    ButtonClicks single;
    if(single.release(100000,true)!=ClickAction::None)return false;
    if(single.poll(449999,false)!=ClickAction::None)return false;
    if(single.poll(450001,false)!=ClickAction::Single)return false;
    if(single.poll(900000,false)!=ClickAction::None)return false;
    ButtonClicks double_click;
    double_click.release(100000,true);
    if(double_click.release(450000,true)!=ClickAction::Cancel)return false;
    if(double_click.poll(900000,false)!=ClickAction::None)return false;
    ButtonClicks holding;
    holding.release(100000,true);
    if(holding.poll(800000,true)!=ClickAction::None)return false;
    holding.release(900000,false);
    if(holding.poll(1500000,false)!=ClickAction::None)return false;
    ButtonClicks recording;
    recording.release(1000000,false);
    if(recording.release(1100000,true)!=ClickAction::None)return false;
    if(recording.release(1300000,true)!=ClickAction::Cancel)return false;
    return true;
}
static_assert(click_contracts(),"Single/double click and recording-release contracts failed");
