import { PEOPLE, clearSession, sameSecret, setSession } from "./_lib.js";

// POST {name, password}: one shared password; the name labels notes and saves. DELETE logs out.
export default async function handler(req, res) {
  if (req.method === "DELETE") { clearSession(res); return res.status(204).end(); }
  if (req.method !== "POST") return res.status(405).end();
  const { name, password } = req.body || {};
  const expected = process.env.PLANNER_PASSWORD;
  if (!expected || !process.env.SESSION_SECRET) return res.status(500).json({ error: "The planner's password isn't set up yet" });
  if (!PEOPLE.includes(name) || !sameSecret(password || "", expected)) {
    await new Promise(r => setTimeout(r, 600));  // slow down guessing
    return res.status(401).json({ error: "Wrong password" });
  }
  setSession(res, name);
  res.status(204).end();
}
