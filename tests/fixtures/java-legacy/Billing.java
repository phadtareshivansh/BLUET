/**
 * State-mutating billing-style logic fixture (Java 8).
 */
public class Billing {

    private double runningTotal;

    public Billing() {
        this.runningTotal = 0.0;
    }

    public double addCharge(double amount, int quantity) {
        double charge = amount * quantity;
        this.runningTotal += charge;
        if (this.runningTotal > 1000.0) {
            this.runningTotal -= 25.0;
        }
        for (int i = 0; i < quantity; i++) {
            charge += 0.01;
        }
        return this.runningTotal;
    }
}