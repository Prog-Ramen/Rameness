# Open issues reported by users of `invoicer`

1. Tax is sometimes a cent off after a discount. Example: a taxable item at 1.50, an untaxable item at 2.70 and a
   15% discount give a taxable amount of 1.27. Compute the taxable amount exactly (the taxable lines' total minus
   their proportional share of the discount, with no intermediate rounding) and round only that final amount,
   half up: here 1.50 - 0.225 = 1.275, so the taxable amount should be 1.28.
2. Adding the same SKU at a different unit price merges it into the existing line and the second price is lost.
   Lines should only merge when both the SKU and the unit price match; otherwise keep a separate line.
3. Dates written with dots, like 05.03.2024 (day-first, 5 March 2024), crash the importer. They should parse.
4. An impossible date such as 31/02/2024 fails with an unhelpful error. It should raise a ValueError whose message
   includes the date text that was given.
5. Our security review found that a description starting with =, +, - or @ is exported to CSV as-is, and
   spreadsheets execute it as a formula. Such cells must be neutralised by prefixing them with a single quote (').
6. A line with a zero or negative quantity is accepted on a normal invoice. Adding one should raise a ValueError,
   except on a credit note (Invoice(..., credit_note=True)), where negative quantities are allowed.
