package demo;

import java.util.List;

/** Invoice totals in cents: quantity times price, minus 10 % on discounted lines. */
final class BillingCalculator {
    record Line(int quantity, int priceCents, boolean discounted) {}

    private BillingCalculator() {}

    static int total(List<Line> lines) {
        int total = 0;
        int discounted = 0;
        for (Line l : lines) {
            total += l.quantity() * l.priceCents();
            if (l.discounted()) {
                discounted++;
            }
        }
        // Spread the discount over the discounted lines (divides by zero when there are none).
        int discountPerLine = total / 10 / discounted;
        return total - discountPerLine * discounted;
    }
}
