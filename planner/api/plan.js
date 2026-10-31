import { DEFAULT_ID, PLAN_ID, db, nowIso, requireUser } from "./_lib.js";

// GET: the shared plan, the starting copy and the sales history (written by the daily sync).
// GET ?check=1: just the latest version, to notice your partner's changes.
// POST {plan, base}: save as the next version. If someone saved since `base`, their notes are kept too
// (the same rule as tracker/stockplan.save).
export default async function handler(req, res) {
  const user = requireUser(req, res);
  if (!user) return;
  res.setHeader("Cache-Control", "no-store");
  if (req.method === "GET") return req.query.check ? check(res) : load(res, user);
  if (req.method === "POST") return save(req, res, user);
  res.status(405).end();
}

async function check(res) {
  const { rows } = await db().query("SELECT version, updated_at, updated_by FROM stock_plans WHERE id = $1", [PLAN_ID]);
  const r = rows[0] || {};
  res.json({ version: Number(r.version || 0), updatedAt: r.updated_at, updatedBy: r.updated_by });
}

async function load(res, user) {
  const { rows } = await db().query("SELECT id, plan, version, updated_at, updated_by FROM stock_plans WHERE id = ANY($1)", [[PLAN_ID, DEFAULT_ID]]);
  const live = rows.find(r => r.id === PLAN_ID), start = rows.find(r => r.id === DEFAULT_ID);
  if (!live || !start) return res.status(404).json({ error: "There's no stock plan in the database yet" });
  const h = await db().query("SELECT data, updated_at FROM stock_plan_history WHERE id = $1", [PLAN_ID]);
  if (!h.rows.length) return res.status(404).json({ error: "The sales history hasn't been worked out yet: run python sync.py stock-plan-history" });
  res.json({ plan: JSON.parse(live.plan), start: JSON.parse(start.plan), history: JSON.parse(h.rows[0].data),
             historyAt: h.rows[0].updated_at, version: Number(live.version), updatedAt: live.updated_at, updatedBy: live.updated_by, user });
}

async function save(req, res, user) {
  const { plan, base } = req.body || {};
  if (!plan || !Array.isArray(plan.lines)) return res.status(400).json({ error: "Not a stock plan" });
  const client = await db().connect();
  try {
    await client.query("BEGIN");
    const { rows } = await client.query("SELECT plan, version FROM stock_plans WHERE id = $1 FOR UPDATE", [PLAN_ID]);
    const current = rows[0];
    if (current && Number(current.version) !== base) {
      const theirs = JSON.parse(current.plan).logs || {}, logs = plan.logs = plan.logs || {};
      for (const [key, entries] of Object.entries(theirs)) {
        const mine = logs[key] = logs[key] || [], ids = new Set(mine.map(e => e.id));
        mine.push(...entries.filter(e => !ids.has(e.id)));
      }
    }
    for (const line of plan.lines) delete line.img;  // photos are stored separately
    const version = (current ? Number(current.version) : 0) + 1, at = nowIso();
    await client.query(
      `INSERT INTO stock_plans (id, plan, version, updated_at, updated_by) VALUES ($1, $2, $3, $4, $5)
       ON CONFLICT (id) DO UPDATE SET plan = EXCLUDED.plan, version = EXCLUDED.version, updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by`,
      [PLAN_ID, JSON.stringify(plan), version, at, user]);
    await client.query("COMMIT");
    res.json({ version, updatedAt: at, updatedBy: user });
  } catch (e) {
    await client.query("ROLLBACK").catch(() => {});
    throw e;
  } finally {
    client.release();
  }
}
