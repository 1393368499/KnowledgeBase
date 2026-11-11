package cn.knowledgebase.relay;

import android.app.Instrumentation;
import android.os.Bundle;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.SharedPreferences;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import org.json.JSONObject;

/** Runs on the actual Android runtime, without network, recording or AI calls. */
public final class RelayChecks extends Instrumentation {
    private int passed=0;
    private void check(boolean ok){if(!ok)throw new AssertionError("Check "+passed);passed++;}
    private interface Throwing {void run()throws Exception;}
    private void rejects(Throwing action)throws Exception{try{action.run();}catch(Exception expected){passed++;return;}throw new AssertionError("Expected rejection at "+passed);}
    @Override public void onCreate(Bundle arguments){super.onCreate(arguments);start();}
    @Override public void onStart(){Bundle result=new Bundle();try{
        check(Protocol.chunkSize(517)==505);check(Protocol.chunkSize(247)==237);
        byte[] raw=new byte[1400];for(int i=0;i<raw.length;i++)raw[i]=(byte)i;
        byte[] p=Protocol.packet(6,65535,123456,raw,5,505);check(p.length==512);check(Protocol.id(p)==65535&&Protocol.offset(p)==123456);
        rejects(()->Protocol.packet(6,1,0,raw,0,507));rejects(()->Protocol.packet(6,1,0,raw,1390,20));
        rejects(()->new Protocol.Request(1,Protocol.MAX_META+1));
        JSONObject meta=new JSONObject().put("server","https://example.org").put("token","test_token_012345678901234567890").put("path","turns/").put("method","POST").put("length",raw.length).put("sha",Protocol.sha(raw));
        check(Protocol.endpoint(meta).equals("https://example.org/api/v1/voice/device/turns/"));
        check(Protocol.endpoint(new JSONObject(meta.toString()).put("path","tasks/").put("method","GET")).endsWith("/device/tasks/"));
        rejects(()->Protocol.endpoint(new JSONObject(meta.toString()).put("path","tasks/").put("method","POST")));
        for(String server:new String[]{"http://example.org","https://u:p@example.org","https://example.org:9999","https://example.org/a","https://example.org/?secret=1"}){
            JSONObject bad=new JSONObject(meta.toString()).put("server",server);rejects(()->Protocol.endpoint(bad));
        }
        rejects(()->Protocol.endpoint(new JSONObject(meta.toString()).put("path","../admin/")));
        rejects(()->Protocol.endpoint(new JSONObject(meta.toString()).put("method","DELETE")));
        byte[] header=meta.toString().getBytes(StandardCharsets.UTF_8);Protocol.Request request=new Protocol.Request(1,header.length);
        int split=header.length/2;request.appendMeta(0,Protocol.packet(2,1,0,header,0,split));request.appendMeta(split,Protocol.packet(2,1,split,header,split,header.length-split));
        request.appendAudio(0,Protocol.packet(3,1,0,raw,0,500));rejects(()->request.appendAudio(0,Protocol.packet(3,1,0,raw,0,5)));
        request.appendAudio(500,Protocol.packet(3,1,500,raw,500,500));request.appendAudio(1000,Protocol.packet(3,1,1000,raw,1000,400));check(Arrays.equals(raw,request.finish()));
        Protocol.Request incomplete=new Protocol.Request(2,header.length);incomplete.appendMeta(0,Protocol.packet(2,2,0,header,0,header.length));rejects(incomplete::finish);
        Protocol.Request gap=new Protocol.Request(3,header.length);rejects(()->gap.appendMeta(1,Protocol.packet(2,3,1,header,0,5)));
        check(Protocol.sha(new byte[0]).equals("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"));
        check(Protocol.ACK_WINDOW==8);
        check(request.ackDue(2)&&request.acknowledgedOffset(2)==header.length);
        check(request.ackDue(3)&&request.acknowledgedOffset(3)==raw.length);
        // Receipts acknowledge verified contiguous offsets, never skipped data.
        byte[] many=new byte[505*9+17];
        JSONObject windowMeta=new JSONObject(meta.toString()).put("length",many.length).put("sha",Protocol.sha(many));
        byte[] wh=windowMeta.toString().getBytes(StandardCharsets.UTF_8);
        Protocol.Request window=new Protocol.Request(4,wh.length);
        window.appendMeta(0,Protocol.packet(2,4,0,wh,0,wh.length));
        for(int i=0;i<7;i++)window.appendAudio(i*505,Protocol.packet(3,4,i*505,many,i*505,505));
        check(!window.ackDue(3));
        window.appendAudio(7*505,Protocol.packet(3,4,7*505,many,7*505,505));
        check(window.ackDue(3)&&window.acknowledgedOffset(3)==4040);
        rejects(()->window.appendAudio(9*505,Protocol.packet(3,4,9*505,many,9*505,17)));
        check(window.acknowledgedOffset(3)==4040);
        window.appendAudio(8*505,Protocol.packet(3,4,8*505,many,8*505,505));check(!window.ackDue(3));
        window.appendAudio(9*505,Protocol.packet(3,4,9*505,many,9*505,17));
        check(window.ackDue(3)&&Arrays.equals(many,window.finish()));
        // A reconnected transfer starts with empty buffers and offset zero.
        Protocol.Request fresh=new Protocol.Request(4,wh.length);
        rejects(()->fresh.appendAudio(4040,Protocol.packet(3,4,4040,many,4040,505)));
        fresh.appendMeta(0,Protocol.packet(2,4,0,wh,0,wh.length));
        check(fresh.acknowledgedOffset(3)==0);
        // Isolate test storage: never replace the user's actual paired session.
        Context isolated=new ContextWrapper(getTargetContext()){
            @Override public SharedPreferences getSharedPreferences(String name,int mode){return super.getSharedPreferences("relay_checks_"+name,mode);}
        };
        try{
            SecureSession.clear(isolated);check(!SecureSession.exists(isolated));
            SecureSession.save(isolated,meta,"AA:BB:CC:DD:EE:FF");
            check(SecureSession.exists(isolated));
            JSONObject restored=SecureSession.load(isolated);
            check(restored!=null&&restored.getString("token").equals(meta.getString("token")));
            check(restored.getString("peer").equals("AA:BB:CC:DD:EE:FF"));
            check(!isolated.getSharedPreferences("paired",0).getAll().toString().contains(meta.getString("token")));
            isolated.getSharedPreferences("paired",0).edit().putString("cipher","AAAA").commit();
            check(SecureSession.load(isolated)==null&&!SecureSession.exists(isolated));
        }finally{SecureSession.clear(isolated);}
        result.putString("stream","PASS "+passed+" protocol checks on Android\n");finish(-1,result);
    }catch(Throwable failure){result.putString("stream","FAIL after "+passed+": "+failure+"\n");finish(0,result);}}
}
