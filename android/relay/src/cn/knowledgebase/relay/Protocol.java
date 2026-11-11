package cn.knowledgebase.relay;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.net.URI;
import java.util.regex.Pattern;
import org.json.JSONObject;

/** Bounded binary framing. No audio, credentials or transcripts are logged. */
final class Protocol {
    static final int MAX_AUDIO=2_000_044, MAX_RESPONSE=120_000, MAX_META=4096;
    static final int ACK_WINDOW=8;
    static int chunkSize(int mtu){return Math.min(512,mtu-3)-7;}
    static final Pattern PATH=Pattern.compile("(?:config/|heartbeat/|standby/|tasks/(?:[0-9]+/(?:(?:rename|delete)/)?)?|task-selection/|task-home/|task-menu/[0-9]+/|turns/|turns/[0-9a-f-]{36}/(?:(?:retry|cancel)/|pages/(?:answer|transcript)/(?:batch/)?[0-9]+/)?)");
    static int type(byte[] p){return p[0]&255;}
    static int id(byte[] p){return (p[1]&255)|((p[2]&255)<<8);}
    static int offset(byte[] p){return ByteBuffer.wrap(p,3,4).order(ByteOrder.LITTLE_ENDIAN).getInt();}
    static byte[] packet(int type,int id,int offset,byte[] data,int start,int count){
        if(count<0||count>505||start<0||start>data.length-count)throw new IllegalArgumentException("蓝牙分片长度无效");
        ByteBuffer b=ByteBuffer.allocate(7+count).order(ByteOrder.LITTLE_ENDIAN);
        b.put((byte)type).putShort((short)id).putInt(offset);if(count>0)b.put(data,start,count);return b.array();
    }
    static String sha(byte[] raw)throws Exception{
        byte[] digest=MessageDigest.getInstance("SHA-256").digest(raw);StringBuilder s=new StringBuilder(64);
        for(byte b:digest)s.append(String.format(java.util.Locale.ROOT,"%02x",b&255));return s.toString();
    }
    static String endpoint(JSONObject meta)throws Exception{
        URI base=new URI(meta.getString("server"));String path=meta.getString("path");
        if(!"https".equals(base.getScheme())||base.getHost()==null||base.getRawUserInfo()!=null||base.getPort()!=-1
            ||base.getRawQuery()!=null||base.getRawFragment()!=null||!(base.getPath().isEmpty()||base.getPath().equals("/"))
            ||!PATH.matcher(path).matches())throw new IllegalArgumentException("服务地址或请求路径无效");
        String method=meta.getString("method"),token=meta.getString("token");
        if(!(method.equals("GET")||method.equals("POST"))||!token.matches("[A-Za-z0-9_-]{20,128}"))throw new IllegalArgumentException("请求格式无效");
        if(method.equals("POST")&&!(path.equals("turns/")||path.equals("heartbeat/")||path.equals("config/")||path.equals("task-selection/")||path.matches("tasks/[0-9]+/(?:rename|delete)/")||path.matches("turns/[0-9a-f-]{36}/(?:cancel|retry)/")))throw new IllegalArgumentException("请求方法无效");
        return "https://"+base.getHost()+"/api/v1/voice/device/"+path;
    }
    static final class Request {
        final int id,total;final long started=android.os.SystemClock.elapsedRealtime();
        final ByteArrayOutputStream metadata=new ByteArrayOutputStream(),audio=new ByteArrayOutputStream();
        JSONObject meta;
        int metaFragments=0,audioFragments=0;
        Request(int id,int total){if(id==0||total<1||total>MAX_META)throw new IllegalArgumentException("请求头过大");this.id=id;this.total=total;}
        void appendMeta(int offset,byte[] p)throws Exception{
            if(meta!=null||offset!=metadata.size()||p.length-7>total-metadata.size())throw new IllegalArgumentException("请求头不完整");
            metadata.write(p,7,p.length-7);
            metaFragments++;
            if(metadata.size()==total){meta=new JSONObject(metadata.toString("UTF-8"));endpoint(meta);int n=meta.getInt("length");if(n<0||n>MAX_AUDIO)throw new IllegalArgumentException("录音超长");}
        }
        void appendAudio(int offset,byte[] p){
            if(meta==null||offset!=audio.size()||p.length-7>meta.optInt("length")-audio.size())throw new IllegalArgumentException("录音分片不完整");
            audio.write(p,7,p.length-7);
            audioFragments++;
        }
        boolean ackDue(int type){return type==2?(metaFragments%ACK_WINDOW==0||metadata.size()==total):
            type==3&&(audioFragments%ACK_WINDOW==0||audio.size()==meta.optInt("length"));}
        int acknowledgedOffset(int type){return type==2?metadata.size():audio.size();}
        byte[] finish()throws Exception{
            if(meta==null||audio.size()!=meta.getInt("length"))throw new IllegalArgumentException("录音未收全");
            byte[] raw=audio.toByteArray();
            if(raw.length>0&&(!meta.getString("path").equals("turns/")||!sha(raw).equals(meta.getString("sha"))))throw new IllegalArgumentException("录音校验失败");
            return raw;
        }
    }
}
