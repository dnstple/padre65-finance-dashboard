import { db, requireUser } from "./_lib.js";

// GET: product photos by line id (data URLs), cached in the browser for an hour
export default async function handler(req, res) {
  if (!requireUser(req, res)) return;
  const { rows } = await db().query("SELECT id, data FROM stock_plan_images");
  res.setHeader("Cache-Control", "private, max-age=3600");
  res.json(Object.fromEntries(rows.map(r => [String(r.id), r.data])));
}
