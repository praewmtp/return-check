# return-check (ตรวจรับของคืน)

Mobile web page for warehouse staff to confirm that returned goods really came back, and for
admins to reconcile marketplace cancellations and refunds (Shopee, TikTok, Lazada) against the
SR (goods-received) report from Express.

- `public/index.html` — the whole front end (no build step).
- `api/index.py` — one Vercel Python function: sign-in, card state, staff actions, weekly import.
- Database: Postgres (set `DATABASE_URL`, e.g. from the Neon integration on Vercel). Tables are created on first use.

## How it works

1. First visit asks for two PINs (staff, admin). They are stored hashed in the database.
2. Staff sign in with a name and the staff PIN, find a parcel, and tap: got everything / incomplete / not received.
3. An admin uploads the week's export files in one go. The server recognises each file by its column
   headers, shows a preview (rows = cards + skipped, new cards, duplicates), and writes only after confirmation.
4. Uploads never overwrite what staff tapped. Uploading the same file twice adds nothing.
5. SR lines are matched by the order number written under each line; product, quantity and price are
   compared with the marketplace data, which is treated as the source of truth.

## Privacy

Uploaded files are parsed in memory and not stored. Only order numbers, tracking numbers, products,
quantities, amounts and dates are kept. Buyer names, phone numbers and addresses are dropped.
No data files belong in this repository (see `.gitignore`).
