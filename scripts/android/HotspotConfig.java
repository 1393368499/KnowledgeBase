import android.content.Context;
import android.os.Looper;
import android.os.Parcel;
import android.os.Parcelable;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.lang.reflect.InvocationTargetException;
import java.util.Arrays;
import java.util.Objects;

/** Runs as the authorized Android shell; honors the OS Wi-Fi permissions.
 * Does not print hotspot identifiers or credentials. Not an installed app.
 */
public final class HotspotConfig {
    private static final String BACKUP = "/data/local/tmp/kb-note4-hotspot-original.parcel";

    public static void main(String[] args) throws Exception {
        String mode = args.length == 0 ? "read" : args[0];
        if (!Arrays.asList("read", "backup", "verify").contains(mode)) {
            throw new IllegalArgumentException("read | backup | verify");
        }
        Looper.prepareMainLooper();
        Class<?> activityThread = Class.forName("android.app.ActivityThread");
        Object thread = activityThread.getMethod("systemMain").invoke(null);
        Context system = (Context) activityThread.getMethod("getSystemContext").invoke(thread);
        Context shell = system.createPackageContext("com.android.shell", 0);
        Object wifi = shell.getSystemService("wifi");
        Class<?> wifiClass = Class.forName("android.net.wifi.WifiManager");
        Class<?> configClass = Class.forName("android.net.wifi.SoftApConfiguration");
        Object config;
        try {
            config = wifiClass.getMethod("getSoftApConfiguration").invoke(wifi);
        } catch (InvocationTargetException error) {
            throw new RuntimeException("OS denied hotspot configuration access: " + error.getCause().getClass().getSimpleName());
        }
        report(configClass, config);
        if (mode.equals("read")) return;
        File original = new File(BACKUP);
        if (mode.equals("backup")) {
            if (!original.exists()) {
                Parcel parcel = Parcel.obtain();
                ((Parcelable)config).writeToParcel(parcel, 0);
                byte[] bytes = parcel.marshall();
                parcel.recycle();
                try (FileOutputStream output = new FileOutputStream(original)) { output.write(bytes); }
                original.setReadable(false, false);
                original.setWritable(false, false);
                original.setReadable(true, true);
                original.setWritable(true, true);
            }
            System.out.println("ORIGINAL_BACKUP_SAVED");
            if (mode.equals("backup")) return;
        }
        byte[] bytes;
        try (FileInputStream input = new FileInputStream(original)) { bytes = input.readAllBytes(); }
        Parcel parcel = Parcel.obtain();
        parcel.unmarshall(bytes, 0, bytes.length); parcel.setDataPosition(0);
        Parcelable.Creator<?> creator = (Parcelable.Creator<?>)configClass.getField("CREATOR").get(null);
        Object before = creator.createFromParcel(parcel); parcel.recycle();
        verifyUnchanged(configClass, before, config);
        System.out.println("ORIGINAL_BAND=" + configClass.getMethod("getBand").invoke(before));
    }

    private static void report(Class<?> cls, Object config) throws Exception {
        System.out.println("CONFIG_BAND=" + cls.getMethod("getBand").invoke(config));
        System.out.println("CONFIG_CHANNEL=" + cls.getMethod("getChannel").invoke(config));
    }

    private static void verifyUnchanged(Class<?> cls, Object before, Object after) throws Exception {
        for (String name : Arrays.asList("getSsid", "getPassphrase", "getSecurityType", "getMaxNumberOfClients", "isAutoShutdownEnabled", "getShutdownTimeoutMillis")) {
            Object a = cls.getMethod(name).invoke(before);
            Object b = cls.getMethod(name).invoke(after);
            if (!Objects.equals(a, b)) throw new IllegalStateException("Unexpected configuration change: " + name);
        }
        System.out.println("OTHER_SETTINGS_UNCHANGED");
    }

}
