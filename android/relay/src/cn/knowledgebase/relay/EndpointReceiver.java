package cn.knowledgebase.relay;

import android.content.*;

/** Only the authorized debugging shell (DUMP permission) may provision a URL. */
public final class EndpointReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context c,Intent i){
        if(!"cn.knowledgebase.relay.SET_ENDPOINT".equals(i.getAction()))return;
        try{String value=i.getStringExtra("server");RelayService.validateEndpoint(value);
            c.getSharedPreferences("devices",0).edit().putString("server",value).apply();
            RelayService.endpointChanged=true;
            setResultCode(1);
        }catch(Exception e){setResultCode(0);}
    }
}
