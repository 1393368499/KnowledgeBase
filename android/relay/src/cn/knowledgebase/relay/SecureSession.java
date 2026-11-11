package cn.knowledgebase.relay;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import org.json.JSONObject;

/** Paired device credentials, encrypted with a non-exportable Android key. */
final class SecureSession {
    private static final String ALIAS="kb-paired-session-v1";
    private static SecretKey key() throws Exception {
        KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);
        if(store.containsAlias(ALIAS))return (SecretKey)store.getKey(ALIAS,null);
        KeyGenerator generator=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,"AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
        return generator.generateKey();
    }
    static boolean exists(Context c){return c.getSharedPreferences("paired",0).contains("cipher");}
    static synchronized void save(Context c,JSONObject meta,String peer)throws Exception {
        JSONObject value=new JSONObject().put("server",meta.getString("server")).put("token",meta.getString("token"))
            .put("path","config/").put("method","GET").put("peer",peer);
        Protocol.endpoint(value);
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.ENCRYPT_MODE,key());
        byte[] raw=cipher.doFinal(value.toString().getBytes(StandardCharsets.UTF_8));
        c.getSharedPreferences("paired",0).edit().putString("iv",Base64.encodeToString(cipher.getIV(),Base64.NO_WRAP))
            .putString("cipher",Base64.encodeToString(raw,Base64.NO_WRAP)).apply();
    }
    static synchronized JSONObject load(Context c){try {
        android.content.SharedPreferences p=c.getSharedPreferences("paired",0);
        if(!p.contains("cipher"))return null;
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Base64.decode(p.getString("iv",""),Base64.NO_WRAP)));
        JSONObject value=new JSONObject(new String(cipher.doFinal(Base64.decode(p.getString("cipher",""),Base64.NO_WRAP)),StandardCharsets.UTF_8));
        Protocol.endpoint(value);return value;
    }catch(Exception e){clear(c);return null;}}
    static void clear(Context c){c.getSharedPreferences("paired",0).edit().clear().apply();}
}
