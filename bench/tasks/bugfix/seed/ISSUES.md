# Open issues reported by users of `invoicer`

1. An item priced 12.345 in our catalogue shows as 12.34 on invoices. Our accountant says amounts must round
   half up, so this should be 12.35.
2. When the same product (same SKU) is added to an invoice twice, first 2 units and then 3, the invoice shows
   3 units instead of 5.
3. Dates in the format 05/03/2024 are imported as 3 May 2024. They are day-first: this is 5 March 2024.
4. Product descriptions that contain commas, such as "Bolt, M6, zinc", lose their commas in the CSV export.
5. An invoice created with a discount above 100% or below 0% is silently accepted and produces nonsense
   totals. Creating such an invoice should raise a ValueError.
