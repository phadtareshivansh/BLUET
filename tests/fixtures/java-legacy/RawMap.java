/**
 * Untyped/raw Map usage fixture (Java 8).
 */
import java.util.HashMap;
import java.util.Map;

public class RawMap {

    private Map store;

    public RawMap() {
        this.store = new HashMap();
    }

    public void put(String key, Object value) {
        this.store.put(key, value);
    }

    @SuppressWarnings("unchecked")
    public String get(String key) {
        return (String) this.store.get(key);
    }
}