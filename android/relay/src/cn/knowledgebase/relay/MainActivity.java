package cn.knowledgebase.relay;

import android.Manifest;
import android.app.*;
import android.bluetooth.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.*;
import android.provider.Settings;
import android.text.TextUtils;
import android.view.*;
import android.widget.*;
import java.util.*;
import org.json.JSONObject;
import org.json.JSONArray;

public final class MainActivity extends Activity {
    private static final int INK=0xff17233d, MUTED=0xff69768c, BLUE=0xff285be5, BG=0xfff2f5fb;
    private LinearLayout tasksPage, devicesPage, deviceList, taskList;
    private TextView status, info, network, transfer, target, taskCount, feedStatus;
    private Button tasksTab, devicesTab;
    private int tab=0, expanded=-1, lastMessage=0, lastDelete=0;
    private String taskSnapshot="";
    private boolean scanAfterPermission=false;
    private final BroadcastReceiver updates=new BroadcastReceiver(){
        @Override public void onReceive(Context c,Intent i){render();}
    };
    private int dp(int n){return Math.round(n*getResources().getDisplayMetrics().density);}
    private GradientDrawable background(int color,int radius){
        GradientDrawable d=new GradientDrawable();d.setColor(color);d.setCornerRadius(dp(radius));return d;
    }
    private TextView text(String s,int size,int color){
        TextView v=new TextView(this);v.setText(s);v.setTextSize(size);v.setTextColor(color);
        v.setPadding(0,dp(5),0,dp(5));return v;
    }
    private LinearLayout column(){LinearLayout v=new LinearLayout(this);v.setOrientation(1);return v;}
    private LinearLayout card(LinearLayout parent){
        LinearLayout v=column();v.setPadding(dp(18),dp(14),dp(18),dp(14));v.setBackground(background(Color.WHITE,18));
        LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.bottomMargin=dp(14);parent.addView(v,p);return v;
    }
    private Button button(String s,LinearLayout parent,boolean primary,Runnable action){
        Button b=new Button(this);b.setText(s);b.setAllCaps(false);b.setTextSize(15);b.setTextColor(primary?Color.WHITE:BLUE);
        b.setBackground(background(primary?BLUE:0xffedf2ff,12));
        LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,dp(s.contains("\n")?72:48));p.topMargin=dp(8);parent.addView(b,p);
        b.setOnClickListener(v->action.run());return b;
    }
    @Override public void onCreate(Bundle saved){
        super.onCreate(saved);lastMessage=RelayService.taskMessageVersion;lastDelete=RelayService.deletePreviewVersion;
        getWindow().setStatusBarColor(BG);getWindow().setNavigationBarColor(BG);
        LinearLayout root=column();root.setBackgroundColor(BG);root.setPadding(dp(20),dp(16),dp(20),dp(12));setContentView(root);
        root.setOnApplyWindowInsetsListener((v,insets)->{
            android.graphics.Insets bars=insets.getInsets(WindowInsets.Type.systemBars());
            v.setPadding(dp(20),dp(12)+bars.top,dp(20),dp(8)+bars.bottom);return insets;
        });
        TextView brand=text("智库随行",28,INK);brand.setTypeface(null,Typeface.BOLD);root.addView(brand);
        root.addView(text("把想法说出来，让任务继续进行",14,MUTED));
        LinearLayout tabs=new LinearLayout(this);tabs.setPadding(0,dp(10),0,dp(14));root.addView(tabs);
        tasksTab=new Button(this);tasksTab.setText("任务中心");devicesTab=new Button(this);devicesTab.setText("设备连接");
        for(Button b:new Button[]{tasksTab,devicesTab}){b.setAllCaps(false);b.setTextSize(16);tabs.addView(b,new LinearLayout.LayoutParams(0,dp(48),1));}
        tasksTab.setOnClickListener(v->showTab(0));devicesTab.setOnClickListener(v->showTab(1));
        ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
        LinearLayout pages=column();scroll.addView(pages);tasksPage=column();devicesPage=column();pages.addView(tasksPage);pages.addView(devicesPage);
        buildTasks();buildDevices();
        registerReceiver(updates,new IntentFilter(RelayService.UPDATE),Context.RECEIVER_NOT_EXPORTED);
        tab=saved==null?(getSharedPreferences("devices",0).getString("last","").isEmpty()?1:0):saved.getInt("tab",0);
        expanded=saved==null?-1:saved.getInt("expanded",-1);showTab(tab);render();
        BluetoothAdapter adapter=((BluetoothManager)getSystemService(BLUETOOTH_SERVICE)).getAdapter();
        if(checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT)==0&&(SecureSession.exists(this)
            ||(adapter!=null&&adapter.isEnabled()&&getSharedPreferences("devices",0).getBoolean("auto",true)&&!getSharedPreferences("devices",0).getString("last","").isEmpty())))send("restore");
    }
    private void buildTasks(){
        LinearLayout selected=card(tasksPage);selected.addView(text("下一条语音发送到",13,MUTED));
        target=text("连接开发板后选择任务",19,INK);selected.addView(target);
        feedStatus=text("",13,MUTED);selected.addView(feedStatus);
        button("＋ 新建任务",selected,true,()->choose(0));
        taskCount=text("我的任务",19,INK);tasksPage.addView(taskCount);
        tasksPage.addView(text("点选任务展开内容和操作。同一任务按顺序处理，不同任务最多 4 项并行。",13,MUTED));
        taskList=column();tasksPage.addView(taskList);
        button("刷新任务",tasksPage,false,()->send("refresh_tasks"));
    }
    private void buildDevices(){
        LinearLayout connection=card(devicesPage);connection.addView(text("蓝牙连接",18,INK));
        status=text("",20,BLUE);connection.addView(status);
        button("配对 / 连接开发板",connection,true,()->beginScan());
        button("断开连接",connection,false,()->send("disconnect"));
        deviceList=column();connection.addView(deviceList);
        LinearLayout board=card(devicesPage);board.addView(text("开发板信息",18,INK));info=text("",14,MUTED);board.addView(info);
        LinearLayout relay=card(devicesPage);relay.addView(text("网络中转",18,INK));network=text("",15,INK);transfer=text("",14,MUTED);relay.addView(network);relay.addView(transfer);
        relay.addView(text("移动数据与 Wi-Fi 均可连接电脑。配对认证后，开发板断开时手机任务仍会自动同步。电脑智库和远程通道需要保持运行。",14,MUTED));
        button("后台运行设置",relay,false,()->startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,android.net.Uri.parse("package:"+getPackageName()))));
        button("更新连接地址",relay,false,()->serverSettings());
        LinearLayout controls=card(devicesPage);controls.addView(text("开发板操作",18,INK));
        controls.addView(text("按住正面按钮说话，松手发送。\n双击取消这条录音。\n长按下键打开任务列表；上下选择、正面确认。\n长按上键 5 秒重启，未发出的录音会阻止重启。\n10 秒无操作显示日历、任务和电量。",15,MUTED));
        controls.addView(text("首次配对时输入板端六位码，不需要通讯录权限。锁屏中转请保留连接通知并允许后台运行。",13,MUTED));
    }
    private void showTab(int value){
        tab=value;tasksPage.setVisibility(value==0?View.VISIBLE:View.GONE);devicesPage.setVisibility(value==1?View.VISIBLE:View.GONE);
        tasksTab.setTextColor(value==0?Color.WHITE:BLUE);tasksTab.setBackground(background(value==0?BLUE:0xffe7edfa,12));
        devicesTab.setTextColor(value==1?Color.WHITE:BLUE);devicesTab.setBackground(background(value==1?BLUE:0xffe7edfa,12));
    }
    private boolean permissions(){
        List<String> needed=new ArrayList<>();
        for(String p:new String[]{Manifest.permission.BLUETOOTH_CONNECT,Manifest.permission.BLUETOOTH_SCAN})
            if(checkSelfPermission(p)!=PackageManager.PERMISSION_GRANTED)needed.add(p);
        if(Build.VERSION.SDK_INT>=33&&checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=0
            &&!getPreferences(0).getBoolean("notificationAsked",false)){
            needed.add(Manifest.permission.POST_NOTIFICATIONS);getPreferences(0).edit().putBoolean("notificationAsked",true).apply();
        }
        if(needed.isEmpty())return true;scanAfterPermission=true;requestPermissions(needed.toArray(new String[0]),1);return false;
    }
    private boolean bluetoothPermission(){return checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT)==0&&checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN)==0;}
    private void send(String action){send(new Intent(this,RelayService.class).setAction(action));}
    private void send(Intent intent){if(!bluetoothPermission()){permissions();return;}startForegroundService(intent);}
    private void beginScan(){
        if(!permissions())return;BluetoothAdapter a=((BluetoothManager)getSystemService(BLUETOOTH_SERVICE)).getAdapter();
        if(a==null){Toast.makeText(this,"手机不支持蓝牙",Toast.LENGTH_SHORT).show();return;}
        if(!a.isEnabled()){startActivityForResult(new Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE),2);return;}showTab(1);send("scan");
    }
    private void choose(int conversation){send(new Intent(this,RelayService.class).setAction("choose_task").putExtra("conversation",conversation));}
    private String state(String s){
        switch(s){case "queued":return "排队等待";case "transcribing":return "识别语音";case "transcribed":return "准备回答";
            case "answering":return "正在处理";case "rendering":return "整理回复";case "answer_ready":return "已完成";
            case "canceled":return "已取消";case "uncertain":return "需核对";case "failed":return "未完成";default:return "处理中";}
    }
    private void rename(JSONObject item){
        EditText field=new EditText(this);field.setSingleLine(true);field.setText(item.optString("title"));field.setSelectAllOnFocus(true);
        LinearLayout wrap=column();wrap.setPadding(dp(22),dp(8),dp(22),0);wrap.addView(field);
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle("重命名任务").setMessage("名称会同步到知识库中的对应会话。")
            .setView(wrap).setNegativeButton("取消",null).setPositiveButton("保存",null).create();
        dialog.setOnShowListener(d->dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{
            String title=field.getText().toString().trim();if(title.isEmpty()||title.length()>200){field.setError("请输入 1–200 个字");return;}
            send(new Intent(this,RelayService.class).setAction("rename_task").putExtra("conversation",item.optInt("conversation_id")).putExtra("title",title));dialog.dismiss();
        }));dialog.show();
    }
    private void showDelete(JSONObject proposal){
        int conversation=proposal.optInt("conversation_id");String data=proposal.toString();
        Runnable abandon=()->send(new Intent(this,RelayService.class).setAction("delete_abandon").putExtra("conversation",conversation).putExtra("proposal",data));
        new AlertDialog.Builder(this).setTitle("删除「"+proposal.optString("title","任务")+"」？")
            .setMessage(proposal.optString("detail")+"\n\n此操作会影响知识库中的会话 #"+conversation+"。")
            .setNegativeButton("保留",(d,w)->abandon.run())
            .setPositiveButton("确认删除",(d,w)->{expanded=-1;send(new Intent(this,RelayService.class).setAction("delete_confirm").putExtra("conversation",conversation).putExtra("proposal",data));})
            .setOnCancelListener(d->abandon.run()).show();
    }
    private void history(LinearLayout parent,int conversation){
        JSONObject detail=RelayService.taskDetail;
        if(!String.valueOf(conversation).equals(RelayService.detailId)||detail==null){
            parent.addView(text("正在读取语音与回复…",14,MUTED));return;
        }
        JSONArray turns=detail.optJSONArray("turns");if(turns==null)return;
        JSONArray messages=detail.optJSONArray("messages");
        if(messages!=null&&messages.length()>0){
            parent.addView(text("会话最新内容 · 与电脑同步",13,MUTED));
            for(int k=0;k<messages.length();k++){JSONObject m=messages.optJSONObject(k);if(m==null)continue;
                String role=m.optString("role"),label="user".equals(role)?"你：":"assistant".equals(role)?"智库：":"系统：";
                TextView line=text(label+m.optString("content")+(m.optBoolean("truncated")?"\n（较长内容请到电脑查看全文）":""),15,"user".equals(role)?INK:MUTED);line.setTextIsSelectable(true);parent.addView(line);}
            JSONArray documents=detail.optJSONArray("documents");if(documents!=null)for(int k=0;k<documents.length();k++){JSONObject doc=documents.optJSONObject(k);if(doc!=null)parent.addView(text("文档 #"+doc.optInt("id")+" · "+doc.optString("title"),14,BLUE));}
            return;
        }
        parent.addView(text("最近 "+turns.length()+" 条语音",13,MUTED));
        for(int k=0;k<turns.length();k++){
            JSONObject turn=turns.optJSONObject(k);if(turn==null)continue;
            TextView q=text("你说："+(turn.optString("transcript").isEmpty()?"等待识别":turn.optString("transcript")),15,INK);q.setTextIsSelectable(true);parent.addView(q);
            String answer=turn.optString("answer_text");
            TextView a=text(answer.isEmpty()?state(turn.optString("state"))+"，同任务的后续语音将按顺序处理。":answer,15,MUTED);a.setTextIsSelectable(true);parent.addView(a);
        }
    }
    private void render(){
        status.setText(RelayService.state);info.setText(RelayService.boardInfo);network.setText(RelayService.networkLabel(this));transfer.setText(RelayService.transfer);
        target.setText(RelayService.activeTaskTitle+(RelayService.activeConversation>0?"  #"+RelayService.activeConversation:""));
        boolean synced=RelayService.routeVersion>0&&RelayService.syncedRoute==RelayService.routeVersion&&RelayService.syncedConversation==RelayService.activeConversation;
        String sync=RelayService.serverStatus;
        if(RelayService.lastSyncAt>0)sync+="\n最近同步 "+new java.text.SimpleDateFormat("HH:mm:ss",Locale.getDefault()).format(new Date(RelayService.lastSyncAt));
        if(RelayService.taskFeedOnline)sync+=synced?" · 已同步到开发板":" · 手机任务已同步";
        feedStatus.setText(sync);
        taskCount.setText("我的任务  ·  "+RelayService.activeTasks+" 项进行中");
        String key=RelayService.tasks.toString()+"|"+expanded+"|"+RelayService.detailId+"|"+String.valueOf(RelayService.taskDetail)+"|"+RelayService.activeConversation;
        if(!key.equals(taskSnapshot)||taskList.getChildCount()==0){
            taskSnapshot=key;taskList.removeAllViews();JSONArray rows=RelayService.tasks;
            if(rows.length()==0){LinearLayout empty=card(taskList);empty.addView(text("还没有任务",18,INK));empty.addView(text("连接开发板，按住说话即可开始。",14,MUTED));button("前往设备连接",empty,false,()->showTab(1));}
            for(int k=0;k<rows.length();k++){
                JSONObject item=rows.optJSONObject(k);if(item==null)continue;int conversation=item.optInt("conversation_id");boolean open=expanded==conversation;
                LinearLayout row=card(taskList);
                TextView title=text((open?"▾  ":"▸  ")+item.optString("title","语音任务"),18,INK);title.setTypeface(null,Typeface.BOLD);title.setMaxLines(2);title.setEllipsize(TextUtils.TruncateAt.END);row.addView(title);
                TextView meta=text(state(item.optString("state"))+" · "+item.optInt("recording_count")+" 条语音 · 会话 #"+conversation+(RelayService.activeConversation==conversation?" · 当前":""),13,BLUE);row.addView(meta);
                View.OnClickListener expand=v->{expanded=open?-1:conversation;taskSnapshot="";if(expanded>0){RelayService.detailId="";send(new Intent(this,RelayService.class).setAction("detail").putExtra("turn",String.valueOf(conversation)));}render();};row.setOnClickListener(expand);title.setOnClickListener(expand);meta.setOnClickListener(expand);
                if(open){
                    history(row,conversation);
                    button("继续这个任务",row,true,()->choose(conversation));
                    button("重命名",row,false,()->rename(item));
                    Button delete=button("删除任务与会话",row,false,()->send(new Intent(this,RelayService.class).setAction("delete_prepare").putExtra("conversation",conversation)));
                    delete.setTextColor(0xffbf3939);if(item.optInt("pending_count")>0){delete.setEnabled(false);row.addView(text("处理中，完成后可删除。",13,MUTED));}
                }
            }
        }
        deviceList.removeAllViews();
        for(Map.Entry<String,String> entry:RelayService.foundSnapshot().entrySet()){
            String address=entry.getKey();button(entry.getValue()+"\n"+address,deviceList,false,()->send(new Intent(this,RelayService.class).setAction("connect").putExtra("address",address)));
        }
        if(lastMessage!=RelayService.taskMessageVersion){lastMessage=RelayService.taskMessageVersion;Toast.makeText(this,RelayService.taskMessage,Toast.LENGTH_LONG).show();}
        if(lastDelete!=RelayService.deletePreviewVersion&&RelayService.deletePreview!=null){lastDelete=RelayService.deletePreviewVersion;showDelete(RelayService.deletePreview);}
    }
    private void serverSettings(){
        RelayService.loadDefaultEndpoint(this);EditText field=new EditText(this);field.setSingleLine(true);field.setText(getSharedPreferences("devices",0).getString("server",""));
        field.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_VARIATION_URI);
        AlertDialog d=new AlertDialog.Builder(this).setTitle("更新连接地址").setMessage("从电脑智库“设备管理”复制最新远程地址，在这里粘贴。验证通过后才替换当前地址；无需重新配对。").setView(field).setNegativeButton("取消",null).setNeutralButton("粘贴",null).setPositiveButton("验证并更新",null).create();
        d.setOnShowListener(x->{d.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener(v->{android.content.ClipboardManager clipboard=(android.content.ClipboardManager)getSystemService(CLIPBOARD_SERVICE);if(clipboard.hasPrimaryClip()&&clipboard.getPrimaryClip().getItemCount()>0)field.setText(clipboard.getPrimaryClip().getItemAt(0).coerceToText(this).toString().trim());});
            d.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{String value=field.getText().toString().trim();try{RelayService.validateEndpoint(value);send(new Intent(this,RelayService.class).setAction("update_endpoint").putExtra("server",value));d.dismiss();Toast.makeText(this,"正在验证连接地址…",Toast.LENGTH_SHORT).show();}catch(Exception e){field.setError("请输入有效的 HTTPS 地址");}});});d.show();
    }
    @Override public void onRequestPermissionsResult(int c,String[] n,int[] g){super.onRequestPermissionsResult(c,n,g);if(c==1&&scanAfterPermission){scanAfterPermission=false;if(bluetoothPermission())beginScan();else Toast.makeText(this,"需要附近设备权限才能连接",Toast.LENGTH_LONG).show();}}
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);if(request==2&&result==RESULT_OK)beginScan();}
    @Override public void onSaveInstanceState(Bundle state){state.putInt("tab",tab);state.putInt("expanded",expanded);super.onSaveInstanceState(state);}
    @Override public void onDestroy(){unregisterReceiver(updates);super.onDestroy();}
}
