// Shared by the API routes: the database (the same Supabase Postgres as the dashboard) and the login cookie.
// Environment (Vercel project settings): DATABASE_URL, PLANNER_PASSWORD, SESSION_SECRET.
import crypto from "node:crypto";
import pg from "pg";

export const PLAN_ID = "winter-2027";
export const DEFAULT_ID = "winter-2027-default";
export const PEOPLE = ["Daniel", "Santiago"];
const COOKIE = "p65_planner";
const DAYS = 30;

let pool;
export function db() {
  // one small pool per warm function; Supabase needs SSL
  pool = pool || new pg.Pool({ connectionString: process.env.DATABASE_URL, ssl: { rejectUnauthorized: false }, max: 2 });
  return pool;
}

export const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");  // same format as the Python side

const sign = body => crypto.createHmac("sha256", process.env.SESSION_SECRET || "").update(body).digest("base64url");

export function sameSecret(a, b) {
  const x = crypto.createHash("sha256").update(String(a)).digest(), y = crypto.createHash("sha256").update(String(b)).digest();
  return crypto.timingSafeEqual(x, y);
}

export function setSession(res, user) {
  const body = Buffer.from(JSON.stringify({ u: user, exp: Date.now() + DAYS * 864e5 })).toString("base64url");
  res.setHeader("Set-Cookie", `${COOKIE}=${body}.${sign(body)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${DAYS * 86400}`);
}

export function clearSession(res) {
  res.setHeader("Set-Cookie", `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0`);
}

// the signed-in name, or null (and a 401 sent)
export function requireUser(req, res) {
  const raw = (req.headers.cookie || "").split(/;\s*/).find(c => c.startsWith(COOKIE + "="));
  const [body, mac] = raw ? raw.slice(COOKIE.length + 1).split(".") : [];
  if (body && mac && process.env.SESSION_SECRET && sameSecret(sign(body), mac)) {
    try {
      const s = JSON.parse(Buffer.from(body, "base64url").toString());
      if (s.exp > Date.now() && PEOPLE.includes(s.u)) return s.u;
    } catch (e) { /* bad cookie */ }
  }
  res.status(401).json({ error: "Please log in" });
  return null;
}
