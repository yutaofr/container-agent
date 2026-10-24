package demo;

import java.util.List;

/** Invoice totals in cents: quantity times price, minus 10 % on discounted lines. */
final class BillingCalculator {
    record Line(int quantity, int priceCents, boolean discounted) {}

    private BillingCalculator() {}

    static int total(List<Line> lines) {
        int total = 0;
        for (Line l : lines) {
            int amount = l.quantity() * l.priceCents();
            total += l.discounted() ? amount - amount / 10 : amount;
        }
        return total;
    }
}
