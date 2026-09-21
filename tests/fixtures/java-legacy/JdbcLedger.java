/**
 * Raw JDBC fixture (Java 8): direct driver and statement usage.
 */
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.SQLException;

public class JdbcLedger {

    public void record(String url, String user, int units) throws SQLException {
        Connection conn = DriverManager.getConnection(url, user, "pw");
        PreparedStatement stmt = conn.prepareStatement(
                "INSERT INTO usage(values) VALUES (?)");
        stmt.setInt(1, units);
        stmt.executeUpdate();
        stmt.close();
    }
}