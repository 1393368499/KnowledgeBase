package cn.knowledgebase.relay;

import android.Manifest;
import android.app.*;
import android.bluetooth.*;
import android.bluetooth.le.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.net.*;
import android.os.*;
import java.io.*;
import java.net.*;
import javax.net.ssl.HttpsURLConnection;
import java.nio.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import org.json.JSONObject;
import org.json.JSONArray;

public final class RelayService extends Service {
    static final String UPDATE="cn.knowledgebase.relay.UPDATE";
    static final UUID SERVICE=UUID.fromString("b04e0001-7a31-4aa4-9b8f-278ba019f604"),
        RX=UUID.fromString("b04e0002-7a31-4aa4-9b8f-278ba019f604"),
        TX=UUID.fromString("b04e0003-7a31-4aa4-9b8f-278ba019f604"),
        INFO=UUID.fromString("b04e0004-7a31-4aa4-9b8f-278ba019f604"),
        CCCD=UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    static volatile String state="尚未连接开发板", boardInfo="连接后显示型号、固件、屏幕和蓝牙地址", transfer="等待录音";
    static volatile JSONArray tasks=new JSONArray();
    static volatile int activeTasks=0;
    static volatile JSONObject taskDetail=null;
    static volatile String detailId="";
    static volatile boolean taskFeedOnline=false;
    static volatile String serverStatus="尚未连接电脑知识库";
    static volatile long lastSyncAt=0;
    static volatile boolean endpointChanged=false;
    static volatile int activeConversation=0,routeVersion=0,syncedRoute=0,syncedConversation=0;
    static volatile String activeTaskTitle="新任务";
    static volatile String taskMessage="";
    static volatile int taskMessageVersion=0,deletePreviewVersion=0;
    static volatile JSONObject deletePreview=null;
    private static final Map<String,String> found=Collections.synchronizedMap(new LinkedHashMap<>());
    static Map<String,String> foundSnapshot(){synchronized(found){return new LinkedHashMap<>(found);}}
    private final Handler handler=new Handler(Looper.getMainLooper());
    private final ExecutorService http=Executors.newFixedThreadPool(4);
    private final Map<Integer,Protocol.Request> incoming=new HashMap<>();
    private final ArrayDeque<byte[]> writes=new ArrayDeque<>();
    private BluetoothAdapter adapter;
    private volatile BluetoothGatt gatt;
    private BluetoothGattCharacteristic rx;
    private int mtu=23, generation=0,retry=0;
    private boolean writing=false,wanted=false,scanning=false;
    private String address="",hardwareInfo="";
    private long writtenAt=0;
    private volatile long lastTraffic=0;
    private volatile JSONObject session=null;
    private volatile boolean pollingTasks=false;
    private long lastTaskPoll=0;
    private int queuedRoute=0,queuedConversation=-1;
    private boolean highPriority=true;
    private boolean uploadAck=false;
    private boolean receiverRegistered=false;
    private String savedSessionToken="";
    private String requestedDetail="";
    private final ConnectivityManager.NetworkCallback networks=new ConnectivityManager.NetworkCallback(){
        @Override public void onAvailable(Network n){handler.post(()->{lastTaskPoll=0;pollTasks();});}
        @Override public void onCapabilitiesChanged(Network n,NetworkCapabilities caps){if(caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED))handler.post(()->{if(!taskFeedOnline){lastTaskPoll=0;pollTasks();}});}
        @Override public void onLost(Network n){handler.post(()->{taskFeedOnline=false;serverStatus="网络切换中，正在自动重连";lastTaskPoll=0;pollTasks();update();});}
    };

    static String networkLabel(Context c){ConnectivityManager cm=(ConnectivityManager)c.getSystemService(CONNECTIVITY_SERVICE);NetworkCapabilities n=cm.getNetworkCapabilities(cm.getActiveNetwork());if(n==null||!n.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET))return "手机当前没有可用网络";if(n.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR))return "正在使用手机移动数据";if(n.hasTransport(NetworkCapabilities.TRANSPORT_WIFI))return "正在使用手机 Wi-Fi 网络";return "手机网络可用";}
    private void update(){sendBroadcast(new Intent(UPDATE).setPackage(getPackageName()));}
    private void status(String value){state=value;update();}
    private final BroadcastReceiver bonds=new BroadcastReceiver(){@Override public void onReceive(Context c,Intent i){
        if(!BluetoothDevice.ACTION_BOND_STATE_CHANGED.equals(i.getAction()))return;
        BluetoothDevice d=i.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE,BluetoothDevice.class);
        if(d==null||!d.getAddress().equals(address)||gatt==null)return;
        int value=i.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE,BluetoothDevice.BOND_NONE);
        if(value==BluetoothDevice.BOND_BONDED){status("配对完成，正在连接服务…");gatt.discoverServices();}
        else if(value==BluetoothDevice.BOND_NONE){status("配对未完成，请重试并输入屏幕配对码");}
    }};
    @Override public void onCreate(){super.onCreate();adapter=((BluetoothManager)getSystemService(BLUETOOTH_SERVICE)).getAdapter();
        if(checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT)!=PackageManager.PERMISSION_GRANTED){status("请允许附近设备权限后连接");stopSelf();return;}
        loadDefaultEndpoint(this);
        session=SecureSession.load(this);
        if(session!=null){address=session.optString("peer");savedSessionToken=session.optString("token");serverStatus="正在恢复电脑连接";}
        NotificationManager nm=getSystemService(NotificationManager.class);nm.createNotificationChannel(new NotificationChannel("relay","蓝牙中转连接",NotificationManager.IMPORTANCE_LOW));
        Intent open=new Intent(this,MainActivity.class);PendingIntent pi=PendingIntent.getActivity(this,0,open,PendingIntent.FLAG_IMMUTABLE);
        startForeground(1,new Notification.Builder(this,"relay").setSmallIcon(android.R.drawable.stat_sys_data_bluetooth).setContentTitle("智库随行 · 蓝牙中转").setContentText("为 Note 4 转发语音与知识库回复，点击查看连接").setContentIntent(pi).setOngoing(true).build());
        registerReceiver(bonds,new IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED),Context.RECEIVER_EXPORTED);
        receiverRegistered=true;
        ((ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE)).registerDefaultNetworkCallback(networks);
        handler.postDelayed(maintenance,1000);
    }
    @Override public int onStartCommand(Intent i,int flags,int startId){
        String action=i==null?"restore":i.getAction();
        if("disconnect".equals(action)){wanted=false;getSharedPreferences("devices",0).edit().putBoolean("auto",false).apply();stopScan();close();status("蓝牙已断开 · 手机任务继续同步");return START_STICKY;}
        if(Build.VERSION.SDK_INT>=31&&checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT)!=PackageManager.PERMISSION_GRANTED){status("需要允许附近设备权限");stopSelf();return START_NOT_STICKY;}
        if("scan".equals(action))scan();
        else if("connect".equals(action)){String next=i.getStringExtra("address");if(session!=null&&!next.equals(session.optString("peer",address))){session=null;savedSessionToken="";SecureSession.clear(this);tasks=new JSONArray();taskDetail=null;detailId="";requestedDetail="";taskFeedOnline=false;lastSyncAt=0;routeVersion=0;activeConversation=0;activeTasks=0;lastTaskPoll=0;serverStatus="等待新开发板认证";}address=next;wanted=true;getSharedPreferences("devices",0).edit().putBoolean("auto",true).apply();retry=0;connect();}
        else if("detail".equals(action))loadDetail(i.getStringExtra("turn"));
        else if("cancel_task".equals(action))cancelTask(i.getStringExtra("turn"));
        else if("refresh_tasks".equals(action)||"endpoint_changed".equals(action)){lastTaskPoll=0;pollTasks();}
        else if("update_endpoint".equals(action))updateEndpoint(i.getStringExtra("server"));
        else if("choose_task".equals(action))chooseTask(i.getIntExtra("conversation",0));
        else if("rename_task".equals(action))renameTask(i.getIntExtra("conversation",0),i.getStringExtra("title"));
        else if("delete_prepare".equals(action))deleteTask(i.getIntExtra("conversation",0),"prepare",null);
        else if("delete_confirm".equals(action)||"delete_abandon".equals(action)){try{deleteTask(i.getIntExtra("conversation",0),"delete_confirm".equals(action)?"confirm":"abandon",new JSONObject(i.getStringExtra("proposal")));}catch(Exception e){message("删除确认已失效，请重新打开任务");}}
        else if("restore".equals(action)){address=getSharedPreferences("devices",0).getString("last","");wanted=!address.isEmpty()&&getSharedPreferences("devices",0).getBoolean("auto",true);if(wanted&&gatt==null)connect();pollTasks();}
        return START_STICKY;
    }
    private void scan(){if(adapter==null||!adapter.isEnabled()){status("请先开启手机蓝牙");return;}stopScan();
        for(BluetoothDevice d:adapter.getBondedDevices())if(d.getName()!=null&&d.getName().startsWith("KB Note4"))found.put(d.getAddress(),d.getName()+" · 已配对");
        ScanFilter f=new ScanFilter.Builder().setServiceUuid(new ParcelUuid(SERVICE)).build();
        adapter.getBluetoothLeScanner().startScan(Collections.singletonList(f),new ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(),scanner);scanning=true;status("正在查找开发板，点下方设备连接");handler.postDelayed(()->{stopScan();if(gatt==null)status(found.isEmpty()?"未找到开发板，请确认已开机并靠近手机":"请选择下方开发板进行配对 / 连接");},12000);
    }
    private final ScanCallback scanner=new ScanCallback(){@Override public void onScanResult(int type,ScanResult result){BluetoothDevice d=result.getDevice();String n=d.getName();found.put(d.getAddress(),n==null?"KB Note4":n);update();}@Override public void onScanFailed(int code){scanning=false;status("蓝牙扫描未完成，请重新尝试");}};
    private void stopScan(){if(scanning&&adapter!=null&&adapter.isEnabled()){adapter.getBluetoothLeScanner().stopScan(scanner);}scanning=false;}
    private void connect(){if(!wanted||address==null||address.isEmpty()||adapter==null||!adapter.isEnabled())return;stopScan();close();
        status("正在连接开发板…");try{gatt=adapter.getRemoteDevice(address).connectGatt(this,false,callbacks,BluetoothDevice.TRANSPORT_LE);}catch(Exception e){status("连接失败，请检查蓝牙后重试");}
    }
    private void close(){generation++;uploadAck=false;syncedRoute=0;syncedConversation=0;queuedRoute=0;queuedConversation=-1;if(gatt!=null){gatt.disconnect();gatt.close();gatt=null;}rx=null;mtu=23;writing=false;writes.clear();highPriority=true;synchronized(incoming){incoming.clear();}}
    private void disconnected(){close();status("连接已断开，正在重连…");if(wanted){int delay=Math.min(30000,1000<<Math.min(retry++,5));handler.postDelayed(()->{if(wanted&&gatt==null)connect();},delay);}}
    private final BluetoothGattCallback callbacks=new BluetoothGattCallback(){
        @Override public void onConnectionStateChange(BluetoothGatt g,int code,int next){handler.post(()->{if(g!=gatt)return;if(next==BluetoothProfile.STATE_CONNECTED&&code==0){retry=0;g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH);
            if(g.getDevice().getBondState()==BluetoothDevice.BOND_BONDED){status("正在发现开发板服务…");g.discoverServices();}
            else {status("请在系统配对窗口输入开发板屏幕上的 6 位码");g.getDevice().createBond();}
        }else if(next==BluetoothProfile.STATE_DISCONNECTED)disconnected();});}
        @Override public void onServicesDiscovered(BluetoothGatt g,int code){handler.post(()->{if(g!=gatt)return;if(code!=0||g.getService(SERVICE)==null){status("开发板固件不支持中转服务");return;}g.requestMtu(517);});}
        @Override public void onMtuChanged(BluetoothGatt g,int value,int code){handler.post(()->{if(g!=gatt)return;if(code!=0||value<247){status("蓝牙数据通道过小，请重新连接");return;}mtu=value;BluetoothGattService service=g.getService(SERVICE);rx=service.getCharacteristic(RX);BluetoothGattCharacteristic tx=service.getCharacteristic(TX);g.setCharacteristicNotification(tx,true);BluetoothGattDescriptor d=tx.getDescriptor(CCCD);if(d==null){status("开发板通知服务缺失");return;}g.writeDescriptor(d,BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);});}
        @Override public void onDescriptorWrite(BluetoothGatt g,BluetoothGattDescriptor d,int code){handler.post(()->{if(g!=gatt)return;if(code!=0){status("需要完成蓝牙安全配对");return;}g.readCharacteristic(g.getService(SERVICE).getCharacteristic(INFO));});}
        @Override public void onCharacteristicRead(BluetoothGatt g,BluetoothGattCharacteristic c,byte[] value,int code){handler.post(()->{if(g!=gatt)return;if(code!=0){status("读取设备信息失败，请重新配对");return;}try{JSONObject j=new JSONObject(new String(value,StandardCharsets.UTF_8));if(j.getInt("protocol")!=1)throw new Exception();uploadAck=j.optBoolean("upload_ack",false);
            hardwareInfo="型号："+j.getString("model")+"\n固件："+j.getString("firmware")+"\n屏幕："+j.getString("screen")+"\n录音："+j.getString("audio")+"\n蓝牙："+address+"\n通道：加密蓝牙 · MTU "+mtu;boardInfo=hardwareInfo;
            getSharedPreferences("devices",0).edit().putString("last",address).apply();found.put(address,"KB Note4 · 已添加");
            byte[] time=ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putLong(System.currentTimeMillis()/1000).array();enqueue(Protocol.packet(8,0,0,time,0,time.length));
        }catch(Exception e){status("设备协议版本不兼容");}});}
        @Override public void onCharacteristicWrite(BluetoothGatt g,BluetoothGattCharacteristic c,int code){handler.post(()->{if(g!=gatt)return;byte[] sent=writes.poll();writing=false;if(code!=0){disconnected();return;}if(sent!=null&&Protocol.type(sent)==8){status("蓝牙已连接，正在接通知识库…");transfer="等待开发板认证";update();}if(sent!=null&&Protocol.type(sent)==11){ByteBuffer b=ByteBuffer.wrap(sent,7,8).order(ByteOrder.LITTLE_ENDIAN);syncedRoute=b.getInt();syncedConversation=b.getInt();update();}pump();});}
        @Override public void onCharacteristicChanged(BluetoothGatt g,BluetoothGattCharacteristic c,byte[] value){byte[] packet=value.clone();handler.post(()->{if(g==gatt)receive(packet);});}
    };
    private void enqueue(byte[] packet){if(gatt==null||rx==null)return;if(writes.size()>1100){disconnected();return;}writes.add(packet);pump();}
    private void enqueuePriority(byte[] packet){
        if(gatt==null||rx==null)return;
        if(writing&&!writes.isEmpty()){byte[] first=writes.poll();writes.addFirst(packet);writes.addFirst(first);}
        else writes.addFirst(packet);
        pump();
    }
    private void pump(){if(writing||writes.isEmpty()||gatt==null||rx==null)return;boost();writing=true;writtenAt=SystemClock.elapsedRealtime();try{if(gatt.writeCharacteristic(rx,writes.peek(),BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT)!=BluetoothStatusCodes.SUCCESS){writing=false;disconnected();}}catch(RuntimeException e){writing=false;disconnected();}}
    private void receive(byte[] packet){if(packet.length<7||packet.length>512)return;lastTraffic=SystemClock.elapsedRealtime();int type=Protocol.type(packet),id=Protocol.id(packet),offset=Protocol.offset(packet);Protocol.Request complete=null;
        if(type==1)handler.post(()->boost());
        try{synchronized(incoming){if(type==1){if(incoming.size()>=4&&!incoming.containsKey(id))throw new IllegalArgumentException();incoming.put(id,new Protocol.Request(id,offset));}
            else if(type==10){incoming.remove(id);return;}
            else {Protocol.Request r=incoming.get(id);if(r==null)return;if(type==2){r.appendMeta(offset,packet);if(r.meta!=null){session=new JSONObject(r.meta.toString());handler.post(()->pollTasks());}}else if(type==3){r.appendAudio(offset,packet);if(r.audio.size()%32768<packet.length){transfer="正在接收录音："+(r.audio.size()/1024)+" KB";update();}}else if(type==4){incoming.remove(id);complete=r;}else throw new IllegalArgumentException();
                if(uploadAck&&(type==2||type==3)&&r.ackDue(type)){byte[] kind={(byte)type};enqueuePriority(Protocol.packet(12,id,r.acknowledgedOffset(type),kind,0,1));}
            }}
            if(complete!=null){Protocol.Request request=complete;int epoch=generation;http.execute(()->forward(request,epoch));}
        }catch(Exception e){synchronized(incoming){incoming.remove(id);}android.util.Log.w("KBRelay","Rejected BLE frame: type="+type+" id="+id+" offset="+offset+" bytes="+packet.length+" error="+e.getClass().getSimpleName());if(uploadAck)enqueuePriority(Protocol.packet(13,id,offset,new byte[0],0,0));transfer="蓝牙传输未完整结束，尚未提交知识库，正在重试";update();}
    }
    private void forward(Protocol.Request request,int epoch){PowerManager.WakeLock lock=((PowerManager)getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"kb:relay");lock.acquire(120000);
        HttpsURLConnection conn=null;int code=0;byte[] result=new byte[0];int pages=0,frames=0,menus=0,conversation=0;
        try{if(epoch!=generation)return;byte[] raw=request.finish();JSONObject meta=request.meta;
            String override=getSharedPreferences("devices",0).getString("server","");if(!override.isEmpty())meta.put("server",override);
            String endpoint=Protocol.endpoint(meta);
            transfer=raw.length>0?"录音已收全，正在发送到知识库":"正在同步知识库";update();
            conn=(HttpsURLConnection)new URL(endpoint).openConnection();conn.setConnectTimeout(15000);conn.setReadTimeout(90000);conn.setInstanceFollowRedirects(false);
            conn.setRequestProperty("Authorization","Bearer "+meta.getString("token"));conn.setRequestProperty("Accept-Encoding","identity");conn.setRequestMethod(meta.getString("method"));
            if(raw.length>0){String turn=UUID.fromString(meta.getString("turn")).toString();String head="--kbrelay\r\nContent-Disposition: form-data; name=\"turn_id\"\r\n\r\n"+turn+"\r\n--kbrelay\r\nContent-Disposition: form-data; name=\"config_version\"\r\n\r\n"+meta.getInt("version")+"\r\n--kbrelay\r\nContent-Disposition: form-data; name=\"audio_sha256\"\r\n\r\n"+meta.getString("sha")+"\r\n--kbrelay\r\nContent-Disposition: form-data; name=\"file\"; filename=\"recording.wav\"\r\nContent-Type: audio/wav\r\n\r\n";
                head=head.replace("\r\n--kbrelay\r\nContent-Disposition: form-data; name=\"audio_sha256\"", "\r\n--kbrelay\r\nContent-Disposition: form-data; name=\"route_version\"\r\n\r\n"+meta.optInt("route_version")+"\r\n--kbrelay\r\nContent-Disposition: form-data; name=\"audio_sha256\"");
                byte[] prefix=head.getBytes(StandardCharsets.UTF_8),suffix="\r\n--kbrelay--\r\n".getBytes(StandardCharsets.UTF_8);
                conn.setRequestProperty("Content-Type","multipart/form-data; boundary=kbrelay");conn.setDoOutput(true);conn.setFixedLengthStreamingMode(prefix.length+raw.length+suffix.length);
                try(OutputStream out=conn.getOutputStream()){out.write(prefix);out.write(raw);out.write(suffix);}
            }else if(meta.getString("method").equals("POST")){conn.setDoOutput(true);byte[] body=new byte[0];if(meta.getString("path").equals("task-selection/")){body=new JSONObject().put("conversation_id",meta.opt("conversation_id")).toString().getBytes(StandardCharsets.UTF_8);conn.setRequestProperty("Content-Type","application/json");}conn.setFixedLengthStreamingMode(body.length);try(OutputStream out=conn.getOutputStream()){out.write(body);}}
            code=conn.getResponseCode();InputStream stream=code>=400?conn.getErrorStream():conn.getInputStream();ByteArrayOutputStream body=new ByteArrayOutputStream();
            if(stream!=null)try(InputStream input=stream){byte[] b=new byte[4096];int n;while((n=input.read(b))!=-1){if(body.size()+n>Protocol.MAX_RESPONSE)throw new IOException("oversize");body.write(b,0,n);}}
            result=body.toByteArray();String digest=conn.getHeaderField("X-Content-SHA256");if(digest!=null&&!digest.equals(Protocol.sha(result)))throw new IOException("checksum");
            pages=conn.getHeaderFieldInt("X-Page-Count",0);frames=conn.getHeaderFieldInt("X-Frame-Count",0);
            menus=conn.getHeaderFieldInt("X-Menu-Count",0);conversation=conn.getHeaderFieldInt("X-Menu-Conversation",0);
            if(meta.getString("path").equals("config/")&&code==200){JSONObject j=new JSONObject(new String(result,StandardCharsets.UTF_8));String mode=j.optString("conversation_mode");boardInfo=hardwareInfo+"\n设备："+j.optString("name")+"\n设备编号："+j.optString("id")+"\n任务："+(mode.equals("tasks")?"按任务选择会话 · 最多 4 项并行":mode.equals("per_turn")?"每条语音一个会话":"同一会话");state="蓝牙已连接 · 热点可以关闭";}
            transfer=code>=200&&code<300?"中转正常 · 等待下一次操作":"知识库暂未就绪（"+code+"）";
        }catch(Exception e){code=0;result=new byte[0];transfer="网络暂不可用，请保持 App 运行，等待重试";}
        finally{if(conn!=null)conn.disconnect();if(lock.isHeld())lock.release();}
        try{JSONObject response=new JSONObject();response.put("code",code).put("length",result.length).put("sha",Protocol.sha(result)).put("pages",pages).put("frames",frames);
            response.put("menus",menus).put("conversation",conversation);
            byte[] header=response.toString().getBytes(StandardCharsets.UTF_8),body=result;
            handler.post(()->{if(epoch!=generation||gatt==null)return;int chunk=Protocol.chunkSize(mtu);enqueue(Protocol.packet(5,request.id,0,header,0,header.length));for(int p=0;p<body.length;p+=chunk){int n=Math.min(chunk,body.length-p);enqueue(Protocol.packet(6,request.id,p,body,p,n));}enqueue(Protocol.packet(7,request.id,body.length,new byte[0],0,0));update();});
        }catch(Exception ignored){}
    }
    private void boost(){lastTraffic=SystemClock.elapsedRealtime();if(gatt!=null&&!highPriority){gatt.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH);highPriority=true;}}
    private final Runnable maintenance=new Runnable(){@Override public void run(){long now=SystemClock.elapsedRealtime();if(endpointChanged){endpointChanged=false;lastTaskPoll=0;}synchronized(incoming){incoming.values().removeIf(r->now-r.started>180000);}if(writing&&now-writtenAt>20000)disconnected();if(gatt!=null&&!writing&&writes.isEmpty()&&highPriority&&now-lastTraffic>15000){gatt.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_BALANCED);highPriority=false;}if(now-lastTaskPoll>(taskFeedOnline&&activeTasks==0?5000:2000))pollTasks();update();handler.postDelayed(this,2000);}};
    private JSONObject taskRequest(String path,boolean post)throws Exception{return taskRequest(path,post,null);}
    private JSONObject taskRequest(String path,boolean post,JSONObject data)throws Exception{
        JSONObject current=session;if(current==null)throw new IOException("disconnected");
        JSONObject meta=new JSONObject(current.toString()).put("path",path).put("method",post?"POST":"GET");
        String override=getSharedPreferences("devices",0).getString("server","");if(!override.isEmpty())meta.put("server",override);
        HttpsURLConnection conn=(HttpsURLConnection)new URL(Protocol.endpoint(meta)).openConnection();
        try{conn.setConnectTimeout(15000);conn.setReadTimeout(20000);conn.setInstanceFollowRedirects(false);conn.setRequestMethod(post?"POST":"GET");conn.setRequestProperty("Authorization","Bearer "+meta.getString("token"));
            if(post){byte[] body=data==null?new byte[0]:data.toString().getBytes(StandardCharsets.UTF_8);conn.setDoOutput(true);conn.setRequestProperty("Content-Type","application/json");conn.setFixedLengthStreamingMode(body.length);try(OutputStream out=conn.getOutputStream()){out.write(body);}}
            int code=conn.getResponseCode();if(code!=200){String detail="请求未完成（"+code+"）";try(InputStream error=conn.getErrorStream()){if(error!=null){String raw=new String(error.readNBytes(4096),StandardCharsets.UTF_8);Object value=new org.json.JSONTokener(raw).nextValue();if(value instanceof JSONObject)detail=((JSONObject)value).optString("detail",detail);else if(value instanceof JSONArray&&((JSONArray)value).length()>0)detail=((JSONArray)value).optString(0,detail);}}catch(Exception ignored){}throw new IOException(detail);}
            try(InputStream in=conn.getInputStream()){ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[4096];int n;while((n=in.read(b))!=-1){if(out.size()+n>Protocol.MAX_RESPONSE)throw new IOException("oversize");out.write(b,0,n);}return new JSONObject(out.toString("UTF-8"));}
        }finally{conn.disconnect();}
    }
    private void pollTasks(){if(session==null||pollingTasks||SystemClock.elapsedRealtime()-lastTaskPoll<2000)return;pollingTasks=true;lastTaskPoll=SystemClock.elapsedRealtime();int epoch=generation;
        http.execute(()->{try{JSONObject result=taskRequest("tasks/",false);if(epoch!=generation)return;JSONObject current=session;
            if(current!=null&&!current.optString("token").equals(savedSessionToken)){try{SecureSession.save(this,current,address);savedSessionToken=current.optString("token");}catch(Exception e){android.util.Log.w("KBRelay","Cannot retain paired session: "+e.getClass().getSimpleName());}}
            handler.post(()->{if(epoch!=generation)return;tasks=result.optJSONArray("results");if(tasks==null)tasks=new JSONArray();activeTasks=result.optInt("active_count");taskFeedOnline=true;lastSyncAt=System.currentTimeMillis();serverStatus="电脑知识库已连接 · 自动同步中";applySelection(result);
                if(!detailId.isEmpty()){boolean present=false;for(int k=0;k<tasks.length();k++)if(String.valueOf(tasks.optJSONObject(k).optInt("conversation_id")).equals(detailId))present=true;if(present)loadDetail(detailId);else{detailId="";taskDetail=null;}}
                update();});}catch(Exception e){handler.post(()->{if(epoch!=generation)return;taskFeedOnline=false;serverStatus="暂时无法连接电脑，正在重试；请确认智库与远程通道已启动";update();});}finally{pollingTasks=false;}});
    }
    private void loadDetail(String id){if(id==null||!id.matches("[1-9][0-9]{0,9}"))return;requestedDetail=id;int epoch=generation;http.execute(()->{try{JSONObject result=taskRequest("tasks/"+id+"/",false);handler.post(()->{if(epoch!=generation||!id.equals(requestedDetail))return;taskDetail=result;detailId=id;update();});}catch(Exception e){transfer="任务详情暂时无法读取，请重试";update();}});}
    private void applySelection(JSONObject result){int version=result.optInt("route_version"),selected=result.optInt("active_conversation_id");if(version<routeVersion)return;routeVersion=version;activeConversation=selected;
        activeTaskTitle=result.optString("active_task_title",selected==0?"新任务":"任务 #"+selected);
        if(version<=0||rx==null||gatt==null||(version==queuedRoute&&selected==queuedConversation))return;
        queuedRoute=version;queuedConversation=selected;byte[] payload=ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putInt(version).putInt(selected).array();byte[] packet=Protocol.packet(11,0,0,payload,0,8);
        enqueuePriority(packet);
    }
    private void chooseTask(int conversation){int epoch=generation;http.execute(()->{try{JSONObject body=new JSONObject().put("conversation_id",conversation==0?JSONObject.NULL:conversation);JSONObject result=taskRequest("task-selection/",true,body);handler.post(()->{if(epoch!=generation)return;applySelection(result);transfer="正在将任务选择同步到开发板";lastTaskPoll=0;pollTasks();update();});}catch(Exception e){transfer="任务切换未成功，请保持蓝牙连接后重试";update();}});}
    private void cancelTask(String id){if(id==null||!id.matches("[0-9a-f-]{36}"))return;http.execute(()->{try{taskRequest("turns/"+id+"/cancel/",true);transfer="该任务已取消";}catch(Exception e){transfer="任务可能已进入 AI 处理，未能取消，请查看最新状态";}handler.post(()->pollTasks());update();});}
    private void message(String text){handler.post(()->{taskMessage=text;taskMessageVersion++;update();});}
    private void updateEndpoint(String candidate){
        JSONObject paired=session;int epoch=generation;
        if(paired==null){message("请先连接已配对开发板完成认证，再更新地址");return;}
        http.execute(()->{HttpsURLConnection conn=null;
            try{validateEndpoint(candidate);JSONObject meta=new JSONObject(paired.toString()).put("server",candidate).put("path","config/").put("method","GET");
                conn=(HttpsURLConnection)new URL(Protocol.endpoint(meta)).openConnection();conn.setConnectTimeout(10000);conn.setReadTimeout(10000);conn.setInstanceFollowRedirects(false);conn.setRequestProperty("Authorization","Bearer "+meta.getString("token"));
                if(conn.getResponseCode()!=200)throw new IOException("unavailable");
                JSONObject result;try(InputStream in=conn.getInputStream()){result=new JSONObject(new String(in.readNBytes(16384),StandardCharsets.UTF_8));}
                UUID.fromString(result.getString("id"));String expected=getSharedPreferences("devices",0).getString("verified_device_id","");
                if(!expected.isEmpty()&&!expected.equals(result.getString("id")))throw new IOException("identity");
                handler.post(()->{if(epoch!=generation||session==null||!paired.optString("token").equals(session.optString("token"))){message("连接已变化，请重新更新地址");return;}
                    getSharedPreferences("devices",0).edit().putString("server",candidate).putString("verified_device_id",result.optString("id")).apply();lastTaskPoll=0;pollTasks();message("地址验证通过，已更新并重新同步");});
            }catch(Exception e){message("新地址无法验证，原地址已保留。请确认电脑智库和远程通道已启动");}
            finally{if(conn!=null)conn.disconnect();}
        });
    }
    private void renameTask(int conversation,String title){int epoch=generation;http.execute(()->{try{JSONObject result=taskRequest("tasks/"+conversation+"/rename/",true,new JSONObject().put("title",title));handler.post(()->{if(epoch!=generation)return;if(result.has("route_version"))applySelection(result);lastTaskPoll=0;pollTasks();loadDetail(String.valueOf(conversation));message("任务和知识库会话已重命名");});}catch(Exception e){message("重命名未完成："+e.getMessage());}});}
    private void deleteTask(int conversation,String action,JSONObject proposal){int epoch=generation;http.execute(()->{try{JSONObject body=new JSONObject().put("action",action);if(proposal!=null)body.put("operation_id",proposal.getInt("operation_id")).put("confirm_token",proposal.optString("confirm_token"));JSONObject result=taskRequest("tasks/"+conversation+"/delete/",true,body);handler.post(()->{if(epoch!=generation)return;if(action.equals("prepare")){deletePreview=result;deletePreviewVersion++;update();}else{lastTaskPoll=0;taskDetail=null;detailId="";pollTasks();if(action.equals("confirm"))message("任务与知识库会话已同步删除");}});}catch(Exception e){message("删除未完成："+e.getMessage());}});}
    @Override public void onDestroy(){wanted=false;stopScan();close();handler.removeCallbacksAndMessages(null);if(receiverRegistered)unregisterReceiver(bonds);try{((ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE)).unregisterNetworkCallback(networks);}catch(Exception ignored){}http.shutdownNow();super.onDestroy();}
    @Override public IBinder onBind(Intent i){return null;}
    static void loadDefaultEndpoint(Context context){
        try(InputStream input=context.getAssets().open("endpoint.txt")){
            byte[] b=input.readNBytes(1024);String value=new String(b,StandardCharsets.UTF_8).trim();
            validateEndpoint(value);android.content.SharedPreferences prefs=context.getSharedPreferences("devices",0);String current=prefs.getString("server",""),previous=prefs.getString("built_in_server","");
            android.content.SharedPreferences.Editor edit=prefs.edit().putString("built_in_server",value);
            if(current.isEmpty()||current.equals(previous))edit.putString("server",value);edit.apply();
        }catch(Exception ignored){}
    }
    static void validateEndpoint(String value)throws Exception{
        Protocol.endpoint(new JSONObject().put("server",value).put("path","config/").put("method","GET").put("token","validation_token_0123456789"));
    }
}
